"""Deterministic validation for IdentityHintAgent candidates.

The LLM proposes bounded identity fields. This module decides which values are
supported by product-specific evidence before they are written to the existing
ProductUnderstanding DTO.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from bussiness_logic.product.model.product_understanding import (
        EncyclopediaEvidenceSet,
    )


GUARD_VERSION = "identity-hint-guard-v1"

_ENTITY_BLOCK_RE = re.compile(
    r"\b(?:actor|actress|album|athlete|baseball|born|bus|city|district|film|"
    r"football|footballer|game|politician|school|singer|song|station|subway|"
    r"television|tv series)\b|"
    r"(?:배우|가수|선수|정치인|축구|야구|도시|지하철|화재|버스|학교|"
    r"드라마|영화|게임|앨범|노래|방송)",
    re.IGNORECASE,
)
_REQUIRES_COOKING_RE = re.compile(
    r"(?:가열|조리)\s*(?:하여|해서|후)?\s*섭취|"
    r"(?:반드시\s*)?(?:가열|조리)\s*(?:필요|요망)|"
    r"(?:cook|heat)\s+before\s+(?:eating|consumption)|"
    r"(?:must|should)\s+be\s+(?:cooked|heated)|"
    r"requires?\s+(?:cooking|heating)|ready[\s-]*to[\s-]*cook",
    re.IGNORECASE,
)
_NON_PROCESS_RE = re.compile(
    r"(?:수산물|축산물|농산물|기타\s*수산물)?\s*가공품|processed\s+product",
    re.IGNORECASE,
)
_GENERIC_HEAD_WORDS = frozenset({"food", "goods", "item", "product"})
_HEAD_STATE_WORDS = frozenset({
    "ambient",
    "boiled",
    "chilled",
    "cooked",
    "dried",
    "fresh",
    "fried",
    "frozen",
    "instant",
    "minced",
    "prepared",
    "preserved",
    "processed",
    "raw",
    "roasted",
    "seasoned",
    "sliced",
    "smoked",
    "steamed",
    "whole",
})

_PROCESS_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "boiled": (re.compile(r"(?:삶은|데친|boiled)", re.I),),
    "cooked": (
        re.compile(r"(?:조리\s*완료|완전\s*가열|익힌|cooked|fully\s+cooked)", re.I),
    ),
    "dried": (re.compile(r"(?:건조|말린|dried)", re.I),),
    "fresh": (re.compile(r"(?:신선|fresh)", re.I),),
    "fried": (re.compile(r"(?:튀김|튀긴|fried)", re.I),),
    "instant": (re.compile(r"(?:즉석|instant)", re.I),),
    "prepared": (
        re.compile(
            r"(?:볶음|볶은|무침|국(?:\s|$)|탕(?:\s|$)|찌개|전골|"
            r"조리된|즉석\s*섭취|stir[\s-]*fried|ready[\s-]*to[\s-]*eat|"
            r"soup|stew)",
            re.I,
        ),
    ),
    "preserved": (re.compile(r"(?:절임|염장|보존\s*처리|pickled|preserved)", re.I),),
    "raw": (re.compile(r"(?:비가열|날것|uncooked|raw)", re.I),),
    "roasted": (re.compile(r"(?:구운|구이|roasted|grilled)", re.I),),
    "seasoned": (re.compile(r"(?:양념|무침|seasoned)", re.I),),
    "smoked": (re.compile(r"(?:훈제|smoked)", re.I),),
    "steamed": (re.compile(r"(?:찐|증숙|steamed)", re.I),),
}
_PRESERVATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "frozen": re.compile(r"(?:냉동|동결|frozen|-\s*18\s*°?\s*c)", re.I),
    "chilled": re.compile(r"(?:냉장|chilled|refrigerated)", re.I),
    "ambient": re.compile(r"(?:실온|상온|ambient)", re.I),
}
_PHYSICAL_FORM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("minced", re.compile(r"(?:다진|다짐|민스|minced)", re.I)),
    ("ground", re.compile(r"(?:분쇄|갈은|ground)", re.I)),
    ("fillet", re.compile(r"(?:필레|fillet)", re.I)),
    ("sliced", re.compile(r"(?:슬라이스|얇게\s*썬|sliced)", re.I)),
    ("slice", re.compile(r"(?:절편|slice)", re.I)),
    ("strip", re.compile(r"(?:채썬|strip)", re.I)),
    ("powder", re.compile(r"(?:분말|powder)", re.I)),
    ("paste", re.compile(r"(?:페이스트|paste)", re.I)),
    ("whole", re.compile(r"(?:통째|통마리|whole)", re.I)),
    ("piece", re.compile(r"(?:조각|piece)", re.I)),
    ("block", re.compile(r"(?:블록|block)", re.I)),
)

_CACHE: dict[str, dict[str, object]] = {}


@dataclass(frozen=True, slots=True)
class IdentityGuardEvidence:
    evidenceHash: str
    productName: str
    factTexts: tuple[str, ...]
    coiPrincipalCandidates: tuple[str, ...]
    coiIngredientLines: tuple[str, ...]
    acceptedEncyclopediaTitles: tuple[str, ...]
    rejectedEncyclopediaTitles: tuple[str, ...]
    explicitProcessingStates: tuple[str, ...]
    explicitPreservationStates: tuple[str, ...]
    explicitPhysicalForms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IdentityHintGuardResult:
    candidate: dict[str, object]
    evidenceHash: str
    reasons: tuple[str, ...]
    needsReview: bool


def _Nfc(value: object) -> str:
    return unicodedata.normalize("NFC", " ".join(str(value or "").split()))


def _Tokens(value: object) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[0-9A-Za-z가-힣]+", _Nfc(value))
        if len(token) >= 2
    }


def _CanonicalTokens(value: object) -> set[str]:
    tokens = _Tokens(value)
    out = set(tokens)
    suffixes = (
        "볶음",
        "비빔밥",
        "국수",
        "전골",
        "찌개",
        "구이",
        "살",
        "국",
        "탕",
        "면",
        "장",
        "떡",
    )
    for token in tokens:
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                out.add(token[: -len(suffix)])
    return out


def _BuildCoiFacts(productName: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        from bussiness_logic.product.services.coi_loader import FindFormForProduct

        form = FindFormForProduct(productName)
    except Exception:  # noqa: BLE001 - missing normalized COI means no authority
        form = None
    if not isinstance(form, Mapping):
        return (), ()

    candidates = tuple(
        dict.fromkeys(
            _Nfc(value)
            for value in form.get("principal_candidates") or ()
            if _Nfc(value)
        )
    )[:4]
    lines: list[str] = []
    for entry in form.get("entries") or ():
        if not isinstance(entry, Mapping):
            continue
        name = _Nfc(entry.get("name_ko") or entry.get("name_raw"))
        subs = [_Nfc(value) for value in entry.get("sub_ingredients") or () if _Nfc(value)]
        if subs:
            name = f"{name} ({', '.join(subs)})"
        origin = _Nfc(entry.get("origin"))
        percent = entry.get("percent")
        parts = [part for part in (name, f"origin={origin}" if origin else "") if part]
        if percent not in (None, ""):
            parts.append(f"percent={percent}")
        if parts:
            lines.append("; ".join(parts))
    return candidates, tuple(dict.fromkeys(lines))[:12]


def _ValidateEncyclopediaTitles(
    *,
    productName: str,
    principalCandidates: tuple[str, ...],
    encyclopediaEvidence: "EncyclopediaEvidenceSet",
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    productTokens = _CanonicalTokens(productName)
    coiTokens = set().union(*(_CanonicalTokens(value) for value in principalCandidates))
    accepted: list[str] = []
    rejected: list[str] = []

    for entry in encyclopediaEvidence.entries:
        title = _Nfc(entry.title)
        if not title:
            continue
        context = f"{title} {_Nfc(entry.description)}"
        titleTokens = _CanonicalTokens(title)
        grade = _Nfc(getattr(entry, "grade", "")).lower()
        blocked = bool(_ENTITY_BLOCK_RE.search(context))
        grounded = bool(titleTokens & (productTokens | coiTokens))
        if not blocked and (grounded or grade == "strong"):
            accepted.append(title)
        else:
            rejected.append(title)
    return tuple(dict.fromkeys(accepted))[:5], tuple(dict.fromkeys(rejected))[:10]


def _ScrubNonProcessText(value: str) -> str:
    return _NON_PROCESS_RE.sub(" ", _REQUIRES_COOKING_RE.sub(" ", value))


def _DetectedValues(
    text: str,
    patterns: Mapping[str, tuple[re.Pattern[str], ...] | re.Pattern[str]],
) -> tuple[str, ...]:
    found: list[str] = []
    for value, patternSet in patterns.items():
        patternsToCheck = patternSet if isinstance(patternSet, tuple) else (patternSet,)
        if any(pattern.search(text) for pattern in patternsToCheck):
            found.append(value)
    return tuple(found)


def BuildIdentityGuardEvidence(
    *,
    productName: str,
    factTexts: tuple[str, ...],
    encyclopediaEvidence: "EncyclopediaEvidenceSet",
) -> IdentityGuardEvidence:
    principalCandidates, ingredientLines = _BuildCoiFacts(productName)
    acceptedTitles, rejectedTitles = _ValidateEncyclopediaTitles(
        productName=productName,
        principalCandidates=principalCandidates,
        encyclopediaEvidence=encyclopediaEvidence,
    )
    rawText = "\n".join((productName, *factTexts, *ingredientLines))
    processText = _ScrubNonProcessText(rawText)
    processing = _DetectedValues(processText, _PROCESS_PATTERNS)
    preservation = _DetectedValues(rawText, _PRESERVATION_PATTERNS)
    physical = tuple(
        value for value, pattern in _PHYSICAL_FORM_PATTERNS if pattern.search(rawText)
    )
    payload = {
        "guard_version": GUARD_VERSION,
        "product_name": _Nfc(productName),
        "fact_texts": [_Nfc(value) for value in factTexts],
        "coi_principal_candidates": list(principalCandidates),
        "coi_ingredient_lines": list(ingredientLines),
        "accepted_encyclopedia_titles": list(acceptedTitles),
        "rejected_encyclopedia_titles": list(rejectedTitles),
        "processing": list(processing),
        "preservation": list(preservation),
        "physical": list(physical),
    }
    evidenceHash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return IdentityGuardEvidence(
        evidenceHash=evidenceHash,
        productName=_Nfc(productName),
        factTexts=tuple(_Nfc(value) for value in factTexts if _Nfc(value)),
        coiPrincipalCandidates=principalCandidates,
        coiIngredientLines=ingredientLines,
        acceptedEncyclopediaTitles=acceptedTitles,
        rejectedEncyclopediaTitles=rejectedTitles,
        explicitProcessingStates=processing,
        explicitPreservationStates=preservation,
        explicitPhysicalForms=physical,
    )


def BuildIdentityGuardPromptBlock(evidence: IdentityGuardEvidence) -> str:
    lines: list[str] = []
    if evidence.coiPrincipalCandidates:
        lines.append(
            "normalized_coi_principal_candidates: "
            + " | ".join(evidence.coiPrincipalCandidates)
        )
    for line in evidence.coiIngredientLines[:8]:
        lines.append(f"normalized_coi_ingredient: {line}")
    return "\n".join(lines)


def _ResolvePrincipal(
    candidate: object,
    authorities: tuple[str, ...],
) -> tuple[str, str]:
    proposed = _Nfc(candidate)
    if not authorities:
        return proposed, ""
    if len(authorities) == 1:
        selected = authorities[0]
        reason = ""
        if proposed and not (_CanonicalTokens(proposed) & _CanonicalTokens(selected)):
            reason = f"principal_replaced_by_coi:{proposed}->{selected}"
        return selected, reason
    proposedTokens = _CanonicalTokens(proposed)
    for authority in authorities:
        if proposedTokens & _CanonicalTokens(authority):
            return authority, ""
    return "", "principal_ambiguous_against_coi"


def _StripHeadStateWords(value: object) -> str:
    words = re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)?|[가-힣]+", _Nfc(value).lower())
    kept = [word for word in words if word not in _HEAD_STATE_WORDS]
    meaningful = [word for word in kept if word not in _GENERIC_HEAD_WORDS]
    return " ".join(meaningful or kept)[:80]


def _StripUnsupportedProcessWords(value: object, supported: str) -> str:
    text = _Nfc(value)
    if supported:
        return text
    return " ".join(
        re.sub(
            r"\b(?:boiled|cooked|dried|fried|prepared|preserved|processed|roasted|"
            r"seasoned|smoked|steamed)\b",
            " ",
            text,
            flags=re.IGNORECASE,
        ).split()
    )


def ValidateIdentityHintCandidate(
    *,
    parsed: Mapping[str, object],
    evidence: IdentityGuardEvidence,
) -> IdentityHintGuardResult:
    candidate = dict(parsed)
    reasons: list[str] = []

    principal, principalReason = _ResolvePrincipal(
        candidate.get("principal_ingredient"),
        evidence.coiPrincipalCandidates,
    )
    candidate["principal_ingredient"] = principal
    if principalReason:
        reasons.append(principalReason)

    proposedProcessing = _Nfc(candidate.get("processing_state")).lower()
    if proposedProcessing and proposedProcessing not in evidence.explicitProcessingStates:
        reasons.append(f"unsupported_processing_state:{proposedProcessing}")
        proposedProcessing = ""
    candidate["processing_state"] = proposedProcessing

    proposedPreservation = _Nfc(candidate.get("preservation_state")).lower()
    if len(evidence.explicitPreservationStates) == 1:
        explicitPreservation = evidence.explicitPreservationStates[0]
        if proposedPreservation and proposedPreservation != explicitPreservation:
            reasons.append(
                "preservation_overridden_by_explicit_evidence:"
                f"{proposedPreservation}->{explicitPreservation}"
            )
        proposedPreservation = explicitPreservation
    elif proposedPreservation not in evidence.explicitPreservationStates:
        if proposedPreservation:
            reasons.append(f"unsupported_preservation_state:{proposedPreservation}")
        proposedPreservation = ""
    candidate["preservation_state"] = proposedPreservation

    proposedForm = _Nfc(candidate.get("physical_form")).lower()
    if len(evidence.explicitPhysicalForms) == 1:
        explicitForm = evidence.explicitPhysicalForms[0]
        if proposedForm and proposedForm != explicitForm:
            reasons.append(
                f"physical_form_overridden_by_explicit_evidence:{proposedForm}->{explicitForm}"
            )
        proposedForm = explicitForm
    elif proposedForm not in evidence.explicitPhysicalForms:
        if proposedForm:
            reasons.append(f"unsupported_physical_form:{proposedForm}")
        proposedForm = ""
    candidate["physical_form"] = proposedForm

    originalHead = _Nfc(candidate.get("identity_head"))
    guardedHead = _StripHeadStateWords(originalHead)
    if originalHead and guardedHead != originalHead.lower():
        reasons.append(f"identity_head_state_terms_removed:{originalHead}->{guardedHead}")
    candidate["identity_head"] = guardedHead
    candidate["name_en"] = _StripUnsupportedProcessWords(
        candidate.get("name_en"),
        proposedProcessing,
    )

    if evidence.rejectedEncyclopediaTitles:
        reasons.append(
            "encyclopedia_rejected:" + "|".join(evidence.rejectedEncyclopediaTitles)
        )

    needsReview = bool(candidate.get("needs_review")) or bool(reasons)
    candidate["needs_review"] = needsReview
    return IdentityHintGuardResult(
        candidate=candidate,
        evidenceHash=evidence.evidenceHash,
        reasons=tuple(reasons),
        needsReview=needsReview,
    )


def GetCachedIdentityHintCandidate(evidenceHash: str) -> dict[str, object] | None:
    value = _CACHE.get(evidenceHash)
    return dict(value) if value is not None else None


def CacheIdentityHintCandidate(
    evidenceHash: str,
    candidate: Mapping[str, object],
) -> None:
    _CACHE[evidenceHash] = dict(candidate)


def ClearIdentityHintGuardCache() -> None:
    _CACHE.clear()
