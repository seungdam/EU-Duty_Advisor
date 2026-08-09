"""Evaluate an intermediate nomenclature context against canonical DTO facts.

The evaluator separates identity, processing, preservation, and signed-form
clauses so a compound label such as ``Uncooked pasta, not stuffed`` is not
compared as one string or bound to one DTO field. Its O/X/SILENCE result is
authoritative only inside that context and cannot alter an ancestor code.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from bussiness_logic.classification.rules.axis_field_binding import (
    ResolveAxisFieldBinding,
)
from bussiness_logic.classification.rules.branch_predicate_evaluator import (
    MatchHasTokenPolarity,
    _dig,
)
from bussiness_logic.classification.rules.species_taxonomy import (
    EvaluateSpeciesQuestion,
)

_TOKEN = re.compile(r"[a-z]+")
_PROCESSING = frozenset({"uncooked", "cooked"})
_PRESERVATION = frozenset({"fresh", "chilled", "frozen", "dried", "smoked"})
_FORMS = frozenset({
    "stuffed",
    "filled",
    "minced",
    "ground",
    "whole",
    "piece",
    "pieces",
})
_FORM_EQUIVALENTS = {
    "stuffed": frozenset({"stuffed", "filled"}),
    "filled": frozenset({"stuffed", "filled"}),
    "minced": frozenset({"minced", "ground", "paste", "puree"}),
    "ground": frozenset({"minced", "ground", "paste", "puree"}),
    "whole": frozenset({"whole"}),
    "piece": frozenset({"piece", "pieces", "portion", "portions", "fillet"}),
    "pieces": frozenset({"piece", "pieces", "portion", "portions", "fillet"}),
}
_RESIDUAL_CONTEXT_WORDS = frozenset({
    "other",
    "elsewhere",
    "specified",
    "included",
    "nes",
})
_LEGAL_WORDS = frozenset({
    "and",
    "as",
    "but",
    "in",
    "or",
    "not",
    "non",
    "otherwise",
    "whether",
    "with",
    "without",
    "to",
    "prepared",
    "preserved",
})
_FORM_PATHS = ";".join((
    "composition_facts.physical_form",
    "composition_facts.contains_wrapper_or_dough",
    "identity_hints.physical_form",
    "identity_hints.food_form",
    "identity_hints.product_form_terms",
))
_PROCESSING_PATHS = ";".join((
    "composition_facts.processing_state",
    "identity_hints.processing_state",
))
_OTHERWISE_PREPARED_PATHS = (
    "composition_facts.processing_state",
    "composition_facts.component_compositions",
)
_PRESERVATION_PATHS = ";".join((
    "composition_facts.preservation_state",
    "identity_hints.preservation_state",
))
_SPECIES_PATHS = (
    "composition_facts.principal_ingredient",
    "composition_facts.ingredient_classes",
    "composition_facts.ingredient_entries",
    "identity_hints.principal_ingredient_guess",
    "identity_hints.ingredient_class",
)


def _tokens(value: Any) -> set[str]:
    return set(_TOKEN.findall(str(value or "").lower().replace("-", " ")))


def _condition(
    axis: str,
    expected: str,
    actual: str,
    reason: str,
    value: str,
    canonical_field: str = "",
) -> dict[str, str]:
    return {
        "axis": axis,
        "expected": expected,
        "verdict": actual,
        "reason": reason,
        "value": value,
        "canonical_field": canonical_field,
    }


def _flatten_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        out: list[str] = []
        for nested in value.values():
            out.extend(_flatten_text(nested))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for nested in value:
            out.extend(_flatten_text(nested))
        return out
    return [str(value)]


def _otherwise_prepared_observation(
    *,
    negative: bool,
    product_facts: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Answer the legal ``otherwise prepared`` clause from direct facts only."""
    texts = [
        text.strip().lower().replace("_", " ")
        for path in _OTHERWISE_PREPARED_PATHS
        for text in _flatten_text(_dig(product_facts, path))
        if text.strip()
    ]
    positive = any(
        re.search(r"\botherwise[ -]+prepared\b", text)
        and not re.search(
            r"\b(?:not|non|without)\b[^,;]{0,48}"
            r"\botherwise[ -]+prepared\b",
            text,
        )
        for text in texts
    ) or any(
        re.search(r"\b(?:pre[ -]?cooked|precooked|prepared[ -]+pasta)\b", text)
        for text in texts
    )
    denied = any(
        re.search(
            r"\b(?:not|non|without)\b[^,;]{0,48}"
            r"\botherwise[ -]+prepared\b",
            text,
        )
        or re.search(r"\b(?:unprepared|plain[ -]+uncooked)\b", text)
        for text in texts
    )

    if positive and denied:
        observed = "SILENCE"
        reason = "otherwise_prepared_conflict"
    elif positive:
        observed = "O"
        reason = "explicit_otherwise_prepared"
    elif denied:
        observed = "X"
        reason = "explicit_not_otherwise_prepared"
    else:
        observed = "SILENCE"
        reason = "otherwise_prepared_unanswered"

    if negative and observed in {"O", "X"}:
        observed = "X" if observed == "O" else "O"
    return _condition(
        "processing_method",
        "not" if negative else "yes",
        observed,
        reason,
        "otherwise prepared",
        ";".join(_OTHERWISE_PREPARED_PATHS),
    )


def _identity_observation(
    identity_tokens: set[str],
    product_facts: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    if not identity_tokens:
        return None
    identity_text = " ".join(sorted(identity_tokens))
    binding = ResolveAxisFieldBinding(
        "product_identity",
        product_facts,
        questionTokens=frozenset(identity_tokens),
    )
    if (
        binding.status == "answered"
        and identity_tokens <= set(binding.tokens)
    ):
        return _condition(
            "product_identity",
            "O",
            "O",
            "canonical_identity_match",
            identity_text,
            binding.dtoField,
        )

    try:
        taxonomy = EvaluateSpeciesQuestion(
            [identity_text],
            product_facts,
            _SPECIES_PATHS,
        )
    except Exception:  # Runtime asset absence remains SILENCE, never X.
        taxonomy = None
    if taxonomy is not None and taxonomy.verdict in {"O", "X"}:
        return _condition(
            "species_source",
            "O",
            taxonomy.verdict,
            taxonomy.reason,
            identity_text,
            ";".join(_SPECIES_PATHS),
        )

    if binding.status != "answered":
        return _condition(
            "product_identity",
            "O",
            "SILENCE",
            "canonical_field_empty",
            identity_text,
            binding.dtoField,
        )
    # An unresolved taxonomy plus a different commercial wording is not proof
    # of exclusion.  Keep the context open for a question.
    return _condition(
        "product_identity",
        "O",
        "SILENCE",
        "identity_not_proven",
        identity_text,
        binding.dtoField,
    )


def _form_observation(
    form: str,
    *,
    negative: bool,
    product_facts: Mapping[str, Any] | None,
) -> dict[str, str]:
    binding = ResolveAxisFieldBinding(
        "physical_form",
        product_facts,
        questionTokens=frozenset({form}),
    )
    form_tokens = set(binding.tokens)
    equivalents = _FORM_EQUIVALENTS.get(form, frozenset({form}))
    positive = bool(form_tokens & equivalents)

    if form in {"minced", "ground"}:
        opposite = bool(
            form_tokens & {"whole", "piece", "pieces", "portion", "fillet"}
        )
    elif form in {"whole", "piece", "pieces"}:
        opposite = bool(form_tokens & {"minced", "ground", "paste", "puree"})
    else:
        opposite = False

    if positive and opposite:
        observed = "SILENCE"
        reason = "physical_form_conflict"
    elif positive:
        observed = "O"
        reason = "canonical_form_match"
    elif opposite:
        observed = "X"
        reason = "canonical_form_opposite"
    else:
        signed, signed_reason = MatchHasTokenPolarity(
            frozenset({form}),
            product_facts,
            _FORM_PATHS,
        )
        observed = {
            "true": "O",
            "false": "X",
        }.get(signed or "", "SILENCE")
        reason = signed_reason or "physical_form_unanswered"

    if negative and observed in {"O", "X"}:
        observed = "X" if observed == "O" else "O"
    return _condition(
        "physical_form",
        "not" if negative else "yes",
        observed,
        reason,
        form,
        binding.dtoField,
    )


def ObserveBranchContext(
    context_scope: str,
    product_facts: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return O/X/SILENCE trace without selecting or eliminating a code."""
    text = str(context_scope or "").strip()
    words = _tokens(text)
    if not words:
        return {"verdict": "SILENCE", "conditions": [], "reason": "empty_context"}
    semantic_words = words - _LEGAL_WORDS - _RESIDUAL_CONTEXT_WORDS
    if not semantic_words:
        return {
            "verdict": "O",
            "conditions": [
                _condition(
                    "branch_context",
                    "O",
                    "O",
                    "residual_context_no_gate",
                    text,
                )
            ],
            "reason": "residual_context_no_gate",
            "context_scope": text,
            "authority": "canonical_context",
        }

    conditions: list[dict[str, str]] = []
    source_clauses: list[str] = []

    for state in sorted(words & _PROCESSING):
        source_clauses.append(f"processing:{state}")
        verdict, reason = MatchHasTokenPolarity(
            frozenset({state}),
            product_facts,
            _PROCESSING_PATHS,
        )
        conditions.append(_condition(
            "processing_method",
            "O",
            {"true": "O", "false": "X"}.get(verdict or "", "SILENCE"),
            reason or "processing_state_unanswered",
            state,
            _PROCESSING_PATHS,
        ))

    preservation_states = sorted(words & _PRESERVATION)
    if preservation_states:
        source_clauses.append(
            "preservation:" + "|".join(preservation_states)
        )
        binding = ResolveAxisFieldBinding(
            "preservation_state",
            product_facts,
            questionTokens=frozenset(preservation_states),
        )
        if binding.status != "answered":
            verdict = "SILENCE"
            reason = "preservation_state_empty"
        elif set(preservation_states) & set(binding.tokens):
            verdict = "O"
            reason = "preservation_state_match"
        else:
            verdict = "X"
            reason = "preservation_state_mismatch"
        conditions.append(_condition(
            "preservation_state",
            "O",
            verdict,
            reason,
            " or ".join(preservation_states),
            binding.dtoField,
        ))

    neutral_form_clause = "whether or not" in text.lower()
    positive_form_conditions: list[dict[str, str]] = []
    negative_form_conditions: list[dict[str, str]] = []
    for form in sorted(words & _FORMS):
        if neutral_form_clause:
            continue
        negative = bool(re.search(
            rf"\b(?:not|non|without)[ -]+{re.escape(form)}\b",
            text,
            re.IGNORECASE,
        ))
        observed = _form_observation(
            form,
            negative=negative,
            product_facts=product_facts,
        )
        if negative:
            negative_form_conditions.append(observed)
        else:
            positive_form_conditions.append(observed)
        source_clauses.append(
            f"form:{'not:' if negative else ''}{form}"
        )

    if len(positive_form_conditions) > 1 and " or " in text.lower():
        positive_verdicts = {
            condition["verdict"] for condition in positive_form_conditions
        }
        if "O" in positive_verdicts:
            group_verdict = "O"
            group_reason = "canonical_form_or_match"
        elif positive_verdicts == {"X"}:
            group_verdict = "X"
            group_reason = "canonical_form_or_mismatch"
        else:
            group_verdict = "SILENCE"
            group_reason = "canonical_form_or_unanswered"
        conditions.append(_condition(
            "physical_form",
            "one_of",
            group_verdict,
            group_reason,
            " or ".join(
                condition["value"] for condition in positive_form_conditions
            ),
            ";".join(dict.fromkeys(
                condition["canonical_field"]
                for condition in positive_form_conditions
                if condition["canonical_field"]
            )),
        ))
    else:
        conditions.extend(positive_form_conditions)
    conditions.extend(negative_form_conditions)

    if re.search(r"\botherwise\s+prepared\b", text, re.IGNORECASE):
        otherwise_negative = bool(re.search(
            r"\bnot\b[^,;]{0,48}\botherwise\s+prepared\b",
            text,
            re.IGNORECASE,
        ))
        source_clauses.append(
            "processing:"
            + ("not:" if otherwise_negative else "")
            + "otherwise_prepared"
        )
        conditions.append(_otherwise_prepared_observation(
            negative=otherwise_negative,
            product_facts=product_facts,
        ))

    identity_tokens = (
        words
        - _PROCESSING
        - _PRESERVATION
        - _FORMS
        - _LEGAL_WORDS
        - _RESIDUAL_CONTEXT_WORDS
    )
    identity = _identity_observation(identity_tokens, product_facts)
    if identity is not None:
        source_clauses.append(
            "identity:" + "|".join(sorted(identity_tokens))
        )
        conditions.append(identity)

    verdicts = {condition["verdict"] for condition in conditions}
    compiled_clause_count = len(conditions)
    source_clause_count = len(source_clauses)
    coverage_complete = (
        source_clause_count > 0
        and compiled_clause_count == source_clause_count
    )
    if "X" in verdicts:
        verdict = "X"
        reason = "context_condition_violated"
    elif conditions and verdicts == {"O"} and coverage_complete:
        verdict = "O"
        reason = "all_context_conditions_confirmed"
    else:
        verdict = "SILENCE"
        reason = (
            "context_clause_coverage_incomplete"
            if not coverage_complete
            else "context_condition_unanswered"
        )
    return {
        "verdict": verdict,
        "conditions": conditions,
        "reason": reason,
        "context_scope": text,
        "authority": "canonical_context",
        "source_clauses": source_clauses,
        "source_clause_count": source_clause_count,
        "compiled_clause_count": compiled_clause_count,
        "coverage_complete": coverage_complete,
    }
