"""Stable classification-question identity and explicit user-answer overlay."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


VALID_ANSWERS = frozenset({"yes", "no", "unknown"})
QUESTION_CONTRACT_VERSION = 2


def _normalized_value(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return " ".join(text.split()).casefold()
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).casefold()
    except (TypeError, ValueError):
        return " ".join(str(value or "").split()).casefold()


def BuildClassificationQuestionKey(
    *,
    stage: str,
    parentCode: str,
    candidateCode: str,
    axis: str,
    canonicalField: str,
    conditionValue: Any,
    predicateOp: str,
    contextScope: str = "",
) -> str:
    """Return a stable identifier for one legal branch question."""
    payload = {
        "stage": str(stage or "").strip().lower(),
        "parent": str(parentCode or "").strip(),
        "candidate": str(candidateCode or "").strip(),
        "axis": str(axis or "").strip().lower(),
        "field": str(canonicalField or "").strip(),
        "value": _normalized_value(conditionValue),
        "op": str(predicateOp or "").strip().lower(),
        "context": " ".join(str(contextScope or "").split()).casefold(),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"cq_{digest}"


def NormalizeClassificationAnswer(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_ANSWERS else ""


def ResolveQuestionCandidateCode(
    detail: Mapping[str, Any],
    *,
    parentCode: str,
    candidateCode: str,
    contextScope: str,
) -> str:
    """Use one stable answer key for a context repeated across sibling leaves."""
    if (
        str(contextScope or "").strip()
        and str(detail.get("context_scope") or "").strip()
    ):
        return str(parentCode or "").strip()
    return str(candidateCode or "").strip()


def FindClassificationAnswer(
    productFacts: Mapping[str, Any],
    *,
    questionKey: str,
) -> Mapping[str, Any] | None:
    """Read an explicit answer without treating it as a generated DTO fact."""
    records = productFacts.get("_classification_answer_facts") or []
    if not isinstance(records, list):
        return None
    for record in reversed(records):
        if not isinstance(record, Mapping):
            continue
        if str(record.get("question_key") or "") != questionKey:
            continue
        if NormalizeClassificationAnswer(record.get("answer")):
            return record
    return None


def _detail_contract(
    detail: Mapping[str, Any],
    *,
    stage: str,
    parentCode: str,
    candidateCode: str,
    contextScope: str,
) -> tuple[str, str, Any, str, str]:
    axis = str(
        detail.get("binding_axis")
        or detail.get("axis")
        or detail.get("cond")
        or ""
    ).strip()
    canonicalField = str(detail.get("field") or "").strip()
    if not canonicalField:
        canonicalField = str(detail.get("binding_paths") or "").split(";", 1)[0].strip()
    conditionValue = detail.get("value")
    predicateOp = str(detail.get("op") or "").strip().lower() or "affirmative"
    questionCandidateCode = ResolveQuestionCandidateCode(
        detail,
        parentCode=parentCode,
        candidateCode=candidateCode,
        contextScope=contextScope,
    )
    questionKey = BuildClassificationQuestionKey(
        stage=stage,
        parentCode=parentCode,
        candidateCode=questionCandidateCode,
        axis=axis,
        canonicalField=canonicalField,
        conditionValue=conditionValue,
        predicateOp=predicateOp,
        contextScope=contextScope,
    )
    return axis, canonicalField, conditionValue, predicateOp, questionKey


def ApplyClassificationAnswers(
    *,
    decisionStatus: str,
    decisionDetail: list[dict[str, Any]],
    productFacts: Mapping[str, Any],
    stage: str,
    parentCode: str,
    candidateCode: str,
    contextScope: str = "",
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Overlay answers on matching SILENCE details and recompute O/X/SILENCE."""
    if not decisionDetail:
        return decisionStatus, decisionDetail, []

    updated: list[dict[str, Any]] = []
    appliedKeys: list[str] = []
    for rawDetail in decisionDetail:
        detail = dict(rawDetail)
        verdict = str(detail.get("verdict") or "").strip().lower()
        if verdict not in ("", "unknown", "undecided", "silent"):
            updated.append(detail)
            continue
        axis, canonicalField, conditionValue, predicateOp, questionKey = _detail_contract(
            detail,
            stage=stage,
            parentCode=parentCode,
            candidateCode=candidateCode,
            contextScope=contextScope,
        )
        record = FindClassificationAnswer(productFacts, questionKey=questionKey)
        if record is None:
            updated.append(detail)
            continue
        answer = NormalizeClassificationAnswer(record.get("answer"))
        if answer == "unknown":
            answerVerdict = "silent"
        elif predicateOp == "not_contains":
            answerVerdict = "false" if answer == "yes" else "true"
        else:
            answerVerdict = "true" if answer == "yes" else "false"
        detail["overridden_by"] = questionKey
        detail["verdict"] = answerVerdict
        updated.append(detail)
        updated.append({
            "cond": axis,
            "op": "user_answer",
            "predicate_op": predicateOp,
            "contract_version": QUESTION_CONTRACT_VERSION,
            "verdict": answerVerdict,
            "field": canonicalField,
            "why": "explicit_user_answer",
            "value": _normalized_value(conditionValue),
            "question_key": questionKey,
            "answer_id": str(record.get("answer_id") or ""),
            "answered_at": str(record.get("answered_at") or ""),
        })
        appliedKeys.append(questionKey)

    if not appliedKeys:
        return decisionStatus, updated, []

    effectiveVerdicts: list[str] = []
    alternativeGroups: dict[str, list[str]] = {}
    for detail in updated:
        op = str(detail.get("op") or "")
        if op in ("axis_projection", "user_answer"):
            continue
        verdict = str(detail.get("verdict") or "").lower()
        if verdict in ("true", "false", "silent", "unknown", "undecided", ""):
            normalizedVerdict = verdict or "silent"
            alternativeGroup = str(detail.get("alt_group") or "")
            if alternativeGroup:
                alternativeGroups.setdefault(alternativeGroup, []).append(
                    normalizedVerdict,
                )
            else:
                effectiveVerdicts.append(normalizedVerdict)
    for groupVerdicts in alternativeGroups.values():
        if "true" in groupVerdicts:
            effectiveVerdicts.append("true")
        elif groupVerdicts and all(value == "false" for value in groupVerdicts):
            effectiveVerdicts.append("false")
        else:
            effectiveVerdicts.append("silent")
    if "false" in effectiveVerdicts:
        status = "violated"
    elif effectiveVerdicts and all(value == "true" for value in effectiveVerdicts):
        status = "confirmed"
    else:
        status = "undecided"
    return status, updated, appliedKeys
