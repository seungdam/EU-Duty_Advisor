"""LLM agent for ProductUnderstanding identity lane.

Baseline-styled, additive path:
  - product name + Wikipedia-derived identity evidence
  - JSON-completion with chapter-index context

The agent never emits HS/CN codes or direct routing decisions; it only enriches
current identity fields and routing hint terms.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from bussiness_logic.bridge.adapter import RuntimeAdapter
from bussiness_logic.product.model.product_understanding import (
    DistilledIdentityFacts,
    EncyclopediaEvidenceSet,
)
from bussiness_logic.product.services.identity_hint_guard import (
    BuildIdentityGuardEvidence,
    BuildIdentityGuardPromptBlock,
    CacheIdentityHintCandidate,
    GetCachedIdentityHintCandidate,
    ValidateIdentityHintCandidate,
)


# [WCO 21부 · 07-23 설계자 지시] 도메인 힌트를 임의 6분류에서 **HS협약
# 공식 21부(Section I~XXI)**로 재작성 — 문서 근거(WCO Nomenclature 부
# 구조·챕터 대응은 협약 정본, 창작 0). 이해 LLM의 chapter_group 21택
# 승인분(07-22)의 구현 자리. 향후 챕터 게이트 일반화(부→허용 챕터)의
# 정본 맵으로도 소비 예정.
WCO_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("I", "01-05", "live animals; animal products"),
    ("II", "06-14", "vegetable products"),
    ("III", "15", "animal or vegetable fats and oils"),
    ("IV", "16-24", "prepared foodstuffs; beverages; tobacco"),
    ("V", "25-27", "mineral products"),
    ("VI", "28-38", "chemical products"),
    ("VII", "39-40", "plastics; rubber"),
    ("VIII", "41-43", "hides; leather; furskins"),
    ("IX", "44-46", "wood; cork; straw"),
    ("X", "47-49", "pulp; paper"),
    ("XI", "50-63", "textiles"),
    ("XII", "64-67", "footwear; headgear"),
    ("XIII", "68-70", "stone; ceramics; glass"),
    ("XIV", "71", "pearls; precious metals"),
    ("XV", "72-83", "base metals"),
    ("XVI", "84-85", "machinery; electrical equipment"),
    ("XVII", "86-89", "vehicles; aircraft; vessels"),
    ("XVIII", "90-92", "optical and measuring; clocks; musical instruments"),
    ("XIX", "93", "arms and ammunition"),
    ("XX", "94-96", "miscellaneous manufactured articles"),
    ("XXI", "97", "works of art; antiques"),
)
DOMAIN_HINT_VOCAB = tuple(s[0] for s in WCO_SECTIONS)

# [축질문 재설계 · 07-23 설계자 승인] 닫힌 enum은 기존 닫힌집합에서 유도
# (수기 목록 0): 형태=_FORM_VOCAB(축스탬프 정본), 가공=분류 상태 어휘
# (frozen/chilled 등 보존어는 보존 슬롯으로 분리).
from bussiness_logic.classification.services.axis_verdict import (  # noqa: E402
    _FORM_VOCAB as _AXIS_FORM_VOCAB,
)
_PROC_RAW = frozenset({
    "raw", "fresh", "frozen", "chilled", "minced", "dried", "live", "whole",
})
_PROC_PREPARED = frozenset({
    "prepared", "cooked", "seasoned", "smoked", "roasted", "boiled", "fried",
    "steamed", "preserved", "processed", "instant",
})
_PROC_ENUM = sorted(_PROC_PREPARED | (_PROC_RAW - {"frozen", "chilled", "whole", "minced", "live"}))
_FORM_ENUM = sorted(_AXIS_FORM_VOCAB)

_IDENTITY_SYSTEM_PROMPT = f"""
Return only one JSON object. No markdown, no code fence.
You are IdentityHintAgent. Answer AXIS QUESTIONS about ONE product from the
supplied evidence. Evidence priority (highest wins on conflict):
  1) normalized_coi_product_facts — product-specific ingredients, species,
     percentages and origin. This is composition authority.
  2) product_label_facts — Korean regulatory label lines (식품유형/원재료명/보관).
     The label's declared food-type IS the product identity. Trust it over
     marketing words and over encyclopedia titles.
  3) product_name.
  4) encyclopedia_titles — entity-validated vocabulary bridge ONLY. Do NOT
     treat an encyclopedia article as the product. Never summarize it.

Never invent. If the evidence does not answer a question, return "" (empty —
downstream treats empty as SILENT and will ask, so empty is SAFE, guessing is
NOT). Do NOT output HS/CN/TARIC codes.

JSON keys (all required):
- name_en: short literal English translation of the product name.
- identity_head: ONE short English noun phrase for WHAT THE PRODUCT IS SOLD AS
  (e.g. "noodle dish", "fish cake", "soup", "rice cake", "seasoned pork ribs").
  Label food-type first; tariff-register nouns; no marketing words.
- principal_ingredient: the single FIRST-listed (highest content) ingredient,
  short English (e.g. "wheat flour", "pork", "clam"). From 원재료명 line when
  present; "" if unknown. Report ranking fact only — no legal judgement.
- processing_state: one of [{", ".join(_PROC_ENUM)}] or "".
- preservation_state: one of [frozen, chilled, ambient] or "".
- physical_form: one of [{", ".join(_FORM_ENUM)}] or "".
- intended_use: one of [human consumption, baby food, animal feed, industrial] or "".
- domain_hints: the WCO SECTION(s) this product belongs to — usually exactly
  ONE roman numeral, two only for a genuine border case. Choose from:
  {"; ".join(f"{sid}=ch{rng} {label}" for sid, rng, label in WCO_SECTIONS)}.
- confidence: 0..1. needs_review: true/false.
""".strip()

_chapter_context_cache: list[str] = []
_chapter_vocab_cache: list[frozenset[str]] = []


def _scoped_chapter_vocab(max_owners: int = 3) -> frozenset[str]:
    """[8회차-1] 등급 승선제 '중'의 법정 어휘 — 소유 챕터 ≤N 토큰만.

    전 챕터 공유 토큰(also/are/food 류)은 판별력 0(라우터 층1과 동일
    원리)이라 승선 근거가 될 수 없다 — 기준선 실측(IU·Haiti가 범용
    토큰으로 중 승선)의 처방. 원천은 cn_chapter_index 행 전체, 수기 0.
    """
    if _scoped_vocab_cache:
        return _scoped_vocab_cache[0]
    owners: dict[str, set] = {}
    from bussiness_logic.classification.repositories.chapter_index_repository import (
        LoadPreClassificationChapterRows,
    )

    for row in LoadPreClassificationChapterRows():
        ch = str(row.get("chapter") or "").strip()
        if not ch:
            continue
        text = " ".join(
            str(row.get(key) or "")
            for key in (
                "title",
                "description",
                "heading_scope",
                "chapter_keywords",
                "raw_scope_signals",
                "prepared_scope_signals",
            )
        )
        for token in set(re.findall(r"[a-z]{3,}", text.lower())):
            owners.setdefault(token, set()).add(ch)
    _scoped_vocab_cache.append(frozenset(
        tok for tok, chs in owners.items() if len(chs) <= max_owners))
    return _scoped_vocab_cache[0]


_scoped_vocab_cache: list[frozenset[str]] = []


def _chapter_vocab() -> frozenset[str]:
    """Word vocabulary of the cn_chapter_index context (DB text, cached).

    Typed identity fields are accepted only when grounded in this vocabulary —
    the same DB-grounding idea as chapter_hint_terms, with no enum in the
    prompt and no hand-written word list.
    """
    if not _chapter_vocab_cache:
        _chapter_vocab_cache.append(
            frozenset(re.findall(r"[a-z]{3,}", _chapter_context().lower())),
        )
    return _chapter_vocab_cache[0]


def _grounded_typed_field(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > 40:
        return ""
    tokens = re.findall(r"[a-z]+", text)
    if tokens and any(token in _chapter_vocab() for token in tokens):
        return text
    return ""


_grounding_vocab_cache: list[frozenset[str]] = []


def _grounding_vocab() -> frozenset[str]:
    """[게이트 어휘 광역판 · 07-23 회귀 수리] 정체 게이트용 어휘 =
    전 품목표 서술(cn_table heading/subheading/cn8 descriptions)
    ∪ taxonomy(통칭 match·ranks·tariff_terms — pork→swine 다리 입구 보존)
    ∪ 식품유형 사전 EN(udon 등 통칭 병기 보존). 전부 기계 유도·수기 0.
    챕터 요약 어휘(_chapter_vocab)는 호/소호 층 정체 단어(octopus·rib)가
    없어 정체 절단 회귀 실측(낙지 octopus 소멸·쪽갈비 대두→ch12 표류)
    — 챕터 힌트 그라운딩 전용으로 원위치."""
    if _grounding_vocab_cache:
        return _grounding_vocab_cache[0]
    toks: set = set()
    # ① 전 품목표 서술 — DB authority only.
    from sqlalchemy import text as _sql_text
    from db.db_session_manager import DbSessionManager

    manager = DbSessionManager.GetInstance()
    if not manager.TableExists("cn_table"):
        raise RuntimeError("Required runtime table is missing: cn_table")
    for row in manager.FetchRows(
        _sql_text(
            "SELECT heading_description AS h, subheading_description AS s,"
            " cn8_description AS c FROM cn_table"
        )
    ):
        payload = dict(row)
        for key in ("h", "s", "c"):
            toks |= set(
                re.findall(
                    r"[a-z]{3,}",
                    str(payload.get(key) or "").lower(),
                )
            )
    # ② taxonomy 통칭·등록어
    from bussiness_logic.product.services.co_loader import _taxonomy

    for entry in (_taxonomy().get("entries") or []):
        for key in ("match", "ranks", "tariff_terms"):
            for value in (entry.get(key) or []):
                toks |= set(re.findall(r"[a-z]{3,}", str(value).lower()))
    # ③ 식품유형 사전 EN
    from bussiness_logic.core.runtime_asset_repository import (
        LoadFoodTypeDictionary,
    )

    for value in LoadFoodTypeDictionary().values():
        toks |= set(re.findall(r"[a-z]{3,}", str(value).lower()))
    _grounding_vocab_cache.append(frozenset(toks))
    return _grounding_vocab_cache[0]


def _vocab_grounded_text(value: object, *, limit_tokens: int = 12) -> str:
    """[지터 게이트 · 2026-07-23 설계자 승인] 자유작문 문자열 → 관세
    어휘집(_grounding_vocab, 전 품목표+taxonomy+사전 기계유도) 교차 토큰만
    남긴 정준형(등장 순서 보존·중복 제거). 어휘 밖 작문 토큰(dish/item/
    themed 류 — 매런 바뀌는 지터 통로)이 결정층으로 흐르는 것을 **코드로
    차단**(프롬프트 지시 아님 — code-driven 원칙). 필수 DB 자산이 없으면
    무게이트 통과하지 않고 런타임 오류로 중단한다."""
    toks = re.findall(r"[a-z]{3,}", str(value or "").lower())
    vocab = _grounding_vocab()
    if not vocab:
        return str(value or "").strip()
    seen: set = set()
    out: list[str] = []
    for t in toks:
        # 단복수 브리지: 'rib'↔'ribs' — 품목표가 복수형만 쓰는 경우 보존
        if (t in vocab or t + "s" in vocab
                or (t.endswith("s") and t[:-1] in vocab)) and t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out[:limit_tokens])


def _vocab_grounded_terms(values: object, *, limit: int) -> tuple[str, ...]:
    """항 목록 게이트 — ASCII 항은 어휘집 교차 토큰으로 재구성(빈 항 탈락),
    한글 포함 항은 무게이트 통과(라벨/표제 병기 계보 — 어휘집이 영문이라
    게이트 불가·전사 병기 보존)."""
    out: list[str] = []
    seen: set = set()
    for v in (values or ()):
        s = str(v).strip()
        if not s:
            continue
        if re.search(r"[가-힣]", s):
            g = s
        else:
            g = _vocab_grounded_text(s, limit_tokens=6)
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return tuple(out[:limit])


def _extract_json(text: str) -> dict[str, object]:
    raw = str(text or "").strip()
    if "```" in raw:
        raw = re.sub(r"^```[a-zA-Z]*", "", raw).strip().rstrip("`").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            value = json.loads(raw[start : end + 1])
            if isinstance(value, dict):
                return value
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _chapter_context() -> str:
    """Compact chapter-index context used only for hint grounding."""
    if _chapter_context_cache:
        return _chapter_context_cache[0]

    lines: list[str] = []
    from bussiness_logic.classification.repositories.chapter_index_repository import (
        LoadPreClassificationChapterRows,
    )

    for row in LoadPreClassificationChapterRows():
        chapter = str(row.get("chapter") or "").strip()
        parts = [
            str(row.get("title") or "").strip(),
            str(row.get("description") or "").strip(),
            str(row.get("heading_scope") or "").strip(),
            str(row.get("chapter_keywords") or "").strip(),
            str(row.get("raw_scope_signals") or "").strip(),
            str(row.get("prepared_scope_signals") or "").strip(),
        ]
        if chapter and any(parts):
            scope = " | ".join(part for part in parts if part)
            lines.append(f"{chapter}: {scope}".rstrip())

    context = "cn_chapter_index (chapter: scope):\n" + "\n".join(lines[:97]) if lines else ""
    _chapter_context_cache.append(context)
    return context


def _compact_evidence(
    *,
    productName: str,
    distilledIdentity: DistilledIdentityFacts,
    encyclopediaEvidence: EncyclopediaEvidenceSet,
    factTexts: tuple[str, ...] = (),
    acceptedEncyclopediaTitles: tuple[str, ...] | None = None,
    normalizedCoiBlock: str = "",
) -> str:
    # [축질문 재설계 · 07-23] 백과 = **표제만**(본문·요약·스니펫 폐지 —
    # 타요/탐폰 계보의 본문 오염 원천 차단, "본문은 오염원·제목만" 실측
    # 원칙의 공급 시점 적용). 약 등급 불승선은 유지.
    if acceptedEncyclopediaTitles is None:
        _has_strong = any(
            str(getattr(e, "grade", "") or "") == "strong"
            for e in encyclopediaEvidence.entries)
        _boarded = [
            e for e in encyclopediaEvidence.entries
            if str(getattr(e, "grade", "") or "") != "weak"
            and not (_has_strong
                     and str(getattr(e, "grade", "") or "") == "medium")
        ][:3]
        _titles = [
            str(e.title).strip() for e in _boarded if str(e.title).strip()
        ]
        for _t in (distilledIdentity.sourceTitles or ()):
            _t = str(_t).strip()
            if _t and _t not in _titles:
                _titles.append(_t)
    else:
        _titles = list(acceptedEncyclopediaTitles)
    encyc = "\n".join(f"- {t}" for t in _titles[:5])
    # [축질문 재설계] 라벨 = 1급 증거 — 기본 ON으로 반전(종전 기본 OFF가
    # "최고 권위 증거를 LLM에 숨기던" 정보 절단이었음, 설계자 확정).
    # ASAP_IDENTITY_FACTS=0 으로만 구기준선 복귀. 상한 8줄/720자 유지.
    label_block = ""
    if (os.environ.get("ASAP_IDENTITY_FACTS", "1") or "1").strip() != "0" and factTexts:
        lines = [str(x).strip()[:180] for x in factTexts if str(x).strip()][:8]
        label_block = "\n\nproduct_label_facts:\n" + "\n".join(f"- {x}" for x in lines)[:720]
    return (
        f"product_name: {productName}\n"
        f"encyclopedia_titles:\n{encyc or '-'}"
        f"{chr(10) + chr(10) + normalizedCoiBlock if normalizedCoiBlock else ''}"
        f"{label_block}"
    )


def _dedup_strings(
    values: object,
    *,
    limit: int,
    allow_single_char: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, tuple):
        rawValues = values
    elif isinstance(values, list):
        rawValues = tuple(values)
    else:
        rawValues = (str(values).strip(),) if str(values).strip() else ()

    out: list[str] = []
    for item in rawValues:
        text = str(item or "").strip()
        if not text:
            continue
        if not allow_single_char and len(text) < 2:
            continue
        if text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


class IdentityHintAgent:
    """Build bounded HS2 routing hints from product and encyclopedia evidence."""

    def __init__(
        self,
        runtimeAdapter: Optional[RuntimeAdapter[object]],
    ) -> None:
        self._runtimeAdapter = runtimeAdapter

    def BuildIdentityFacts(
        self,
        *,
        productName: str,
        distilledIdentity: DistilledIdentityFacts,
        encyclopediaEvidence: EncyclopediaEvidenceSet,
        max_tokens: int | None = None,
        factTexts: tuple[str, ...] = (),
    ) -> dict[str, object]:
        return _BuildIdentityFacts(
            productName=productName,
            distilledIdentity=distilledIdentity,
            encyclopediaEvidence=encyclopediaEvidence,
            max_tokens=max_tokens,
            factTexts=factTexts,
            runtimeAdapter=self._runtimeAdapter,
        )


def _BuildIdentityFacts(
    *,
    productName: str,
    distilledIdentity: DistilledIdentityFacts,
    encyclopediaEvidence: EncyclopediaEvidenceSet,
    max_tokens: int | None = None,
    factTexts: tuple[str, ...] = (),
    runtimeAdapter: Optional[RuntimeAdapter[object]],
) -> dict[str, object]:
    """Combine evidence into identity fields via one LLM call.

    Returns a dict keyed by ``IdentityHintSet`` fields plus
    understanding mode + error fields. On failure returns a best-effort failure
    payload; caller overlays regex identity.
    """
    from bussiness_logic.bridge.schema import LlmGenerationOptions, LlmRequest

    if runtimeAdapter is None:
        return {
            "understanding_mode": "llm_fallback",
            "llm_error": "identity_hint_runtime_not_configured",
        }

    tokens = max_tokens if max_tokens is not None else int(
        os.environ.get("ASAP_PRODUCT_UNDERSTANDING_MAX_TOKENS", "4096")
    )
    guardEnabled = (
        os.environ.get("ASAP_IDENTITY_HINT_GUARD", "0") or "0"
    ).strip().lower() in ("1", "true", "yes", "on")
    guardEvidence = (
        BuildIdentityGuardEvidence(
            productName=productName,
            factTexts=factTexts,
            encyclopediaEvidence=encyclopediaEvidence,
        )
        if guardEnabled
        else None
    )
    user_prompt = _compact_evidence(
        productName=productName,
        distilledIdentity=distilledIdentity,
        encyclopediaEvidence=encyclopediaEvidence,
        factTexts=factTexts,
        acceptedEncyclopediaTitles=(
            guardEvidence.acceptedEncyclopediaTitles if guardEvidence else None
        ),
        normalizedCoiBlock=(
            BuildIdentityGuardPromptBlock(guardEvidence) if guardEvidence else ""
        ),
    )
    chapter_context = _chapter_context()
    if chapter_context:
        user_prompt = f"{user_prompt}\n\n{chapter_context}"
    # 빈/무효 응답은 1회 재시도 — US_KR name-only 실측에서 실패 10건 중
    # 8건이 empty_or_invalid_json(복불복)이었다. 실패 시 원문 앞부분을
    # llm_error에 남겨 '무엇이 왔는지'를 사후 판독 가능하게 한다.
    parsed = (
        GetCachedIdentityHintCandidate(guardEvidence.evidenceHash) or {}
        if guardEvidence
        else {}
    )
    cacheHit = bool(parsed)
    cachedGuardReasons = tuple(parsed.pop("_identity_guard_reasons", ()))
    raw_text = ""
    last_error = ""
    for attempt in range(0 if cacheHit else 2):
        try:
            response = runtimeAdapter.Generate(
                LlmRequest(
                    user_prompt=user_prompt,
                    system_prompt=_IDENTITY_SYSTEM_PROMPT,
                    generation_options=LlmGenerationOptions(temperature=0, max_tokens=tokens),
                ),
            )
            raw_text = str(getattr(response, "generatedText", ""))
            parsed = _extract_json(raw_text)
        except Exception as error:  # noqa: BLE001 — fallback to regex only
            last_error = f"{type(error).__name__}: {error}"
            parsed = {}
        if parsed:
            break

    if not parsed:
        return {
            "understanding_mode": "llm_fallback",
            "llm_error": (
                f"empty_or_invalid_json(retried) raw[:200]={raw_text[:200]!r}"
                if not last_error else f"{last_error} (retried)"
            ),
        }

    guardReasons: tuple[str, ...] = ()
    guardHash = ""
    guardNeedsReview = bool(parsed.get("needs_review"))
    if guardEvidence:
        guardResult = ValidateIdentityHintCandidate(
            parsed=parsed,
            evidence=guardEvidence,
        )
        parsed = guardResult.candidate
        guardReasons = cachedGuardReasons or guardResult.reasons
        guardHash = guardResult.evidenceHash
        guardNeedsReview = guardResult.needsReview
        cachePayload = dict(parsed)
        cachePayload["_identity_guard_reasons"] = list(guardReasons)
        CacheIdentityHintCandidate(guardEvidence.evidenceHash, cachePayload)

    # [축질문 매핑 · 07-23 설계자 승인] 6질문 답 → 기존 IdentityHintSet
    # 필드(하류 계약 보존). 자유작문 필드는 전부 코드 조립으로 대체 —
    # LLM 산출은 닫힌 슬롯뿐, enum 밖 값은 코드가 기각(빈칸=SILENT).
    head = _vocab_grounded_text(parsed.get("identity_head"), limit_tokens=6)
    name_en = str(parsed.get("name_en") or "").strip()[:80]
    principal = str(parsed.get("principal_ingredient") or "").strip().lower()[:40]
    proc = str(parsed.get("processing_state") or "").strip().lower()
    if proc not in set(_PROC_ENUM):
        proc = ""
    pres = str(parsed.get("preservation_state") or "").strip().lower()
    if pres not in ("frozen", "chilled", "ambient"):
        pres = ""
    form = str(parsed.get("physical_form") or "").strip().lower()
    if form not in set(_FORM_ENUM):
        form = ""
    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    # ntd = 코드 조립(LLM 문장 폐지) — 구문 통째 세미콜론 병기(합성어 보존).
    # 라벨 전사 헤드(_AssembleNtdHead)가 컴포넌트에서 추가로 앞에 병기된다.
    ntd = "; ".join(dict.fromkeys(
        p for p in (head, principal, proc, pres, form) if p))
    hint_terms = _vocab_grounded_terms([head, principal], limit=8)
    return {
        "translated_product_name": name_en,
        "commercial_identity": head or name_en or productName,
        "normalized_tariff_description": ntd,
        "identity_terms": _vocab_grounded_terms(
            [name_en, head, principal], limit=16),
        "product_form_terms": _dedup_strings(
            [form, proc, pres], limit=20),
        "ingredient_class": _grounded_typed_field(principal),
        "principal_ingredient_guess": principal if (os.environ.get(
            "ASAP_IDENTITY_PRINCIPAL", "1") or "1").strip() != "0" else "",
        "accessory_ingredients": (),  # 성분 서열은 composition entries가 정본
        "food_form": head,
        "processing_state": proc,
        "preservation_state": pres,
        "physical_form": form,
        "domain_hints": tuple(
            term
            for term in _dedup_strings(parsed.get("domain_hints"), limit=6)
            if term in DOMAIN_HINT_VOCAB
        )[:6],
        "chapter_hint_terms": hint_terms,
        "chapter_hint_source_terms": _dedup_strings([head, principal], limit=8),
        "chapter_hint_basis": (
            (
                f"axis_questions_guarded:{guardHash[:12]}"
                if guardEnabled
                else "axis_questions"
            )
            if hint_terms
            else (
                "axis_questions_guarded_empty"
                if guardEnabled
                else "axis_questions_empty"
            )),
        "chapter_hint_status": "enabled" if hint_terms else "not_enabled",
        "confidence": confidence,
        "needs_review": guardNeedsReview,
        "conflict_reason": "; ".join(guardReasons)[:600],
        "identity_guard_hash": guardHash,
        "understanding_mode": "llm_json",
        "llm_error": "",
    }
