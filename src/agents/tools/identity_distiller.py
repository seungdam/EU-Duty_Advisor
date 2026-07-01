"""IdentityDistillerTool - compact commodity identity extraction.

The distiller turns product name + raw encyclopedia entries into a small,
closed-schema commodity identity. Raw encyclopedia text remains evidence-only;
DomainRouter should read this distilled object, not the raw entries.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


GEMINI_ENDPOINT = (
    os.environ.get("ASAP_IDENTITY_DISTILLER_ENDPOINT_URL")
    or os.environ.get("ASAP_ENGINE_GEMINI_ENDPOINT_URL")
    or os.environ.get("EU_EXPORT_GOOGLE_AI_STUDIO_ENDPOINT_URL")
    or "https://generativelanguage.googleapis.com/v1beta/openai"
).rstrip("/")
GEMINI_CHAT_COMPLETIONS_PATH = (
    os.environ.get("ASAP_IDENTITY_DISTILLER_CHAT_COMPLETIONS_PATH")
    or os.environ.get("ASAP_ENGINE_GEMINI_CHAT_COMPLETIONS_PATH")
    or os.environ.get("EU_EXPORT_LLM_CHAT_COMPLETIONS_PATH")
    or "/chat/completions"
)
DEFAULT_MODEL = "gemini-2.5-flash"
PROMPT_VERSION = "identity_distiller_v1_fewshot_2026_06_30"

INGREDIENT_CLASSES = {
    "fish",
    "crustacean",
    "mollusc",
    "meat",
    "poultry",
    "vegetable",
    "fruit",
    "cereal",
    "rice",
    "noodle",
    "dairy",
    "egg",
    "legume",
    "nut",
    "sugar",
    "beverage",
    "mixed",
    "other",
}
FOOD_FORMS = {
    "raw_ingredient",
    "soup",
    "sauce",
    "paste",
    "noodle",
    "dumpling",
    "rice_meal",
    "rice_cake",
    "bread_pastry",
    "fried_snack",
    "side_dish",
    "mixed_meal",
    "beverage",
    "other",
}
PROCESSING_STATES = {
    "raw",
    "frozen_raw",
    "chilled_raw",
    "cooked",
    "fried",
    "roasted",
    "seasoned",
    "marinated",
    "preserved",
    "dried",
    "smoked",
    "unknown",
}


def _load_local_env() -> None:
    project_root = Path(os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[3]))
    env_path = project_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


def _read_api_key() -> str:
    for key in (
        "ASAP_IDENTITY_DISTILLER_GEMINI_API_KEY",
        "ASAP_ENGINE_GEMINI_API_KEY",
        "EU_EXPORT_GOOGLE_AI_STUDIO_API_KEY",
        "EU_EXPORT_LLM_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def _list_strings(value: Any, *, limit: int = 12) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        candidates = re.split(r"[,;/|]\s*|\n+", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = []
    for item in candidates:
        text = re.sub(r"\s+", " ", str(item or "").strip())
        if text and text not in out:
            out.append(text[:120])
        if len(out) >= limit:
            break
    return out


def _entry_digest(entries: list[dict[str, Any]]) -> str:
    raw = json.dumps(
        [
            {
                "rank": item.get("rank"),
                "title": item.get("title"),
                "description": item.get("description"),
                "link": item.get("link"),
                "content_hash": item.get("content_hash"),
            }
            for item in entries[:3]
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class DistilledIdentity:
    configured: bool
    succeeded: bool
    provider: str = "gemini"
    model: str = DEFAULT_MODEL
    prompt_version: str = PROMPT_VERSION
    ingredient_class: str = "other"
    food_form: str = "other"
    processing_state: str = "unknown"
    normalized_tariff_description: str = ""
    source_used: str = "name_only"
    confidence: float = 0.0
    identity_terms: list[str] = field(default_factory=list)
    composition_terms: list[str] = field(default_factory=list)
    processing_terms: list[str] = field(default_factory=list)
    needs_review: bool = False
    conflict_reason: str = ""
    raw_response_hash: str = ""
    entry_digest: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class IdentityDistillerTool:
    """Code-driven Gemini distiller with closed enum validation."""

    def __init__(self, *, model: str | None = None, timeout: float = 60.0) -> None:
        eu_export_provider = (os.environ.get("EU_EXPORT_LLM_PROVIDER") or "").lower()
        eu_export_model = (
            os.environ.get("EU_EXPORT_LLM_MODEL")
            if eu_export_provider in {"gemini", "google", "google_ai_studio"}
            else ""
        )
        self.model = (
            model
            or os.environ.get("ASAP_IDENTITY_DISTILLER_MODEL")
            or os.environ.get("ASAP_ENGINE_GEMINI_MODEL")
            or eu_export_model
            or DEFAULT_MODEL
        )
        self.timeout = timeout

    def distill(
        self,
        *,
        product_name: str,
        product_name_en: str = "",
        entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        entries = list(entries or [])[:3]
        api_key = _read_api_key()
        entry_digest = _entry_digest(entries)
        if not api_key:
            return DistilledIdentity(
                configured=False,
                succeeded=False,
                model=self.model,
                entry_digest=entry_digest,
                needs_review=True,
                error="gemini_api_key_missing",
            ).as_dict()

        prompt = self._build_prompt(product_name=product_name, product_name_en=product_name_en, entries=entries)
        try:
            raw = self._call_gemini(prompt, api_key=api_key)
            parsed = _extract_json(raw)
            return self._validate(parsed, raw_response=raw, entry_digest=entry_digest).as_dict()
        except Exception as exc:  # noqa: BLE001 - distillation must degrade gracefully
            return DistilledIdentity(
                configured=True,
                succeeded=False,
                model=self.model,
                entry_digest=entry_digest,
                needs_review=True,
                error=f"{type(exc).__name__}: {exc}",
            ).as_dict()

    def _call_gemini(self, prompt: str, *, api_key: str) -> str:
        endpoint_root = (
            os.environ.get("ASAP_IDENTITY_DISTILLER_ENDPOINT_URL")
            or os.environ.get("ASAP_ENGINE_GEMINI_ENDPOINT_URL")
            or os.environ.get("EU_EXPORT_GOOGLE_AI_STUDIO_ENDPOINT_URL")
            or os.environ.get("EU_EXPORT_LLM_ENDPOINT_URL")
            or GEMINI_ENDPOINT
        ).rstrip("/")
        chat_path = (
            os.environ.get("ASAP_IDENTITY_DISTILLER_CHAT_COMPLETIONS_PATH")
            or os.environ.get("ASAP_ENGINE_GEMINI_CHAT_COMPLETIONS_PATH")
            or os.environ.get("EU_EXPORT_LLM_CHAT_COMPLETIONS_PATH")
            or GEMINI_CHAT_COMPLETIONS_PATH
        )
        endpoint = f"{endpoint_root}{chat_path}"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract customs commodity identity only. "
                        "Return one minified valid JSON object only. "
                        "Do not classify HS/CN/TARIC."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 2048,
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def _build_prompt(self, *, product_name: str, product_name_en: str, entries: list[dict[str, Any]]) -> str:
        evidence = {
            "product_name_ko": product_name,
            "product_name_en": product_name_en,
            "naver_encyclopedia_entries": [
                {
                    "rank": item.get("rank"),
                    "title": item.get("title"),
                    "description": item.get("description"),
                    "link": item.get("link"),
                    "content_hash": item.get("content_hash"),
                }
                for item in entries[:3]
            ],
        }
        return (
            "Task: distill commodity identity for EU customs routing.\n"
            "Use the Korean product name first. Use encyclopedia entries only as grounding.\n"
            "Ignore recipes, cooking steps, nutrition, marketing blurbs, restaurants, blogs, and unrelated meanings.\n"
            "If entries conflict with an obvious commodity in the product name, prefer the product name and set needs_review=true.\n"
            "Do not output HS/CN/TARIC codes.\n\n"
            "Allowed enums:\n"
            f"ingredient_class={sorted(INGREDIENT_CLASSES)}\n"
            f"food_form={sorted(FOOD_FORMS)}\n"
            f"processing_state={sorted(PROCESSING_STATES)}\n\n"
            "Examples:\n"
            "Input product_name='멘보샤', entry says '중국식 새우 토스트 또는 새우 샌드위치 튀김'.\n"
            "{\"ingredient_class\":\"crustacean\",\"food_form\":\"fried_snack\",\"processing_state\":\"fried\","
            "\"normalized_tariff_description\":\"fried shrimp toast made with bread\","
            "\"source_used\":1,\"confidence\":0.88,\"identity_terms\":[\"shrimp toast\",\"fried shrimp sandwich\"],"
            "\"composition_terms\":[\"shrimp\",\"bread\"],\"processing_terms\":[\"fried\"],"
            "\"needs_review\":false,\"conflict_reason\":\"\"}\n"
            "Input product_name='명란', entry says 'pollack roe; salted pollack roe'.\n"
            "{\"ingredient_class\":\"fish\",\"food_form\":\"raw_ingredient\",\"processing_state\":\"preserved\","
            "\"normalized_tariff_description\":\"salted pollack roe\","
            "\"source_used\":1,\"confidence\":0.86,\"identity_terms\":[\"pollack roe\"],"
            "\"composition_terms\":[\"fish roe\"],\"processing_terms\":[\"salted\",\"preserved\"],"
            "\"needs_review\":false,\"conflict_reason\":\"\"}\n"
            "Input product_name='다진 청양고추', no useful entry.\n"
            "{\"ingredient_class\":\"vegetable\",\"food_form\":\"raw_ingredient\",\"processing_state\":\"raw\","
            "\"normalized_tariff_description\":\"minced green chilli pepper\","
            "\"source_used\":\"name_only\",\"confidence\":0.72,\"identity_terms\":[\"green chilli pepper\"],"
            "\"composition_terms\":[\"chilli pepper\"],\"processing_terms\":[\"minced\"],"
            "\"needs_review\":false,\"conflict_reason\":\"\"}\n\n"
            "Output JSON keys exactly:\n"
            "ingredient_class, food_form, processing_state, normalized_tariff_description, "
            "source_used, confidence, identity_terms, composition_terms, processing_terms, "
            "needs_review, conflict_reason\n\n"
            "Input evidence:\n"
            f"{json.dumps(evidence, ensure_ascii=False)}"
        )

    def _validate(self, parsed: dict[str, Any], *, raw_response: str, entry_digest: str) -> DistilledIdentity:
        ingredient_class = str(parsed.get("ingredient_class") or "other").strip()
        food_form = str(parsed.get("food_form") or "other").strip()
        processing_state = str(parsed.get("processing_state") or "unknown").strip()
        if ingredient_class not in INGREDIENT_CLASSES:
            ingredient_class = "other"
        if food_form not in FOOD_FORMS:
            food_form = "other"
        if processing_state not in PROCESSING_STATES:
            processing_state = "unknown"

        try:
            confidence = float(parsed.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        source_used = parsed.get("source_used")
        if source_used in (1, 2, 3):
            source_used_text = str(source_used)
        else:
            source_used_text = str(source_used or "name_only")
            if source_used_text not in {"1", "2", "3", "name_only"}:
                source_used_text = "name_only"

        description = re.sub(r"\s+", " ", str(parsed.get("normalized_tariff_description") or "").strip())
        needs_review = bool(parsed.get("needs_review")) or not description
        return DistilledIdentity(
            configured=True,
            succeeded=True,
            model=self.model,
            ingredient_class=ingredient_class,
            food_form=food_form,
            processing_state=processing_state,
            normalized_tariff_description=description[:240],
            source_used=source_used_text,
            confidence=confidence,
            identity_terms=_list_strings(parsed.get("identity_terms"), limit=12),
            composition_terms=_list_strings(parsed.get("composition_terms"), limit=12),
            processing_terms=_list_strings(parsed.get("processing_terms"), limit=12),
            needs_review=needs_review,
            conflict_reason=str(parsed.get("conflict_reason") or "").strip()[:240],
            raw_response_hash=hashlib.sha256(raw_response.encode("utf-8")).hexdigest()[:16],
            entry_digest=entry_digest,
            error="",
        )
