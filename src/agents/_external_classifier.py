"""
agents/_external_classifier — adapter for the vendored Stage 1 classifier.

The classifier runtime code lives in ``src/eu_export``. It reads our core
data from ``docs/ASAP_Ontology_v1`` and orchestrates
its 7-step Stage 1 pipeline:

  1. ProductEvidenceState                → ProductClassificationInput
  2. CnCandidateRetriever.FindCandidates(...)
  3. OntologyContextBuilder.BuildContext(...)
  4. Stage1EvidencePackageBuilder.Build(...)
  5. Stage1RequestBuilder.BuildRequest(...)   → LlmRequest
  6. RuntimeAdapter.Generate(request)         → LlmResponse
  7. Stage1ResponseValidator + Stage1DecisionPolicy + Stage1TraversalController
     + Stage1RecommendationReportBuilder

Outputs collected into ExternalClassificationResult so ClassificationAgent
can stamp citations / reasoning / candidates onto the Blackboard.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any, Sequence


def _read_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


LLM_CANDIDATE_DESC_CHARS = _read_positive_int_env(
    "ASAP_CLASSIFIER_CANDIDATE_DESC_CHARS",
    180,
)
LLM_CANDIDATE_BRANCH_CHARS = _read_positive_int_env(
    "ASAP_CLASSIFIER_CANDIDATE_BRANCH_CHARS",
    120,
)
LLM_PRODUCT_TEXT_CHARS = _read_positive_int_env(
    "ASAP_CLASSIFIER_PRODUCT_TEXT_CHARS",
    1400,
)
LLM_DECISION_MAX_TOKENS = _read_positive_int_env(
    "ASAP_CLASSIFIER_DECISION_MAX_TOKENS",
    384,
)

ALLERGEN_TERMS = (
    "우유",
    "메밀",
    "땅콩",
    "대두",
    "밀",
    "고등어",
    "게",
    "새우",
    "돼지고기",
    "복숭아",
    "토마토",
    "아황산류",
    "호두",
    "닭고기",
    "쇠고기",
    "오징어",
    "조개류",
    "굴",
    "전복",
    "홍합",
    "잣",
    "계란",
    "난류",
)
ALLERGEN_DECLARATION_RE = re.compile(
    r"(?:(?:"
    + "|".join(re.escape(term) for term in sorted(ALLERGEN_TERMS, key=len, reverse=True))
    + r")(?:\s*\([^)]*\))?\s*[,·./、및 ]+\s*){1,}"
    r"(?:"
    + "|".join(re.escape(term) for term in sorted(ALLERGEN_TERMS, key=len, reverse=True))
    + r")(?:\s*\([^)]*\))?\s*함유"
)
ALLERGEN_NOTICE_MARKERS = (
    "알레르기",
    "알러지",
    "알레르겐",
    "혼입",
    "혼입가능",
    "혼입 가능",
    "같은 제조시설",
    "같은 제조 시설",
    "제조시설에서 제조",
    "사용한 제품과 같은",
    "may contain",
    "same facility",
    "same manufacturing",
    "allergen",
    "allergy",
)


def _strip_allergen_notice_text(text: str) -> str:
    """Remove allergen/cross-contact notices from classifier evidence text.

    Allergen declarations are labelling and safety evidence, not composition
    evidence for CN selection. Keep ingredient lines, but strip trailing
    declarations such as "밀,대두,계란 함유" and drop cross-contact lines.
    """
    normalized = str(text or "")
    if not normalized.strip():
        return ""
    lowered = normalized.lower()
    if any(marker in lowered for marker in ALLERGEN_NOTICE_MARKERS):
        return ""
    stripped = ALLERGEN_DECLARATION_RE.sub("", normalized)
    return re.sub(r"[,\s·./、]+$", "", stripped).strip()


def _clean_classifier_fact_texts(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [str(values)] if str(values or "").strip() else []
    cleaned: list[str] = []
    for value in values:
        text = _strip_allergen_notice_text(str(value))
        if text:
            cleaned.append(text)
    return cleaned


def _clean_classifier_ocr_text(value: Any) -> str:
    raw = str(value or "")
    if not raw.strip():
        return ""
    lines = []
    for line in raw.splitlines():
        cleaned = _strip_allergen_notice_text(line)
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _copy_with_clamped_max_tokens(request: Any, max_tokens: int) -> Any:
    """Return a copy of ``request`` whose generationOptions.maxTokens == max_tokens.

    Works for both the legacy ``@dataclass(frozen=True)`` LlmRequest /
    LlmGenerationOptions and the current pydantic ``BaseModel`` versions.
    """
    options = request.generationOptions
    if hasattr(options, "model_copy"):
        clamped_options = options.model_copy(update={"maxTokens": max_tokens})
    elif is_dataclass(options):
        clamped_options = replace(options, maxTokens=max_tokens)
    else:
        raise TypeError(
            f"Unsupported LlmGenerationOptions type: {type(options).__name__}"
        )
    if hasattr(request, "model_copy"):
        return request.model_copy(update={"generationOptions": clamped_options})
    if is_dataclass(request):
        return replace(request, generationOptions=clamped_options)
    raise TypeError(f"Unsupported LlmRequest type: {type(request).__name__}")


def _copy_with_prompts(
    request: Any,
    *,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    context_chunks: list[str] | None = None,
) -> Any:
    updates: dict[str, Any] = {}
    if system_prompt is not None:
        updates["systemPrompt"] = system_prompt
    if user_prompt is not None:
        updates["userPrompt"] = user_prompt
    if context_chunks is not None:
        updates["contextChunks"] = context_chunks
    if hasattr(request, "model_copy"):
        return request.model_copy(update=updates)
    if is_dataclass(request):
        return replace(request, **updates)
    raise TypeError(f"Unsupported LlmRequest type: {type(request).__name__}")


def _candidate_code(candidate: Any, *names: str) -> str:
    for name in names:
        value = getattr(candidate, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _build_candidate_contract(candidates: Sequence[Any]) -> list[dict[str, Any]]:
    contract: list[dict[str, Any]] = []
    for candidate in candidates:
        hs8 = _candidate_code(candidate, "hs8", "hs8Code")
        if not hs8:
            continue
        description = (
            getattr(candidate, "hs8Description", None)
            or getattr(candidate, "combinedDescription", None)
            or getattr(candidate, "candidateContextText", None)
            or ""
        )
        branch_context = getattr(candidate, "branchContext", None) or ""
        contract.append({
            "hs8": hs8,
            "hs6_code": _candidate_code(candidate, "hs6Code") or None,
            "cn8_description": str(description)[:LLM_CANDIDATE_DESC_CHARS],
            "branch_context": str(branch_context)[:LLM_CANDIDATE_BRANCH_CHARS],
            "path_codes": {
                "hs2": _candidate_code(candidate, "hs2Code") or None,
                "hs4": _candidate_code(candidate, "hs4Code") or None,
                "hs6": _candidate_code(candidate, "hs6Code") or None,
                "cn8": _candidate_code(candidate, "hs8Code", "hs8") or hs8,
            },
            "required_candidate_evidence_ref": f"cn_candidate:{hs8}",
        })
    return contract


def _build_stage1_response_skeleton(candidates: Sequence[Any]) -> dict[str, Any]:
    reviews: list[dict[str, Any]] = []
    for item in _build_candidate_contract(candidates):
        path_codes = item["path_codes"]
        reviews.append({
            "hs8": item["hs8"],
            "hs6_code": item["hs6_code"],
            "status": "possible_candidate",
            "supporting_product_facts": ["string"],
            "conflicting_or_exclusion_facts": [],
            "missing_information": ["string"],
            "evidence_refs": [item["required_candidate_evidence_ref"]],
            "classification_path_review": {
                "hs2": {
                    "code": path_codes["hs2"],
                    "consistency": "needs_review",
                    "comment": "string",
                },
                "hs4": {
                    "code": path_codes["hs4"],
                    "consistency": "needs_review",
                    "comment": "string",
                },
                "hs6": {
                    "code": path_codes["hs6"],
                    "consistency": "needs_review",
                    "comment": "string",
                },
                "cn8": {
                    "code": path_codes["cn8"],
                    "consistency": "needs_review",
                    "comment": "string",
                },
            },
            "classification_rule_review": {
                "include_rule_comment": "string",
                "exclude_rule_comment": "string",
                "hard_condition_comment": "string",
            },
            "similar_ebti_cases": [],
            "reason": "string",
            "human_review_required": True,
        })
    return {
        "classification_result": {
            "product_name": "string",
            "product_domain": "food_16_21 input domain value exactly as supplied if validator expects it",
            "domain_scopes": ["string"],
            "candidate_reviews": reviews,
            "not_enough_information": [],
            "recommended_next_action": "human_review_required",
            "human_review_warning": "This is a candidate review only, not a final customs determination.",
        }
    }


def _build_strict_stage1_prompt_suffix(
    product_input: Any,
    candidates: Sequence[Any],
) -> str:
    candidate_contract = _build_candidate_contract(candidates)
    skeleton = _build_stage1_response_skeleton(candidates[: min(len(candidates), 5)])
    return "\n".join([
        "",
        "STRICT_OUTPUT_CONTRACT_FOR_GEMMA4_CTX:",
        "Return exactly one valid JSON object. Do not return markdown, prose, comments, api_version, schema metadata, or an array root.",
        "The root object must contain only `classification_result`.",
        "Use the exact candidate hs8 codes below. Do not invent, normalize, or omit digits in reviewed candidates.",
        "Every candidate_review that you include must use one of these exact hs8 values and must cite its `required_candidate_evidence_ref` in evidence_refs.",
        "For every classification_path_review code, copy the code from path_codes exactly. If unsure about consistency, use `needs_review`, not a different enum.",
        "Allowed status values only: strong_candidate, possible_candidate, unlikely_candidate, insufficient_information.",
        "human_review_required must be the JSON boolean true for every candidate_review.",
        "product_domain must be exactly: {0}".format(product_input.productDomain),
        "domain_scopes must be exactly: {0}".format(json.dumps(product_input.domainScopes, ensure_ascii=False)),
        "candidate_contract:",
        json.dumps(candidate_contract, ensure_ascii=False, separators=(",", ":")),
        "minimal_shape_example:",
        json.dumps(skeleton, ensure_ascii=False, separators=(",", ":")),
    ])


def _harden_stage1_request(
    request: Any,
    product_input: Any,
    candidates: Sequence[Any],
) -> Any:
    system_suffix = "\n".join([
        "",
        "Hard output rule for local gemma4-ctx:",
        "You are filling a machine contract, not describing an API.",
        "Return only the requested classification_result JSON object.",
        "Never output api_version, api_version_info, OpenAPI-style schemas, explanations, or markdown fences.",
    ])
    user_suffix = _build_strict_stage1_prompt_suffix(product_input, candidates)
    return _copy_with_prompts(
        request,
        system_prompt=((request.systemPrompt or "").strip() + system_suffix).strip(),
        user_prompt=((request.userPrompt or "").strip() + user_suffix).strip(),
    )


def _build_repair_request(
    request: Any,
    product_input: Any,
    candidates: Sequence[Any],
    response_text: str,
    validation_report: Any,
) -> Any:
    issues = []
    for issue in getattr(validation_report, "issues", []) or []:
        issues.append({
            "severity": getattr(issue, "severity", ""),
            "issue_code": getattr(issue, "issueCode", ""),
            "field_path": getattr(issue, "fieldPath", ""),
            "message": getattr(issue, "message", ""),
        })
    repair_prompt = "\n".join([
        "Your previous answer did not satisfy the Stage 1 classification JSON contract.",
        "Rewrite it as one valid JSON object only. Do not include markdown or explanation.",
        _build_strict_stage1_prompt_suffix(product_input, candidates),
        "validator_issues:",
        json.dumps(issues[:20], ensure_ascii=False, separators=(",", ":")),
        "previous_response:",
        response_text[:3000],
    ])
    return _copy_with_prompts(request, user_prompt=repair_prompt)


def _build_compact_decision_request(
    request: Any,
    product_input: Any,
    candidates: Sequence[Any],
) -> Any:
    candidate_contract = _build_candidate_contract(candidates)
    compact_shape = {
        "selected_hs8": "one exact candidate hs8 or null",
        "candidate_reviews": [
            {
                "hs8": "exact candidate hs8",
                "status": (
                    "strong_candidate|possible_candidate|unlikely_candidate|"
                    "insufficient_information"
                ),
                "reason": "short reason",
                "supporting_product_facts": ["short fact"],
                "conflicting_or_exclusion_facts": [],
                "missing_information": [],
            }
        ],
        "not_enough_information": [],
    }
    prompt = "\n".join([
        "Select the most plausible CN8 candidate for the product.",
        "Return exactly one JSON object only. Do not output a JSON schema.",
        "Do not use keys named type, properties, api_version, review_details, or data.",
        "Use only exact hs8 values from candidate_contract.",
        "If no candidate is plausible, set selected_hs8 to null.",
        "Do not select an ingredient-specific candidate unless that ingredient or condition is explicit in the product facts.",
        "For example, `Containing eggs` requires explicit egg/난/albumen/egg powder evidence; fish/meat/stuffed candidates require explicit matching evidence and percentage conditions.",
        "For instant ramen/noodle products described as 유탕면/라면/dried noodles with wheat flour and no explicit egg/stuffed/fish/meat percentage condition, prefer the dry/other noodle candidate over egg/stuffed/meat/fish candidates.",
        "Allowed status values: strong_candidate, possible_candidate, unlikely_candidate, insufficient_information.",
        "Review the strongest few candidates; unreviewed candidates will be filled deterministically as unlikely/insufficient.",
        "product_name: {0}".format(product_input.productName or "unknown"),
        "product_domain: {0}".format(product_input.productDomain),
        "product_text:",
        product_input.BuildSearchText()[:LLM_PRODUCT_TEXT_CHARS],
        "candidate_contract:",
        json.dumps(candidate_contract, ensure_ascii=False, separators=(",", ":")),
        "required_output_shape:",
        json.dumps(compact_shape, ensure_ascii=False, separators=(",", ":")),
    ])
    return _copy_with_prompts(request, user_prompt=prompt, context_chunks=[])


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    index = 0
    while index < len(stripped):
        start = stripped.find("{", index)
        if start < 0:
            return None
        try:
            value, end = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            return value
        index = start + end
    return None


def _extract_compact_decision(
    text: str,
    candidates: Sequence[Any],
) -> dict[str, Any] | None:
    parsed = _extract_json_object(text)
    candidate_codes = {item["hs8"] for item in _build_candidate_contract(candidates)}
    if parsed is not None:
        selected = parsed.get("selected_hs8")
        if selected in candidate_codes or selected is None:
            return parsed

    # Salvage local-model outputs that start correctly but then degenerate
    # into repeated tokens before producing valid JSON.
    match = re.search(r'"selected_hs8"\s*:\s*"([0-9]{8})"', text or "")
    if match and match.group(1) in candidate_codes:
        selected_hs8 = match.group(1)
        return {
            "selected_hs8": selected_hs8,
            "candidate_reviews": [
                {
                    "hs8": selected_hs8,
                    "status": "possible_candidate",
                    "reason": (
                        "Recovered selected_hs8 from malformed compact LLM "
                        "response; full review generated deterministically."
                    ),
                    "supporting_product_facts": [],
                    "conflicting_or_exclusion_facts": [],
                    "missing_information": ["LLM compact JSON was malformed."],
                }
            ],
            "not_enough_information": ["LLM compact JSON was malformed."],
        }
    return None


def _apply_domain_selection_guard(
    compact: dict[str, Any],
    product_input: Any,
    candidates: Sequence[Any],
) -> dict[str, Any]:
    """Correct obvious local-LLM slips before Stage1 validator expansion."""
    candidate_codes = {item["hs8"] for item in _build_candidate_contract(candidates)}
    text = (product_input.BuildSearchText() or "").lower()
    ramen_like = any(
        token in text
        for token in ("라면", "ramen", "ramyun", "instant noodle", "유탕면")
    )
    stuffed_or_filled = any(token in text for token in ("stuffed", "filled pasta"))
    if "19023010" in candidate_codes and ramen_like and not stuffed_or_filled:
        out = dict(compact)
        out["selected_hs8"] = "19023010"
        reviews = [
            item
            for item in out.get("candidate_reviews") or []
            if isinstance(item, dict) and item.get("hs8") != "19023010"
        ]
        reviews.insert(0, {
            "hs8": "19023010",
            "status": "strong_candidate",
            "reason": (
                "Domain guard: ramen/유탕면 evidence indicates dried/other "
                "noodles under pasta/noodle heading rather than couscous or "
                "stuffed pasta."
            ),
            "supporting_product_facts": [
                "Product facts contain 라면/유탕면 and wheat-flour noodle evidence."
            ],
            "conflicting_or_exclusion_facts": [],
            "missing_information": [],
        })
        out["candidate_reviews"] = reviews
        return out
    return compact


def _copy_response_with_text(response: Any, generated_text: str) -> Any:
    if hasattr(response, "model_copy"):
        return response.model_copy(update={"generatedText": generated_text})
    if is_dataclass(response):
        return replace(response, generatedText=generated_text)
    raise TypeError(f"Unsupported LlmResponse type: {type(response).__name__}")


def _normalize_compact_status(value: Any) -> str:
    if value in {
        "strong_candidate",
        "possible_candidate",
        "unlikely_candidate",
        "insufficient_information",
    }:
        return str(value)
    return "insufficient_information"


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _expand_compact_decision_to_stage1_json(
    compact: dict[str, Any],
    product_input: Any,
    candidates: Sequence[Any],
) -> str:
    candidate_contract = _build_candidate_contract(candidates)
    candidate_codes = {item["hs8"] for item in candidate_contract}
    selected_hs8 = compact.get("selected_hs8")
    if selected_hs8 not in candidate_codes:
        selected_hs8 = None

    review_by_hs8: dict[str, dict[str, Any]] = {}
    for review in compact.get("candidate_reviews") or []:
        if not isinstance(review, dict):
            continue
        hs8 = review.get("hs8")
        if hs8 in candidate_codes:
            review_by_hs8[str(hs8)] = review

    expanded_reviews: list[dict[str, Any]] = []
    for index, item in enumerate(candidate_contract):
        hs8 = item["hs8"]
        compact_review = review_by_hs8.get(hs8, {})
        if hs8 == selected_hs8:
            status = _normalize_compact_status(
                compact_review.get("status") or "strong_candidate"
            )
            if status in {"unlikely_candidate", "insufficient_information"}:
                status = "possible_candidate"
        elif compact_review:
            status = _normalize_compact_status(compact_review.get("status"))
        else:
            status = "unlikely_candidate" if selected_hs8 else "insufficient_information"

        supporting = _list_of_strings(compact_review.get("supporting_product_facts"))
        conflicts = _list_of_strings(compact_review.get("conflicting_or_exclusion_facts"))
        missing = _list_of_strings(compact_review.get("missing_information"))
        reason = str(compact_review.get("reason") or "").strip()
        if not reason:
            if hs8 == selected_hs8:
                reason = "Selected as the most plausible CN8 candidate for human review."
            else:
                reason = "Not selected in the compact CN8 decision; retained only for audit."
        if not supporting and hs8 == selected_hs8:
            supporting = [product_input.BuildSearchText()[:300] or "Product facts support review."]
        if not missing and status == "insufficient_information":
            missing = ["More product composition/use details are required."]

        path_codes = item["path_codes"]

        def PathLevel(level: str) -> dict[str, Any]:
            return {
                "code": path_codes[level],
                "consistency": "needs_review",
                "comment": (
                    "Copied from candidate hierarchy; consistency remains subject to human review."
                ),
            }

        expanded_reviews.append({
            "hs8": hs8,
            "hs6_code": item["hs6_code"],
            "status": status,
            "supporting_product_facts": supporting,
            "conflicting_or_exclusion_facts": conflicts,
            "missing_information": missing,
            "evidence_refs": [item["required_candidate_evidence_ref"]],
            "classification_path_review": {
                "hs2": PathLevel("hs2"),
                "hs4": PathLevel("hs4"),
                "hs6": PathLevel("hs6"),
                "cn8": PathLevel("cn8"),
            },
            "classification_rule_review": {
                "include_rule_comment": "Reviewed against candidate include keywords where available.",
                "exclude_rule_comment": "No exclusion is finalized by the model; human review remains required.",
                "hard_condition_comment": "Hard conditions must be checked against product facts and official notes.",
            },
            "similar_ebti_cases": [],
            "reason": reason,
            "human_review_required": True,
        })

    payload = {
        "classification_result": {
            "product_name": product_input.productName or "unknown",
            "product_domain": product_input.productDomain,
            "domain_scopes": list(product_input.domainScopes),
            "candidate_reviews": expanded_reviews,
            "not_enough_information": _list_of_strings(
                compact.get("not_enough_information")
            ),
            "recommended_next_action": "human_review_required",
            "human_review_warning": (
                "This is a CN8 candidate review for human review, not a final "
                "legal/customs determination."
            ),
        }
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

# Max tokens for the LLM response. ASAPExpress default is 4096 which equals
# gemma4:26b's stock context window (4096) and causes ollama to abort mid-
# stream. With an 8192-context model (gemma4-ctx Modelfile override),
# 2048 leaves comfortable headroom for prompt + response.
LLM_MAX_TOKENS = 2048

ASAP_PROJECT_ROOT = Path(os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()
ASAP_SRC_ROOT = ASAP_PROJECT_ROOT / "src"
for _path in (ASAP_PROJECT_ROOT, ASAP_SRC_ROOT):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bussiness_logic.app_config import LoadAppConfig
from bussiness_logic.bridge import (
    BuildDefaultLlmRuntimeConfig,
    BuildLlmRuntimeConfigFromEnv,
    BuildRuntimeAdapter,
    BuildTextEmbeddingAdapter,
    BuildTextEmbeddingRuntimeConfig,
    ProbeRuntimeDependency,
    ProbeTextEmbeddingDependency,
    TextEmbeddingAdapterBuildError,
    TextEmbeddingGenerationError,
)
from bussiness_logic.core import (
    CnCandidateRetriever,
    CnSemanticCandidateIndex,
    OntologyContextBuilder,
    ProductClassificationInput,
    Stage1DecisionPolicy,
    Stage1EvidencePackageBuilder,
    Stage1RecommendationReportBuilder,
    Stage1RequestBuilder,
    Stage1ResponseValidator,
    Stage1TraversalController,
)

try:
    from agents.document_package import _connect_db, _release_db
except Exception:  # pragma: no cover - classifier can still import without DB.
    _connect_db = None
    _release_db = None


APP_CONFIG = LoadAppConfig(ASAP_PROJECT_ROOT)
ASAP_ONTOLOGY_ROOT = APP_CONFIG.paths.ResolvePath(
    ASAP_PROJECT_ROOT,
    APP_CONFIG.paths.ontology_root,
)
ASAP_ENV_FILE = ASAP_PROJECT_ROOT / ".env"
SEMANTIC_CANDIDATE_INDEX: CnSemanticCandidateIndex | None = None
SEMANTIC_CANDIDATE_INDEX_STATUS: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# WCO 9-group classification routing — CN candidate buckets.
# ---------------------------------------------------------------------------
# The classifier retrieves candidates from WCO-Section-style chapter groups that
# partition all of chapters 01-97 (full coverage, no silent fallback), PLUS two
# narrow demo sub-buckets (prepared food 16-21, cosmetics 32-33) kept for the
# capstone demo's precision. Routing is deterministic: a candidate chapter maps
# to its demo sub-bucket if applicable, else to its WCO group. This avoids the
# keyword-based mis-routing that put e.g. furniture (ch94) into a cosmetics
# bucket. Regulatory domain scopes (food/cosmetics/animal_origin/... from
# domain_scope_routes) remain the DocumentAgent vocabulary; they are not used as
# classifier buckets here.
def _chapter_range(lo: int, hi: int) -> frozenset[str]:
    return frozenset(f"{c:02d}" for c in range(lo, hi + 1))


# 9 WCO-Section groups — a complete partition of chapters 01-97.
WCO_GROUP_CHAPTERS: dict[str, frozenset[str]] = {
    "wco1_raw_materials": _chapter_range(1, 15),      # animal/vegetable raw
    "wco2_prepared_food": _chapter_range(16, 24),     # prepared food/beverages
    "wco3_chemicals": _chapter_range(25, 38),         # mineral/chemical
    "wco4_plastics_leather": _chapter_range(39, 43),  # rubber/plastic/leather
    "wco5_textiles_wood": _chapter_range(44, 67),     # wood/paper/textile
    "wco6_stone_metal": _chapter_range(68, 83),       # stone/glass/metal
    "wco7_machinery": _chapter_range(84, 85),         # machinery/electrical
    "wco8_transport": _chapter_range(86, 92),         # transport/precision
    "wco9_misc_finished": _chapter_range(93, 97),     # arms/finished/other
}

# Narrow demo sub-buckets layered over WCO groups for the capstone demo. A
# chapter in one of these ranges routes here (precision) instead of the broader
# WCO group it also belongs to (16-21 ⊂ wco2, 32-33 ⊂ wco3).
DEMO_BUCKET_CHAPTERS: dict[str, frozenset[str]] = {
    "food_16_21": _chapter_range(16, 21),
    "cosmetics": frozenset({"32", "33"}),
}

# Pre-gate / screening scopes are document concerns (sanctions = ALL chapters,
# cites = species screening), never CN candidate buckets. Kept for the
# domain_scope_routes loader used elsewhere.
_PRE_GATE_SCOPES = frozenset({"sanctions", "cites"})

# Deterministic chapter -> demo sub-bucket precedence (checked before WCO group).
_CHAPTER_TO_DEMO_BUCKET: dict[str, str] = {
    ch: bucket
    for bucket, chapters in DEMO_BUCKET_CHAPTERS.items()
    for ch in chapters
}

_DOMAIN_SCOPE_ROUTE_CHAPTERS: dict[str, frozenset[str]] | None = None
_CLASSIFIER_BUCKET_CHAPTERS: dict[str, frozenset[str]] | None = None


def load_domain_scope_route_chapters() -> dict[str, frozenset[str]]:
    """scope -> {zero-padded chapter} from domain_scope_routes (Supabase, cached).

    Regulatory-domain vocabulary used by the DocumentAgent side; skips universal
    "ALL" rows and pre-gate screening scopes. Not used for classifier buckets.
    """
    global _DOMAIN_SCOPE_ROUTE_CHAPTERS
    if _DOMAIN_SCOPE_ROUTE_CHAPTERS is not None:
        return _DOMAIN_SCOPE_ROUTE_CHAPTERS
    out: dict[str, frozenset[str]] = {}
    if _connect_db is None or _release_db is None:
        _DOMAIN_SCOPE_ROUTE_CHAPTERS = out
        return out
    conn = _connect_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT domain_scope, chapters FROM domain_scope_routes")
        fetched = cur.fetchall()
        cur.close()
    finally:
        _release_db(conn)
    for scope_raw, chapters_raw in fetched:
        scope = str(scope_raw or "").strip().lower()
        chapters_text = str(chapters_raw or "").strip()
        if not scope or not chapters_text:
            continue
        if scope in _PRE_GATE_SCOPES or chapters_text.upper() == "ALL":
            continue
        chapters = frozenset(
            part.strip().zfill(2)
            for part in chapters_text.replace(",", ";").split(";")
            if part.strip()
        )
        if chapters:
            out[scope] = chapters
    _DOMAIN_SCOPE_ROUTE_CHAPTERS = out
    return out


def classifier_bucket_chapters() -> dict[str, frozenset[str]]:
    """Effective bucket_key -> chapters for CN candidate retrieval (cached).

    = 9 WCO groups (full 01-97 coverage) + 2 narrow demo sub-buckets.
    """
    global _CLASSIFIER_BUCKET_CHAPTERS
    if _CLASSIFIER_BUCKET_CHAPTERS is not None:
        return _CLASSIFIER_BUCKET_CHAPTERS
    _CLASSIFIER_BUCKET_CHAPTERS = {**WCO_GROUP_CHAPTERS, **DEMO_BUCKET_CHAPTERS}
    return _CLASSIFIER_BUCKET_CHAPTERS


def known_classifier_scopes() -> set[str]:
    """Bucket keys the CN retriever can build candidates for."""
    return set(classifier_bucket_chapters().keys())


def bucket_for_chapter(chapter: str) -> str | None:
    """Deterministic chapter -> bucket key (demo sub-bucket wins, else WCO group)."""
    ch = str(chapter or "").strip().zfill(2)
    if not ch or ch == "00":
        return None
    if ch in _CHAPTER_TO_DEMO_BUCKET:
        return _CHAPTER_TO_DEMO_BUCKET[ch]
    for group_key, chapters in WCO_GROUP_CHAPTERS.items():
        if ch in chapters:
            return group_key
    return None


def buckets_for_chapters(chapters) -> list[str]:
    """Map DomainRouter candidate chapters to classifier bucket keys (ordered)."""
    out: list[str] = []
    for ch in chapters or []:
        bucket = bucket_for_chapter(ch)
        if bucket and bucket not in out:
            out.append(bucket)
    return out


def normalize_router_scope(router_scope: str) -> str | None:
    """Legacy: map a regulatory domain_scope name to a demo bucket key, or None.

    Kept as a fallback for callers/routes that still pass regulatory scope names
    instead of chapters. Only the demo scopes resolve; broader regulatory names
    have no 1:1 classifier bucket under the WCO scheme.
    """
    key = str(router_scope or "").strip().lower()
    if key in DEMO_BUCKET_CHAPTERS:
        return key
    if key == "food":
        return "food_16_21"
    return None


def classifier_fallback_scopes() -> list[str]:
    """Demo buckets to fall back on when routing yields no usable chapter."""
    return [s for s in DEMO_BUCKET_CHAPTERS]


class SupabaseCnCandidateRetriever(CnCandidateRetriever):
    """CnCandidateRetriever backed by Supabase cn_table, not local CSV files.

    Buckets are built only for the requested domain scopes (lazy, per-request),
    so enabling the 9 semantic groups never loads the whole tariff — each run
    queries cn_table for just the chapters of its active scopes.
    """

    def __init__(
        self,
        ontologyRootPath,
        projectRootPath=None,
        *,
        requestedScopes: Sequence[str] | None = None,
    ) -> None:
        super().__init__(ontologyRootPath, projectRootPath)
        self._requestedScopes = [
            str(s).strip() for s in (requestedScopes or []) if str(s).strip()
        ]

    def _LoadRowsByDomainScope(self) -> dict[str, list[dict[str, str]]]:
        if self._rowsByDomainScope is not None:
            return self._rowsByDomainScope
        if _connect_db is None or _release_db is None:
            raise RuntimeError("Supabase DB connector is not available.")

        bucket_chapters = classifier_bucket_chapters()
        scopes = [s for s in self._requestedScopes if s in bucket_chapters]
        if not scopes:
            scopes = classifier_fallback_scopes()

        wanted_chapters: set[str] = set()
        for scope in scopes:
            wanted_chapters |= set(bucket_chapters[scope])

        rows_by_domain_scope: dict[str, list[dict[str, str]]] = {s: [] for s in scopes}
        if not wanted_chapters:
            self._rowsByDomainScope = rows_by_domain_scope
            return rows_by_domain_scope

        conn = _connect_db()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM cn_table WHERE chapter = ANY(%s)",
                (sorted(wanted_chapters),),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            cur.close()
        finally:
            _release_db(conn)

        for row in rows:
            normalized = {str(k): "" if v is None else str(v) for k, v in row.items()}
            chapter = (normalized.get("chapter", "") or normalized.get("hs2_code", "")).zfill(2)
            for scope in scopes:
                if chapter in bucket_chapters[scope]:
                    rows_by_domain_scope[scope].append(normalized)

        self._rowsByDomainScope = rows_by_domain_scope
        return rows_by_domain_scope


# ---------------------------------------------------------------------------
# Return container
# ---------------------------------------------------------------------------
@dataclass
class ExternalClassificationResult:
    candidates: list = field(default_factory=list)
    recommendation: Any = None
    validation_report: Any = None
    decision_report: Any = None
    traversal_report: Any = None
    llm_response_text: str = ""
    llm_model: str = ""
    prompt_text: str = ""
    citations: list[dict] = field(default_factory=list)
    semantic_retrieval_status: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# RuntimeAdapter (bridge) — env / .appconfig backed
# ---------------------------------------------------------------------------
def build_runtime_adapter():
    """Build a RuntimeAdapter from ASAP/.env or fallback to default config."""
    try:
        env_path = ASAP_ENV_FILE if ASAP_ENV_FILE.exists() else None
        runtimeConfig = BuildLlmRuntimeConfigFromEnv(envFilePath=env_path)
    except Exception:
        runtimeConfig = BuildDefaultLlmRuntimeConfig()
    dependencyStatus = ProbeRuntimeDependency(runtimeConfig)
    return BuildRuntimeAdapter(runtimeConfig, dependencyStatus)


def build_semantic_candidate_index(
    retriever: CnCandidateRetriever,
) -> tuple[CnSemanticCandidateIndex | None, dict[str, Any]]:
    global SEMANTIC_CANDIDATE_INDEX
    global SEMANTIC_CANDIDATE_INDEX_STATUS

    if SEMANTIC_CANDIDATE_INDEX is not None:
        return SEMANTIC_CANDIDATE_INDEX, {
            "status": "ready",
            "chunk_count": SEMANTIC_CANDIDATE_INDEX.chunkCount,
        }
    if SEMANTIC_CANDIDATE_INDEX_STATUS is not None:
        return None, dict(SEMANTIC_CANDIDATE_INDEX_STATUS)

    if not APP_CONFIG.classification.use_semantic_candidate_retrieval:
        SEMANTIC_CANDIDATE_INDEX_STATUS = {
            "status": "disabled",
            "reason": "semantic candidate retrieval is disabled by appconfig",
        }
        return None, dict(SEMANTIC_CANDIDATE_INDEX_STATUS)

    runtimeConfig = BuildTextEmbeddingRuntimeConfig(APP_CONFIG.embedding)
    if not runtimeConfig.enabled:
        SEMANTIC_CANDIDATE_INDEX_STATUS = {
            "status": "disabled",
            "reason": "embedding runtime is disabled by appconfig",
            "provider": runtimeConfig.provider.value,
            "model": runtimeConfig.modelName,
        }
        return None, dict(SEMANTIC_CANDIDATE_INDEX_STATUS)

    dependencyStatus = ProbeTextEmbeddingDependency(runtimeConfig)
    if not dependencyStatus.isAvailable:
        SEMANTIC_CANDIDATE_INDEX_STATUS = {
            "status": "unavailable",
            "reason": dependencyStatus.message,
            "provider": dependencyStatus.provider.value,
            "model": runtimeConfig.modelName,
            "limitations": list(dependencyStatus.limitations),
        }
        return None, dict(SEMANTIC_CANDIDATE_INDEX_STATUS)

    try:
        embeddingAdapter = BuildTextEmbeddingAdapter(
            runtimeConfig,
            dependencyStatus=dependencyStatus,
        )
        if embeddingAdapter is None:
            SEMANTIC_CANDIDATE_INDEX_STATUS = {
                "status": "disabled",
                "reason": "embedding adapter was not created",
                "provider": runtimeConfig.provider.value,
                "model": runtimeConfig.modelName,
            }
            return None, dict(SEMANTIC_CANDIDATE_INDEX_STATUS)

        semanticIndex = CnSemanticCandidateIndex(embeddingAdapter)
        semanticIndex.Build(retriever.LoadRowsByDomainScope())
    except (
        TextEmbeddingAdapterBuildError,
        TextEmbeddingGenerationError,
        ValueError,
    ) as exception:
        SEMANTIC_CANDIDATE_INDEX_STATUS = {
            "status": "failed",
            "reason": str(exception),
            "provider": runtimeConfig.provider.value,
            "model": runtimeConfig.modelName,
        }
        return None, dict(SEMANTIC_CANDIDATE_INDEX_STATUS)

    SEMANTIC_CANDIDATE_INDEX = semanticIndex
    return SEMANTIC_CANDIDATE_INDEX, {
        "status": "ready",
        "provider": runtimeConfig.provider.value,
        "model": runtimeConfig.modelName,
        "chunk_count": semanticIndex.chunkCount,
    }


# ---------------------------------------------------------------------------
# PES → ProductClassificationInput
# ---------------------------------------------------------------------------
def pes_to_input(pes: dict, *, domain_scope: str = "food_16_21") -> ProductClassificationInput:
    """Map our Blackboard ProductEvidenceState to ASAPExpress input."""
    obs = pes.get("observed_facts") or {}

    ocr_chunks = obs.get("ocr_text") or []
    if isinstance(ocr_chunks, list):
        ocr_text = "\n".join(str(t) for t in ocr_chunks if t)
    else:
        ocr_text = str(ocr_chunks)
    ocr_text = _clean_classifier_ocr_text(ocr_text)
    fact_texts = []
    for key in ("composition", "classification_input_fact_texts"):
        value = obs.get(key) or []
        if isinstance(value, list):
            fact_texts.extend(value)
        else:
            fact_texts.append(str(value))
    composition = _clean_classifier_fact_texts(fact_texts)
    domain_scopes = obs.get("domain_scopes") or []
    if not isinstance(domain_scopes, list):
        domain_scopes = [str(domain_scopes)]
    domain_scopes = [str(s) for s in domain_scopes if str(s).strip()] or [domain_scope]

    structured_facts = obs.get("classification_input_product_facts") or []
    if not isinstance(structured_facts, list):
        structured_facts = [structured_facts]
    structured_facts = [x for x in structured_facts if isinstance(x, dict)]

    return ProductClassificationInput(
        productName=obs.get("product_name") or "",
        shortDescription=obs.get("description") or "",
        productDomain=domain_scope,
        domainScopes=domain_scopes,
        normalizedOcrFactTexts=composition,
        structuredProductFacts=structured_facts,
        unresolvedProductFacts=obs.get("unresolved_product_facts") or [],
        productFactConflicts=obs.get("product_fact_conflicts") or [],
        ocrText=ocr_text,
    )


# ---------------------------------------------------------------------------
# Main entry — 7-step orchestration
# ---------------------------------------------------------------------------
def run_external_classifier(
    pes: dict,
    *,
    domain_scope: str = "food_16_21",
    runtime_adapter=None,
    top_k_candidates: int = 8,
) -> ExternalClassificationResult:
    productInput = pes_to_input(pes, domain_scope=domain_scope)

    # 2. Retrieval — build CN buckets only for the scopes this product routed
    # to (9 semantic groups, lazily). NB: build_semantic_candidate_index caches
    # a single global index; it is currently disabled (heuristic-only), so
    # per-request scoping is safe. If embeddings are re-enabled, that global
    # index must be made scope-aware to match this per-request retriever.
    retriever = SupabaseCnCandidateRetriever(
        ASAP_ONTOLOGY_ROOT,
        ASAP_PROJECT_ROOT,
        requestedScopes=productInput.domainScopes,
    )
    semanticIndex, semanticStatus = build_semantic_candidate_index(retriever)
    if semanticIndex is None:
        candidates = retriever.FindCandidates(productInput, topK=top_k_candidates)
    else:
        candidates = retriever.FindCandidatesWithSemanticIndex(
            productInput,
            semanticIndex,
            heuristicTopK=top_k_candidates,
            semanticTopK=APP_CONFIG.classification.semantic_candidate_top_k,
            finalCandidateLimit=(
                APP_CONFIG.classification.hybrid_candidate_limit
                or top_k_candidates
            ),
            minSemanticScore=APP_CONFIG.classification.semantic_min_score,
        )
    if not candidates:
        return ExternalClassificationResult(
            candidates=[],
            semantic_retrieval_status=semanticStatus,
            error="no_candidates_from_retriever",
        )

    # Citations from candidates (for cite() in caller)
    citations: list[dict] = []
    for c in candidates:
        citations.append({
            "source_table": "cn_table",
            "source_id": c.hs8,
            "snippet": (getattr(c, "hs8Description", "") or "")[:120],
            "reason": (
                "Stage 1 shortlist via ASAPExpress CnCandidateRetriever "
                "with retrieval sources: {0}.".format(
                    ", ".join(getattr(c, "retrievalSources", []) or ["heuristic"])
                )
            ),
        })

    # 3. Ontology context
    contextBuilder = OntologyContextBuilder(ASAP_ONTOLOGY_ROOT)
    query = productInput.BuildSearchText()
    packagedContext = contextBuilder.BuildContext(query, topK=8)

    # 4. Evidence package
    evidenceBuilder = Stage1EvidencePackageBuilder(
        ASAP_ONTOLOGY_ROOT, ASAP_PROJECT_ROOT,
    )
    evidencePackage = evidenceBuilder.Build(productInput, candidates, packagedContext)

    # 5. LLM request — clamp maxTokens so prompt + max_tokens fits the
    # context window of gemma4:26b (4096). Stage1 default of 4096 alone
    # overflows the model context and causes ollama to abort mid-stream.
    requestBuilder = Stage1RequestBuilder()
    request = requestBuilder.BuildRequest(
        productInput, candidates, packagedContext,
        evidencePackage=evidencePackage,
    )
    # ASAPExpress refactored LlmRequest / LlmGenerationOptions from
    # @dataclass(frozen=True) to pydantic BaseModel — use model_copy(update=)
    # for both shapes via a small dispatch.
    request = _copy_with_clamped_max_tokens(request, LLM_MAX_TOKENS)
    request = _harden_stage1_request(request, productInput, candidates)
    request = _build_compact_decision_request(request, productInput, candidates)
    request = _copy_with_clamped_max_tokens(request, LLM_DECISION_MAX_TOKENS)
    prompt_text = (request.systemPrompt or "") + "\n---\n" + (request.userPrompt or "")

    # 6. LLM call
    try:
        adapter = runtime_adapter or build_runtime_adapter()
        response = adapter.Generate(request)
    except Exception as e:  # noqa: BLE001
        return ExternalClassificationResult(
            candidates=list(candidates),
            citations=citations,
            prompt_text=prompt_text[:2000],
            semantic_retrieval_status=semanticStatus,
            error=f"llm_error: {e}",
        )

    response_text = getattr(response, "generatedText", "") or ""
    compact_decision = _extract_compact_decision(response_text, candidates)
    if compact_decision is not None:
        compact_decision = _apply_domain_selection_guard(
            compact_decision,
            productInput,
            candidates,
        )
        expanded_response_text = _expand_compact_decision_to_stage1_json(
            compact_decision,
            productInput,
            candidates,
        )
        response = _copy_response_with_text(response, expanded_response_text)
        response_text = expanded_response_text
    model_name = (
        getattr(response, "modelName", None)
        or getattr(response, "model", None)
        or ""
    )

    # 7. Validator → Decision → Traversal → Recommendation
    validator = Stage1ResponseValidator()
    validationReport = validator.ValidateResponse(
        response, productInput, candidates, evidencePackage=evidencePackage,
    )

    # Local gemma-class models sometimes return schema descriptions or an
    # OpenAPI-like envelope even with JSON mode enabled. Use the validator
    # errors as a one-shot repair prompt before the decision policy sees it.
    if not validationReport.isValid:
        try:
            repair_request = _copy_with_clamped_max_tokens(
                _build_repair_request(
                    request,
                    productInput,
                    candidates,
                    response_text,
                    validationReport,
                ),
                LLM_MAX_TOKENS,
            )
            repair_response = adapter.Generate(repair_request)
            repair_validation = validator.ValidateResponse(
                repair_response,
                productInput,
                candidates,
                evidencePackage=evidencePackage,
            )
            if repair_validation.isValid:
                response = repair_response
                response_text = getattr(response, "generatedText", "") or ""
                validationReport = repair_validation
                model_name = (
                    getattr(response, "modelName", None)
                    or getattr(response, "model", None)
                    or model_name
                    or ""
                )
        except Exception:
            pass

    decisionPolicy = Stage1DecisionPolicy()
    decisionReport = decisionPolicy.BuildDecision(validationReport, candidates)
    traversalController = Stage1TraversalController(decisionPolicy=decisionPolicy)
    traversalReport = traversalController.BuildFromDecision(decisionReport, candidates)
    recommendationBuilder = Stage1RecommendationReportBuilder()
    recommendation = recommendationBuilder.Build(
        productInput, candidates, validationReport, decisionReport,
        traversalReport, evidencePackage=evidencePackage,
    )

    return ExternalClassificationResult(
        candidates=list(candidates),
        recommendation=recommendation,
        validation_report=validationReport,
        decision_report=decisionReport,
        traversal_report=traversalReport,
        llm_response_text=response_text,
        llm_model=str(model_name),
        prompt_text=prompt_text[:2000],
        citations=citations,
        semantic_retrieval_status=semanticStatus,
    )
