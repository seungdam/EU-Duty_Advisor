"""
Product_Understanding_Agent — deterministic product understanding stage.

This stage turns ProductEvidenceState into a compact read model for
DomainRouterTool and Classification_Agent. It intentionally does not call an
LLM. The output is a Blackboard object so admin logs can show how raw OCR/text
became routing keywords and processing-state signals.
"""
from __future__ import annotations

import re
from typing import Any

import os
from pathlib import Path

from agents.agent_base import BaseAgent
from agents.blackboard import BlackboardStore, now_iso

# B-2: a lightweight, non-thinking model translates the cleaned Korean product
# facts into ONE English description phrased in HS/CN tariff nomenclature so it
# matches the English cn_table descriptions the retriever scores against. The
# heavy classification model (gemma4-ctx) is left untouched; translation is a
# separate cheap step (~1-2s on gemma3:4b).
_TRANSLATION_MODEL = os.environ.get("ASAP_TRANSLATION_MODEL", "gemma3:4b")
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
    ("soup_or_stew", r"국|탕|찌개|스프|soup|stew|broth"),
    ("frozen_prepared", r"냉동.*(조제|가공|볶음|튀김)|frozen.*(prepared|cooked)"),
]

RAW_SIGNAL_PATTERNS: list[tuple[str, str]] = [
    ("raw", r"생물|생고기|생선|raw"),
    ("fresh", r"신선|fresh"),
    ("live", r"활어|산|live"),
    ("dried_raw", r"건조|말린|dried"),
]

DOMAIN_HINT_PATTERNS: list[tuple[str, str]] = [
    ("food", r"식품|라면|면|소스|국|탕|만두|주꾸미|김치|food|edible|noodle|sauce|soup"),
    ("cosmetics", r"화장품|크림|로션|향수|샴푸|cosmetic|cream|lotion|perfume|shampoo"),
    ("pharmaceutical", r"의약품|약품|백신|medicine|pharmaceutical|tablet|capsule|vaccine"),
]


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

        fact_texts = [
            _stringify_fact(v)
            for key in (
                "composition",
                "classification_input_product_facts",
                "classification_input_fact_texts",
                "ocr_text",
            )
            for v in _as_list(obs.get(key))
        ]
        fact_texts = _dedupe(fact_texts, limit=120)
        classification_text = "\n".join(
            _dedupe([product_name, description, *fact_texts], limit=140)
        )
        lower_text = classification_text.lower()

        # B-2: translate the cleaned Korean facts into tariff-nomenclature English
        # so the retriever (which scores against English cn_table descriptions)
        # can surface the right candidates. Best-effort; "" on failure.
        classification_text_en = translate_to_tariff_english(
            product_name,
            _dedupe([description, *fact_texts], limit=40),
        )

        processing_signals = _match_signals(PROCESSED_SIGNAL_PATTERNS, classification_text)
        raw_signals = _match_signals(RAW_SIGNAL_PATTERNS, classification_text)
        domain_hints = _match_signals(DOMAIN_HINT_PATTERNS, classification_text)

        keywords = _extract_terms(classification_text)
        processing_state = "processed_or_prepared" if processing_signals else "unknown"
        if raw_signals and not processing_signals:
            processing_state = "raw_or_fresh"

        understanding_id = store.next_id("pu")
        out = {
            "object_type": "ProductUnderstandingFacts",
            "created_by": self.agent_name,
            "created_at": now_iso(),
            "understanding_id": understanding_id,
            "product_id": product_id,
            "source_product_id": product_id,
            "product_name": product_name,
            "short_description": description,
            "classification_text": classification_text[:12000],
            "classification_text_en": classification_text_en[:2000],
            "classification_text_line_count": len([x for x in classification_text.splitlines() if x.strip()]),
            "keywords": keywords,
            "processing_state": processing_state,
            "processing_signals": processing_signals,
            "raw_material_signals": raw_signals,
            "domain_hints": [d["signal"] for d in domain_hints],
            "routing_terms": _dedupe(
                [*(d["matched_text"] for d in domain_hints), *keywords],
                limit=80,
            ),
            "unknowns": [
                "processing_state" if processing_state == "unknown" else "",
                "primary_ingredient_ratio",
            ],
            "evidence": {
                "fact_text_count": len(fact_texts),
                "has_ocr_text": bool(obs.get("ocr_text")),
                "input_reconstruction_mode": (
                    (obs.get("input_reconstruction") or {}).get("mode")
                    if isinstance(obs.get("input_reconstruction"), dict)
                    else None
                ),
            },
        }
        out["unknowns"] = [u for u in out["unknowns"] if u]

        store.put("product_understanding", out)
        self.wrote(understanding_id)
        self.reason(
            f"Built ProductUnderstandingFacts {understanding_id}: "
            f"{len(keywords)} keyword(s), processing_state={processing_state}, "
            f"domain_hints={out['domain_hints']}."
        )
