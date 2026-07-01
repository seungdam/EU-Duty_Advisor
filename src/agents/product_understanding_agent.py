"""
Product_Understanding_Agent — evidence -> routing/classification facts.

This stage turns ProductEvidenceState into a compact read model for
DomainRouterTool and Classification_Agent. It keeps raw OCR/text separate from
inferred product facts, and uses a best-effort ontology-guided LLM pass to
produce tariff-routing terms before falling back to deterministic signals.
"""
from __future__ import annotations

import json
import re
from typing import Any

import os
from pathlib import Path

from agents import dto
from agents.agent_base import BaseAgent
from agents.blackboard import BlackboardStore, now_iso

# B-2: a lightweight, non-thinking model translates the cleaned Korean product
# facts into ONE English description phrased in HS/CN tariff nomenclature so it
# matches the English cn_table descriptions the retriever scores against. The
# heavy classification model (gemma4-ctx) is left untouched; translation is a
# separate cheap step (~1-2s on gemma3:4b).
_TRANSLATION_MODEL = os.environ.get(
    "ASAP_PRODUCT_UNDERSTANDING_MODEL",
    os.environ.get("ASAP_TRANSLATION_MODEL", "gemma3:4b"),
)
_ENABLE_PRODUCT_UNDERSTANDING_LLM = (
    os.environ.get("ASAP_ENABLE_PRODUCT_UNDERSTANDING_LLM", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
_ENABLE_ENCYCLOPEDIA_LOOKUP = (
    os.environ.get("ASAP_ENABLE_ENCYCLOPEDIA_LOOKUP", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
_TRANSLATION_SYSTEM_PROMPT = (
    "You are a customs tariff classification assistant. Convert a Korean "
    "food/cosmetic product into ONE concise English sentence phrased in HS/CN "
    "tariff nomenclature vocabulary. Use tariff terms where they apply, e.g. "
    "'prepared or preserved', 'not stuffed', 'cooked/uncooked', 'frozen', "
    "'dried', 'in airtight containers', 'containing ... by weight', the physical "
    "form, the processing/preparation state, and the single ingredient that "
    "gives the product its essential character. Output only that English "
    "description. No HS/CN codes, no commentary, no Korean."
)
_translation_adapter_cache: list = []
_ontology_context_cache: list[str] = []
_last_product_understanding_trace: dict[str, Any] = {}

_ONTOLOGY_CONTEXT_FILES = [
    "stage_contract/Product_Understanding.md",
    "stage_contract/Regulatory_Domain_Routing.md",
    "profiles/ProductUnderstandingTool_Profile.md",
    "profiles/DomainRouterTool_Profile.md",
    "tables/CN_Chapter_Index.md",
]

_PRODUCT_UNDERSTANDING_SYSTEM_PROMPT = """
Return only one JSON object. No markdown, no code fence.
You are ProductUnderstandingTool. Convert product evidence into chapter-routing
facts, not HS/CN/TARIC codes.

Allowed processing_state:
processed_or_prepared, raw_or_fresh, not_food_processing, unknown.
Allowed domain_hints:
food, cosmetics, pharmaceutical, hazardous, animal_origin, other.

Rules:
- Do not output HS/CN/TARIC or documents.
- Allergen/cross-contact text is excluded routing evidence.
- Country, manufacturer, expiry, package material, seller, customer-center text
  is admin label evidence, not product form.
- "제조국" is not soup/stew evidence.
- For cosmetics/non-food, never force food words like frozen/prepared food.
- Translate the Korean product name into an English commercial/tariff identity.
- Use English tariff-like product form terms: pastry/bakery product, prepared
  rice meal, noodle/pasta preparation, sauce/condiment, cosmetic deodorant.
- chapter_routing_terms are the only high-confidence terms DomainRouter should
  score strongly against cn_chapter_index. Put concise terms there, not full
  sentences.
- Use "rice meal" only when evidence contains 밥/솥밥/rice meal.
- For 만두/dumpling, use "stuffed pasta" and "cereal preparation"; do not treat
  it as meat/fish preparation unless a meat/fish percentage is decisive.
- For pastry/약과/bakery/cake/noodle/pasta/rice meal, include "cereal preparation"
  or "bakery product" in routing_keywords.
- For 무침/반찬/side dish/salad/seasoned tofu, use "miscellaneous edible preparation"
  and "side dish"; do not call it rice meal.
- For 생물/live/fresh fish/eel/seafood, processing_state is raw_or_fresh. 손질,
  cleaned, handled, cut, packed, or portioned is not cooked/prepared by itself.

JSON keys:
translated_product_name, commercial_identity, normalized_tariff_description,
principal_ingredient_terms, ingredient_taxonomy_terms, composition_terms,
product_form_terms, processing_terms, processing_state, use_context_terms,
domain_hints, chapter_routing_terms, routing_keywords, blocked_routing_terms,
excluded_from_routing_terms, confidence, needs_review.
""".strip()

_PRODUCT_UNDERSTANDING_ONTOLOGY_SUMMARY = """
Ontology routing summary:
- ProductUnderstandingFacts feeds DomainRouter, not final classification.
- DomainRouter matches routing_keywords/product_form_terms/processing_state
  against cn_chapter_index include/exclude/guardrail columns.
- Prepared foods must not route only by raw ingredient/allergen mentions.
- Baseline/pre-TARIC document lookup uses RoutingContext after chapter routing.
""".strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _set_product_understanding_trace(payload: dict[str, Any]) -> None:
    _last_product_understanding_trace.clear()
    _last_product_understanding_trace.update(payload)


def _product_understanding_trace() -> dict[str, Any]:
    return dict(_last_product_understanding_trace)


def _translation_adapter():
    """Build (once) an Ollama RuntimeAdapter pinned to the translation model."""
    if _translation_adapter_cache:
        return _translation_adapter_cache[0]
    from bussiness_logic.bridge import (
        BuildDefaultLlmRuntimeConfig,
        BuildLlmRuntimeConfigFromEnv,
        BuildRuntimeAdapter,
        ProbeRuntimeDependency,
    )

    env_path = Path(
        os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[2])
    ) / ".env"
    try:
        config = BuildLlmRuntimeConfigFromEnv(envFilePath=env_path)
    except Exception:  # noqa: BLE001 - fall back to defaults if .env missing
        config = BuildDefaultLlmRuntimeConfig()
    config = config.model_copy(update={"modelName": _TRANSLATION_MODEL})
    adapter = BuildRuntimeAdapter(config, ProbeRuntimeDependency(config))
    _translation_adapter_cache.append(adapter)
    return adapter


def _project_root() -> Path:
    return Path(os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()


def _load_ontology_context() -> str:
    """Small LLM-readable contract pack for ProductUnderstanding.

    The docs are source-of-truth guidance, not runtime data. Keep this compact
    so local Ollama calls stay usable.
    """
    if _ontology_context_cache:
        return _ontology_context_cache[0]
    root = _project_root()
    ontology_root = root / os.environ.get("ASAP_ONTOLOGY_ROOT", "data/ASAP_Ontology")
    chunks: list[str] = []
    for rel in _ONTOLOGY_CONTEXT_FILES:
        path = ontology_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Frontmatter and long table docs can swamp small local models. Keep a
        # short excerpt only; the system prompt carries the active guardrails.
        text = text[:280]
        chunks.append(f"## {rel}\n{text}")
    context = (_PRODUCT_UNDERSTANDING_ONTOLOGY_SUMMARY + "\n\n" + "\n\n".join(chunks))[:2500]
    _ontology_context_cache.append(context)
    return context


def _list_strings(value: Any, *, limit: int = 30) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            item = item.get("term") or item.get("value") or item.get("text") or ""
        text = re.sub(r"\s+", " ", str(item or "").strip())
        if text:
            out.append(text)
    return _dedupe(out, limit=limit)


def _normalize_domain_hint(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text:
        return ""
    if any(x in text for x in ("cosmetic", "deodorant", "skincare", "toilet preparation", "화장")):
        return "cosmetics"
    if any(x in text for x in ("food", "edible", "pastry", "snack", "meal", "식품", "음식")):
        return "food"
    if any(x in text for x in ("pharma", "medicine", "drug", "의약")):
        return "pharmaceutical"
    if any(x in text for x in ("hazard", "chemical", "위험", "화학")):
        return "hazardous"
    if any(x in text for x in ("animal", "meat", "fish", "dairy", "동물", "축산", "수산")):
        return "animal_origin"
    if "other" in text:
        return "other"
    return ""


def _normalize_processing_state(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text:
        return ""
    if text in {"processed_or_prepared", "raw_or_fresh", "not_food_processing", "unknown"}:
        return text
    if any(x in text for x in ("prepared", "processed", "cooked", "baked", "fried", "roasted", "seasoned")):
        return "processed_or_prepared"
    if any(x in text for x in ("cosmetic", "non-food", "non food", "toilet preparation")):
        return "not_food_processing"
    if any(x in text for x in ("raw", "fresh", "live")):
        return "raw_or_fresh"
    return ""


def _has_rice_meal_evidence(text: str) -> bool:
    return re.search(
        r"쌀|쌀국수|쌀면|밥|솥밥|비빔밥|볶음밥|죽|누룽지|떡|"
        r"\brice\b|\brice meal\b|\brice dish\b|\brice noodle\b|"
        r"\brice porridge\b|\brice cake\b",
        text,
        flags=re.I,
    ) is not None


def _has_bakery_evidence(text: str) -> bool:
    return re.search(r"빵|케이크|과자|비스킷|쿠키|약과|pastry|bakery|bread|cake|biscuit|cookie", text, flags=re.I) is not None


def _has_mollusc_evidence(text: str) -> bool:
    return re.search(r"낙지|주꾸미|쭈꾸미|오징어|문어|octopus|squid|cuttlefish|mollusc", text, flags=re.I) is not None


def _has_term_evidence(text: str, pattern: str) -> bool:
    return re.search(pattern, text or "", flags=re.I) is not None


def _list_without_unsupported_forms(items: list[str], evidence_text: str) -> list[str]:
    out: list[str] = []
    has_rice = _has_rice_meal_evidence(evidence_text)
    has_bakery = _has_bakery_evidence(evidence_text)
    for item in items:
        norm = item.lower()
        if re.match(r"^chapter\s+\d+", norm) or re.match(r"^\d{1,2}\s*[-/]\s*\d{1,2}$", norm):
            continue
        if re.match(r"^\d{4}(?:[.]\d+)?$", norm) or re.match(r"^\d{6,10}$", norm):
            continue
        if not has_rice and (
            "rice meal" in norm
            or "rice dish" in norm
            or "rice porridge" in norm
            or "rice noodle" in norm
            or norm == "rice"
        ):
            continue
        if not has_bakery and ("bakery product" in norm or "pastry" in norm):
            continue
        if "mung bean" in norm and not _has_term_evidence(evidence_text, r"녹두|mung\s*bean"):
            continue
        if "sunflower" in norm and not _has_term_evidence(evidence_text, r"해바라기|sunflower"):
            continue
        if norm == "starch" and not _has_term_evidence(evidence_text, r"전분|starch"):
            continue
        out.append(item)
    return out


def _text_without_unsupported_forms(text: str, evidence_text: str) -> str:
    cleaned = str(text or "")
    if not _has_rice_meal_evidence(evidence_text):
        cleaned = re.sub(r"\bprepared rice noodle product\b", "prepared noodle product", cleaned, flags=re.I)
        cleaned = re.sub(r"\brice noodle product\b", "noodle product", cleaned, flags=re.I)
        cleaned = re.sub(r"\bprepared rice meal\b,?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\brice meal\b,?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\bprepared rice dish\b,?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\brice dish\b,?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\binstant rice porridge\b,?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\brice porridge\b,?\s*", "", cleaned, flags=re.I)
    if not _has_bakery_evidence(evidence_text):
        cleaned = re.sub(r"\bbakery product\b,?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\bpastry product\b,?\s*", "", cleaned, flags=re.I)
    if not _has_term_evidence(evidence_text, r"녹두|mung\s*bean"):
        cleaned = re.sub(r"\bmung bean paste\b,?\s*", "", cleaned, flags=re.I)
    if not _has_term_evidence(evidence_text, r"해바라기|sunflower"):
        cleaned = re.sub(r"\bsunflower seed oil\b,?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bchapter\s+\d+\b,?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d{1,2}\s*[-/]\s*\d{1,2}\b,?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d{4}(?:[.]\d+)?\b,?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d{6,10}\b,?\s*", "", cleaned, flags=re.I)
    return re.sub(r"\s*,\s*,+", ", ", cleaned).strip(" ,")


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _compact_evidence_for_llm(
    *,
    product_name: str,
    description: str,
    fact_texts: list[str],
    allergen_notice_texts: list[str],
) -> str:
    facts = "\n".join(f"- {t[:350]}" for t in fact_texts[:10] if t.strip())
    allergens = "\n".join(f"- {t[:250]}" for t in allergen_notice_texts[:5] if t.strip())
    return (
        f"product_name: {product_name}\n"
        f"description: {description}\n\n"
        f"classification_relevant_evidence:\n{facts or '-'}\n\n"
        f"allergen_or_cross_contact_evidence_do_not_route_by_this:\n{allergens or '-'}"
    )


def build_product_understanding_dto(
    *,
    product_name: str,
    description: str,
    fact_texts: list[str],
    allergen_notice_texts: list[str],
) -> tuple[dict[str, Any], str]:
    """Return (dto, error). Empty dto means fallback should be used."""
    if not _ENABLE_PRODUCT_UNDERSTANDING_LLM:
        _set_product_understanding_trace({
            "enabled": False,
            "model": _TRANSLATION_MODEL,
            "error": "disabled_by_env",
        })
        return {}, "disabled_by_env"
    from bussiness_logic.bridge.schema import (
        LlmGenerationOptions,
        LlmRequest,
        LlmResponseFormat,
    )

    user_prompt = _compact_evidence_for_llm(
        product_name=product_name,
        description=description,
        fact_texts=fact_texts,
        allergen_notice_texts=allergen_notice_texts,
    )
    context_chunks = [_load_ontology_context()]
    max_tokens = _env_int("ASAP_PRODUCT_UNDERSTANDING_MAX_TOKENS", 1600)
    _set_product_understanding_trace({
        "enabled": True,
        "model": _TRANSLATION_MODEL,
        "max_tokens": max_tokens,
        "system_prompt_source": "src/agents/product_understanding_agent.py:_PRODUCT_UNDERSTANDING_SYSTEM_PROMPT",
        "ontology_context_files": list(_ONTOLOGY_CONTEXT_FILES),
        "user_prompt": user_prompt,
        "user_prompt_length": len(user_prompt),
        "context_chunk_lengths": [len(chunk) for chunk in context_chunks],
        "parse_ok": False,
        "fallback_reason": "",
    })
    request = LlmRequest(
        user_prompt=user_prompt,
        system_prompt=_PRODUCT_UNDERSTANDING_SYSTEM_PROMPT,
        context_chunks=context_chunks,
        # Some local Ollama models emit only "{" in provider JSON mode. Ask for
        # JSON in the prompt but request plain text, then parse ourselves.
        response_format=LlmResponseFormat.TEXT,
        generation_options=LlmGenerationOptions(temperature=0.0, max_tokens=max_tokens),
    )
    try:
        response = _translation_adapter().Generate(request)
        generated = getattr(response, "generatedText", "") or ""
        dto = _extract_json_object(generated)
        trace = _product_understanding_trace()
        trace.update({
            "generated_text": generated,
            "generated_text_length": len(generated),
            "parse_ok": bool(dto),
            "parsed_keys": sorted(dto.keys()) if isinstance(dto, dict) else [],
        })
        if not dto:
            preview = re.sub(r"\s+", " ", generated.strip())[:300]
            trace["fallback_reason"] = f"empty_or_invalid_json: {preview}"
            _set_product_understanding_trace(trace)
            return {}, f"empty_or_invalid_json: {preview}"
        _set_product_understanding_trace(trace)
        return dto, ""
    except Exception as exc:  # noqa: BLE001 - ProductUnderstanding degrades to rules
        trace = _product_understanding_trace()
        trace.update({
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "fallback_reason": f"{type(exc).__name__}: {exc}",
        })
        _set_product_understanding_trace(trace)
        return {}, f"{type(exc).__name__}: {exc}"


def translate_to_tariff_english(product_name: str, fact_texts: list[str]) -> str:
    """Korean product facts -> one tariff-nomenclature English sentence.

    Returns "" on any failure so the pipeline degrades to the Korean text
    rather than breaking.
    """
    facts = "; ".join(t for t in fact_texts[:20] if t.strip())
    if not (product_name.strip() or facts.strip()):
        return ""
    from bussiness_logic.bridge.schema import (
        LlmGenerationOptions,
        LlmRequest,
        LlmResponseFormat,
    )

    request = LlmRequest(
        user_prompt=(
            f"Korean product: {product_name}\n"
            f"Facts/ingredients: {facts}\n"
            "Tariff-style English description:"
        ),
        system_prompt=_TRANSLATION_SYSTEM_PROMPT,
        response_format=LlmResponseFormat.TEXT,
        generation_options=LlmGenerationOptions(temperature=0.0, max_tokens=200),
    )
    try:
        response = _translation_adapter().Generate(request)
        return (getattr(response, "generatedText", "") or "").strip()
    except Exception:  # noqa: BLE001 - translation is best-effort
        return ""


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_/-]{2,}|[가-힣]{2,}|\d+(?:[.,]\d+)?\s*%?")

PROCESSED_SIGNAL_PATTERNS: list[tuple[str, str]] = [
    ("prepared", r"조제|가공|prepared|processed"),
    ("cooked", r"조리|익힌|cooked|boiled|steamed"),
    ("fried", r"튀김|볶음|유탕|fried|stir[- ]?fried|roasted"),
    ("seasoned", r"양념|소스|시즈닝|seasoned|sauce|marinated"),
    ("instant", r"즉석|인스턴트|라면|ramen|instant"),
    ("soup_or_stew", r"(?<!중)국|탕|찌개|스프|soup|stew|broth"),
    ("frozen_prepared", r"냉동.*(조제|가공|볶음|튀김)|frozen.*(prepared|cooked)"),
]

RAW_SIGNAL_PATTERNS: list[tuple[str, str]] = [
    ("raw", r"생물|생고기|생선|raw"),
    ("live", r"활어|산|live"),
    ("dried_raw", r"건조|말린|dried"),
]

DOMAIN_HINT_PATTERNS: list[tuple[str, str]] = [
    ("food", r"식품|라면|유탕면|면류|소스|미역국|국물|탕|찌개|스프|만두|주꾸미|김치|food|edible|noodle|sauce|soup|stew|broth"),
    # Do not treat bare "cream/크림" as a cosmetics signal. Food products such
    # as cream soup/sauce otherwise route to chapter 33.
    ("cosmetics", r"화장품|데오드란트|데오도란트|롤온|로션|향수|샴푸|스킨케어|토너|에센스|cosmetic|skincare|deodorant|antiperspirant|roll[- ]?on|lotion|perfume|shampoo|toner|essence|(?:skin|face|body|moisturizing)\s+cream"),
    ("pharmaceutical", r"의약품|약품|백신|medicine|pharmaceutical|tablet|capsule|vaccine"),
]

ALLERGEN_NOTICE_RE = re.compile(
    r"알레르|알러지|알레르겐|allergen|allergy|may contain|trace allergen|"
    r"같은\s*제조시설|동일\s*제조시설|교차\s*오염|혼입|cross[- ]?contact",
    re.I,
)
ALLERGEN_FOOD_TERMS = {
    "우유", "대두", "밀", "메밀", "땅콩", "호두", "잣", "계란", "난류",
    "돼지고기", "닭고기", "쇠고기", "소고기", "고등어", "게", "새우",
    "오징어", "조개류", "굴", "전복", "홍합", "복숭아", "토마토", "아황산",
    "milk", "soy", "soybean", "wheat", "buckwheat", "peanut", "walnut",
    "egg", "pork", "chicken", "beef", "mackerel", "crab", "shrimp",
    "squid", "shellfish", "oyster", "abalone", "mussel", "peach", "tomato",
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _stringify_fact(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts: list[str] = []
        for key in (
            "field_name",
            "field",
            "name",
            "fact_key",
            "label",
            "field_value",
            "value",
            "text",
            "fact_text",
        ):
            val = value.get(key)
            if val is not None:
                parts.append(str(val))
        return " ".join(p for p in parts if p).strip()
    return str(value).strip()


def _dedupe(items: list[str], *, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        normalized = re.sub(r"\s+", " ", str(item or "").strip())
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
        if limit and len(out) >= limit:
            break
    return out


def _extract_terms(text: str) -> list[str]:
    stop = {
        "상품설명", "상품이미지", "참조", "컬리", "고객", "센터", "보관", "주의",
        "kcal", "mg", "g", "and", "the", "with", "for", "from",
    }
    terms = []
    for match in TOKEN_RE.finditer(text or ""):
        term = match.group(0).strip()
        if not term or term.lower() in stop:
            continue
        if term.isdigit():
            continue
        terms.append(term)
    return _dedupe(terms, limit=160)


def _clean_encyclopedia_query(value: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", str(value or ""))
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:g|kg|ml|l|개입|팩|종|인분)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:택\s*1|택1|냉동|냉장|상온|간편|프리미엄)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip(" -_/|")


def _encyclopedia_query_candidates(*values: str) -> list[str]:
    candidates: list[str] = []
    for raw in values:
        cleaned = _clean_encyclopedia_query(raw)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
        compact = re.sub(r"\s+", "", cleaned)
        # Keep only product-level head nouns. Do not fall back to generic
        # preparation suffixes such as "볶음" or "구이"; those are dictionary
        # words but not commodity identities and they pollute routing.
        if re.search(r"[가-힣]", compact) and 2 <= len(compact) <= 16 and compact not in candidates:
            candidates.append(compact)
    return candidates[:8]


def _lookup_encyclopedia_identity(queries: list[str]) -> dict[str, Any]:
    if not _ENABLE_ENCYCLOPEDIA_LOOKUP or not queries:
        return {
            "enabled": _ENABLE_ENCYCLOPEDIA_LOOKUP,
            "configured": False,
            "found": False,
            "usable_for_routing": False,
            "quality_status": "disabled" if not _ENABLE_ENCYCLOPEDIA_LOOKUP else "no_query",
            "quality_reasons": ["disabled"] if not _ENABLE_ENCYCLOPEDIA_LOOKUP else ["empty_query"],
            "query": "",
            "query_candidates": queries,
            "title": "",
            "description": "",
            "link": "",
            "source": "naver_encyc",
            "content_hash": "",
            "error": "disabled" if not _ENABLE_ENCYCLOPEDIA_LOOKUP else "",
        }
    try:
        from agents.tools.encyclopedia_lookup import lookup_first

        result = lookup_first(queries, display=5, timeout=10.0)
        payload = result.as_dict()
        payload["enabled"] = True
        payload["query_candidates"] = queries
        return payload
    except Exception as exc:  # noqa: BLE001 - this tool must not break ProductUnderstanding
        return {
            "enabled": True,
            "configured": False,
            "found": False,
            "usable_for_routing": False,
            "quality_status": "error",
            "quality_reasons": ["lookup_exception"],
            "query": queries[0] if queries else "",
            "query_candidates": queries,
            "title": "",
            "description": "",
            "link": "",
            "source": "naver_encyc",
            "content_hash": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _distill_encyclopedia_identity(
    *,
    product_name: str,
    translated_product_name: str,
    encyclopedia_identity: dict[str, Any],
) -> dict[str, Any]:
    """Return compact identity fields distilled from raw Naver evidence.

    The returned object is allowed to enter routing DTO fields. Raw Naver
    entries remain evidence only.
    """
    entries = encyclopedia_identity.get("entries") or []
    try:
        from agents.tools.identity_distiller import IdentityDistillerTool

        return IdentityDistillerTool().distill(
            product_name=product_name,
            product_name_en=translated_product_name,
            entries=entries if isinstance(entries, list) else [],
        )
    except Exception as exc:  # noqa: BLE001 - ProductUnderstanding must degrade gracefully
        return {
            "configured": False,
            "succeeded": False,
            "provider": "gemini",
            "model": "",
            "prompt_version": "",
            "ingredient_class": "other",
            "food_form": "other",
            "processing_state": "unknown",
            "normalized_tariff_description": "",
            "source_used": "name_only",
            "confidence": 0.0,
            "identity_terms": [],
            "composition_terms": [],
            "processing_terms": [],
            "needs_review": True,
            "conflict_reason": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _allergen_term_count(text: str) -> int:
    lower = (text or "").lower()
    count = 0
    for term in ALLERGEN_FOOD_TERMS:
        if term.lower() in lower:
            count += 1
    return count


def _is_allergen_notice_text(text: str) -> bool:
    """Return True for allergen/cross-contact notices, not principal facts."""
    stripped = re.sub(r"\s+", " ", text or "").strip()
    if not stripped:
        return False
    if ALLERGEN_NOTICE_RE.search(stripped):
        return True
    # Korean labels often have a standalone line like
    # "우유, 대두, 밀, 돼지고기 ... 함유". Treat those as label/allergen
    # evidence only when multiple known allergen terms are present.
    if "함유" in stripped and _allergen_term_count(stripped) >= 2:
        return True
    return False


def _match_signals(patterns: list[tuple[str, str]], text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for signal, pattern in patterns:
        m = re.search(pattern, text or "", flags=re.I)
        if m:
            out.append({"signal": signal, "matched_text": m.group(0)})
    return out


class ProductUnderstandingAgent(BaseAgent):
    agent_name = "Product_Understanding_Agent"
    stage = "ProductUnderstanding"
    llm_model = _TRANSLATION_MODEL

    def run(self, store: BlackboardStore) -> None:
        bb = store.load()
        pes = bb.get("product_evidence_state") or {}
        if not pes:
            raise RuntimeError("No ProductEvidenceState on the Blackboard.")
        product_id = pes.get("product_id") or ""
        self.read_input(product_id)

        obs = pes.get("observed_facts") or {}
        product_name = str(obs.get("product_name") or "")
        description = str(obs.get("description") or "")

        raw_fact_texts = [
            _stringify_fact(v)
            for key in (
                "composition",
                "classification_input_product_facts",
                "classification_input_fact_texts",
                "ocr_text",
            )
            for v in _as_list(obs.get(key))
        ]
        raw_fact_texts = _dedupe(raw_fact_texts, limit=160)
        allergen_notice_texts = _dedupe(
            [text for text in raw_fact_texts if _is_allergen_notice_text(text)],
            limit=60,
        )
        fact_texts = _dedupe(
            [text for text in raw_fact_texts if not _is_allergen_notice_text(text)],
            limit=120,
        )
        classification_text = "\n".join(
            _dedupe([product_name, description, *fact_texts], limit=140)
        )

        # B-2 revised: first ask ProductUnderstandingTool for a structured
        # routing DTO. If unavailable, fall back to the older one-sentence
        # tariff translation for classifier compatibility.
        understanding_dto, understanding_error = build_product_understanding_dto(
            product_name=product_name,
            description=description,
            fact_texts=fact_texts,
            allergen_notice_texts=allergen_notice_texts,
        )
        raw_llm_understanding = (
            dict(understanding_dto) if isinstance(understanding_dto, dict) else {}
        )
        translated_product_name = str(
            understanding_dto.get("translated_product_name") or ""
        ).strip()
        commercial_identity = str(
            understanding_dto.get("commercial_identity") or ""
        ).strip()
        normalized_tariff_description = str(
            understanding_dto.get("normalized_tariff_description") or ""
        ).strip()
        # Validate LLM-proposed routing terms only against source evidence, not
        # against other LLM-proposed terms. Otherwise a hallucinated term such
        # as "rice meal" becomes self-justifying evidence.
        evidence_text_for_validation = "\n".join(
            [product_name, description, *fact_texts]
        )
        translated_product_name = _text_without_unsupported_forms(
            translated_product_name,
            evidence_text_for_validation,
        )
        commercial_identity = _text_without_unsupported_forms(
            commercial_identity,
            evidence_text_for_validation,
        )
        normalized_tariff_description = _text_without_unsupported_forms(
            normalized_tariff_description,
            evidence_text_for_validation,
        )
        classification_text_en = normalized_tariff_description or translate_to_tariff_english(
            product_name,
            _dedupe([description, *fact_texts], limit=40),
        )

        processing_signals = _match_signals(PROCESSED_SIGNAL_PATTERNS, classification_text)
        raw_signals = _match_signals(RAW_SIGNAL_PATTERNS, classification_text)
        domain_hints = _match_signals(DOMAIN_HINT_PATTERNS, classification_text)

        keywords = _extract_terms(classification_text)
        allergen_notice_terms = _extract_terms("\n".join(allergen_notice_texts))
        dto_processing_state = _normalize_processing_state(
            str(understanding_dto.get("processing_state") or "")
        )
        processing_state = (
            dto_processing_state
            if dto_processing_state
            else ("processed_or_prepared" if processing_signals else "unknown")
        )
        if raw_signals and not processing_signals and not dto_processing_state:
            processing_state = "raw_or_fresh"

        dto_domain_hints = [
            normalized_hint
            for hint in _list_strings(understanding_dto.get("domain_hints"), limit=12)
            for normalized_hint in [_normalize_domain_hint(hint)]
            if normalized_hint
        ]
        dto_product_form_terms = _list_strings(understanding_dto.get("product_form_terms"), limit=40)
        dto_processing_terms = _list_strings(understanding_dto.get("processing_terms"), limit=40)
        dto_principal_terms = _list_strings(
            understanding_dto.get("principal_ingredient_terms")
            or understanding_dto.get("principal_component_terms"),
            limit=40,
        )
        dto_ingredient_taxonomy_terms = _list_strings(
            understanding_dto.get("ingredient_taxonomy_terms"),
            limit=40,
        )
        dto_composition_terms = _list_strings(understanding_dto.get("composition_terms"), limit=60)
        dto_use_context_terms = _list_strings(understanding_dto.get("use_context_terms"), limit=40)
        dto_chapter_routing_terms = _list_strings(
            understanding_dto.get("chapter_routing_terms"),
            limit=80,
        )
        dto_routing_keywords = _list_strings(understanding_dto.get("routing_keywords"), limit=80)
        dto_product_form_terms = _list_without_unsupported_forms(
            dto_product_form_terms,
            evidence_text_for_validation,
        )
        dto_processing_terms = _list_without_unsupported_forms(
            dto_processing_terms,
            evidence_text_for_validation,
        )
        dto_principal_terms = _list_without_unsupported_forms(
            dto_principal_terms,
            evidence_text_for_validation,
        )
        dto_ingredient_taxonomy_terms = _list_without_unsupported_forms(
            dto_ingredient_taxonomy_terms,
            evidence_text_for_validation,
        )
        dto_composition_terms = _list_without_unsupported_forms(
            dto_composition_terms,
            evidence_text_for_validation,
        )
        dto_use_context_terms = _list_without_unsupported_forms(
            dto_use_context_terms,
            evidence_text_for_validation,
        )
        dto_chapter_routing_terms = _list_without_unsupported_forms(
            dto_chapter_routing_terms,
            evidence_text_for_validation,
        )
        dto_routing_keywords = _list_without_unsupported_forms(
            dto_routing_keywords,
            evidence_text_for_validation,
        )
        if _has_mollusc_evidence(evidence_text_for_validation):
            dto_ingredient_taxonomy_terms = _dedupe(
                [*dto_ingredient_taxonomy_terms, "mollusc", "aquatic invertebrate"],
                limit=40,
            )
            dto_principal_terms = _dedupe(
                [*dto_principal_terms, "mollusc"],
                limit=40,
            )
            if processing_state == "processed_or_prepared":
                dto_chapter_routing_terms = _dedupe(
                    [
                        *dto_chapter_routing_terms,
                        "prepared or preserved molluscs",
                        "mollusc preparation",
                    ],
                    limit=80,
                )
        blocked_routing_terms = [
            {
                "term": str(item.get("term") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
            for item in _as_list(understanding_dto.get("blocked_routing_terms"))
            if isinstance(item, dict) and str(item.get("term") or "").strip()
        ][:80]
        excluded_routing_terms = [
            {
                "term": str(item.get("term") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
            for item in _as_list(understanding_dto.get("excluded_from_routing_terms"))
            if isinstance(item, dict) and str(item.get("term") or "").strip()
        ][:80]
        excluded_term_values = {
            x["term"].lower()
            for x in [*blocked_routing_terms, *excluded_routing_terms]
        }
        dto_routing_terms = _dedupe(
            [
                translated_product_name,
                commercial_identity,
                *dto_chapter_routing_terms,
                *dto_routing_keywords,
                *dto_product_form_terms,
                *dto_processing_terms,
                *dto_principal_terms,
                *dto_ingredient_taxonomy_terms,
                *dto_composition_terms,
                *dto_use_context_terms,
            ],
            limit=120,
        )
        dto_routing_terms = [
            term for term in dto_routing_terms if term.lower() not in excluded_term_values
        ]
        encyclopedia_queries = _encyclopedia_query_candidates(
            product_name,
            translated_product_name,
            commercial_identity,
        )
        encyclopedia_identity = _lookup_encyclopedia_identity(encyclopedia_queries)
        distilled_identity = _distill_encyclopedia_identity(
            product_name=product_name,
            translated_product_name=translated_product_name,
            encyclopedia_identity=encyclopedia_identity,
        )
        encyclopedia_usable = bool(distilled_identity.get("succeeded")) and bool(
            distilled_identity.get("normalized_tariff_description")
        )
        lexical_grounding = dict(distilled_identity)
        lexical_identity_terms = _list_strings(distilled_identity.get("identity_terms"), limit=20)
        lexical_composition_terms = _list_strings(distilled_identity.get("composition_terms"), limit=20)
        lexical_processing_terms = _list_strings(distilled_identity.get("processing_terms"), limit=20)
        lexical_definition = str(distilled_identity.get("normalized_tariff_description") or "").strip()
        encyclopedia_term_text = " ".join(
            _dedupe(
                [
                    str(distilled_identity.get("ingredient_class") or ""),
                    str(distilled_identity.get("food_form") or ""),
                    str(distilled_identity.get("processing_state") or ""),
                    lexical_definition,
                    *lexical_identity_terms,
                    *lexical_composition_terms,
                    *lexical_processing_terms,
                ],
                limit=60,
            )
        ) if encyclopedia_usable else ""
        encyclopedia_terms = _extract_terms(encyclopedia_term_text)[:60]
        if encyclopedia_usable:
            self.cite(
                "naver_encyc",
                str(encyclopedia_identity.get("title") or encyclopedia_identity.get("query") or ""),
                snippet=str(encyclopedia_identity.get("description") or "")[:200],
                reason="Raw encyclopedia evidence used by IdentityDistillerTool.",
            )
            self.cite(
                "identity_distiller",
                str(distilled_identity.get("prompt_version") or "identity_distiller"),
                snippet=lexical_definition[:200],
                reason="Gemini-distilled compact identity used for ProductFacts_dto routing fields.",
            )
            dto_routing_keywords = _dedupe(
                [*encyclopedia_terms, *dto_routing_keywords],
                limit=80,
            )
            dto_chapter_routing_terms = _dedupe(
                [*encyclopedia_terms[:30], *dto_chapter_routing_terms],
                limit=80,
            )
            dto_routing_terms = _dedupe(
                [*encyclopedia_terms, *dto_routing_terms],
                limit=140,
            )
        elif encyclopedia_identity.get("found"):
            self.reason(
                "Encyclopedia identity evidence fetched but distillation did not produce routing identity: "
                + str(distilled_identity.get("error") or distilled_identity.get("conflict_reason") or "")
            )
        elif encyclopedia_identity.get("error"):
            self.reason(f"Encyclopedia identity lookup skipped/failed: {encyclopedia_identity.get('error')}")

        identity_lane = {
            "raw_product_name": product_name,
            "short_description": description,
            "cleaned_head_noun_candidates": encyclopedia_queries,
            "translated_product_name": translated_product_name[:500],
            "commercial_identity": commercial_identity[:800],
            "normalized_tariff_description": normalized_tariff_description[:2000],
            "product_form_terms": dto_product_form_terms,
            "commodity_identity_terms": _dedupe(
                [
                    translated_product_name,
                    commercial_identity,
                    normalized_tariff_description,
                    *dto_product_form_terms,
                    *dto_use_context_terms,
                    *encyclopedia_terms,
                    *lexical_identity_terms,
                ],
                limit=100,
            ),
            "distilled_identity": distilled_identity,
            "lexical_groundings": [distilled_identity] if distilled_identity else [],
            "external_identity_sources": [
                {
                    "source": "naver_encyc",
                    "enabled": bool(encyclopedia_identity.get("enabled")),
                    "configured": bool(encyclopedia_identity.get("configured")),
                    "found": bool(encyclopedia_identity.get("found")),
                    "usable_for_routing": False,
                    "quality_status": encyclopedia_identity.get("quality_status") or "",
                    "quality_reasons": encyclopedia_identity.get("quality_reasons") or [],
                    "query": encyclopedia_identity.get("query") or "",
                    "query_candidates": encyclopedia_identity.get("query_candidates") or [],
                    "entry_count": len(encyclopedia_identity.get("entries") or []),
                    "content_hash": encyclopedia_identity.get("content_hash") or "",
                    "entry_digest": distilled_identity.get("entry_digest") or "",
                    "error": encyclopedia_identity.get("error") or "",
                }
            ],
        }
        composition_lane = {
            "raw_ocr_fact_text_count": len(fact_texts),
            "principal_ingredient_terms": _dedupe(
                [*dto_principal_terms, *lexical_composition_terms],
                limit=60,
            ),
            "ingredient_taxonomy_terms": dto_ingredient_taxonomy_terms,
            "composition_terms": _dedupe(
                [*dto_composition_terms, *lexical_composition_terms],
                limit=80,
            ),
            "processing_terms": _dedupe(
                [*dto_processing_terms, *lexical_processing_terms],
                limit=60,
            ),
            "processing_state": processing_state,
            "processing_signals": processing_signals,
            "raw_material_signals": raw_signals,
            "allergen_notice_terms_excluded": allergen_notice_terms,
            "allergen_notice_texts_excluded": allergen_notice_texts,
        }
        classifier_projection = {
            "identity_context": "\n".join(
                x
                for x in [
                    translated_product_name,
                    commercial_identity,
                    normalized_tariff_description,
                    lexical_definition,
                    " ".join(lexical_identity_terms),
                ]
                if str(x).strip()
            )[:2500],
            "composition_context": "\n".join(
                _dedupe(
                    [
                        *dto_principal_terms,
                        *dto_ingredient_taxonomy_terms,
                        *dto_composition_terms,
                        *dto_processing_terms,
                        *lexical_composition_terms,
                        *lexical_processing_terms,
                        processing_state,
                    ],
                    limit=100,
                )
            )[:2500],
            "route_terms": _dedupe(
                [*dto_chapter_routing_terms, *dto_routing_keywords],
                limit=100,
            ),
            "excluded_context_summary": {
                "allergen_notice_text_count": len(allergen_notice_texts),
                "allergen_notice_terms": allergen_notice_terms[:40],
            },
        }

        understanding_id = store.next_id("pu")
        out = dto.ProductFacts_dto(
            created_by=self.agent_name,
            created_at=now_iso(),
            understanding_id=understanding_id,
            product_id=product_id,
            product_name=product_name,
            short_description=description,
            classification_text=classification_text[:12000],
            classification_text_en=classification_text_en[:2000],
            translated_product_name=translated_product_name[:500],
            commercial_identity=commercial_identity[:800],
            normalized_tariff_description=normalized_tariff_description[:2000],
            classification_text_line_count=len([x for x in classification_text.splitlines() if x.strip()]),
            keywords=keywords,
            keyword_map={
                "chapter_routing_terms": dto_chapter_routing_terms,
                "routing_keywords": dto_routing_keywords,
                "product_form_terms": dto_product_form_terms,
                "processing_terms": dto_processing_terms,
                "principal_ingredient_terms": _dedupe(
                    [*dto_principal_terms, *lexical_composition_terms],
                    limit=60,
                ),
                "ingredient_taxonomy_terms": dto_ingredient_taxonomy_terms,
                "composition_terms": _dedupe(
                    [*dto_composition_terms, *lexical_composition_terms],
                    limit=80,
                ),
                "use_context_terms": dto_use_context_terms,
            },
            product_form_terms=dto_product_form_terms,
            processing_terms=_dedupe([*dto_processing_terms, *lexical_processing_terms], limit=60),
            principal_ingredient_terms=_dedupe([*dto_principal_terms, *lexical_composition_terms], limit=60),
            ingredient_taxonomy_terms=dto_ingredient_taxonomy_terms,
            composition_terms=_dedupe([*dto_composition_terms, *lexical_composition_terms], limit=80),
            use_context_terms=dto_use_context_terms,
            chapter_routing_terms=dto_chapter_routing_terms,
            routing_keywords=dto_routing_keywords,
            blocked_routing_terms=blocked_routing_terms,
            excluded_from_routing_terms=excluded_routing_terms,
            allergen_notice_texts=allergen_notice_texts,
            allergen_notice_terms=allergen_notice_terms,
            processing_state=processing_state,
            processing_signals=processing_signals,
            raw_material_signals=raw_signals,
            domain_hints=_dedupe([*dto_domain_hints, *(d["signal"] for d in domain_hints)], limit=20),
            product_understanding_mode="llm_json" if understanding_dto else "regex_fallback",
            llm_understanding_error=understanding_error,
            llm_confidence=understanding_dto.get("confidence"),
            needs_review=bool(understanding_dto.get("needs_review")),
            routing_terms=_dedupe(
                [*dto_routing_terms, *(d["matched_text"] for d in domain_hints), *keywords],
                limit=140,
            ),
            unknowns=[
                "processing_state" if processing_state == "unknown" else "",
                "primary_ingredient_ratio",
            ],
            identity_lane=identity_lane,
            composition_lane=composition_lane,
            classifier_projection=classifier_projection,
            evidence={
                "fact_text_count": len(fact_texts),
                "allergen_notice_text_count": len(allergen_notice_texts),
                "has_ocr_text": bool(obs.get("ocr_text")),
                "encyclopedia_lookup": {
                    "source": "naver_encyc",
                    "enabled": bool(encyclopedia_identity.get("enabled")),
                    "configured": bool(encyclopedia_identity.get("configured")),
                    "found": bool(encyclopedia_identity.get("found")),
                    "usable_for_routing": False,
                    "quality_status": encyclopedia_identity.get("quality_status") or "",
                    "quality_reasons": encyclopedia_identity.get("quality_reasons") or [],
                    "query": encyclopedia_identity.get("query") or "",
                    "query_candidates": encyclopedia_identity.get("query_candidates") or [],
                    "title": encyclopedia_identity.get("title") or "",
                    "description": encyclopedia_identity.get("description") or "",
                    "raw_entries": encyclopedia_identity.get("entries") or [],
                    "content_hash": encyclopedia_identity.get("content_hash") or "",
                    "error": encyclopedia_identity.get("error") or "",
                },
                "identity_distillation": distilled_identity,
                "llm_raw_understanding": raw_llm_understanding,
                "llm_understanding_trace": _product_understanding_trace(),
                "llm_raw_understanding_usage": "audit_only_not_routing_input",
                "sanitized_for_routing_fields": [
                    "translated_product_name",
                    "commercial_identity",
                    "normalized_tariff_description",
                    "product_form_terms",
                    "processing_terms",
                    "principal_ingredient_terms",
                    "ingredient_taxonomy_terms",
                    "composition_terms",
                    "use_context_terms",
                    "chapter_routing_terms",
                    "routing_keywords",
                ],
                "ontology_context_files": list(_ONTOLOGY_CONTEXT_FILES),
                "input_reconstruction_mode": (
                    (obs.get("input_reconstruction") or {}).get("mode")
                    if isinstance(obs.get("input_reconstruction"), dict)
                    else None
                ),
            },
        )

        store.put("product_understanding", out)
        self.wrote(understanding_id)
        self.reason(
            f"Built ProductUnderstandingFacts {understanding_id}: "
            f"{len(keywords)} keyword(s), processing_state={processing_state}, "
            f"domain_hints={out['domain_hints']}, mode={out['product_understanding_mode']}."
        )
