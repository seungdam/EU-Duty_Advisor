"""Canonical decision-axis to ProductUnderstandingFacts binding.

This module is the single runtime authority for deciding which DTO facts may
answer one legal branch question.  It deliberately does not decide O/X; it
selects the canonical evidence lane and exposes exact paths, values, and
tokens to the decision evaluators.

Bindings are tiered.  A higher-authority source with usable data wins, while
an empty or sentinel-only tier falls through.  This keeps COI/reconstruction
composition facts authoritative over identity hints without turning missing
composition data into a forced mismatch.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from bussiness_logic.classification.rules.branch_predicate_evaluator import (
    _dig,
    _field_tokens,
)

_SENTINELS = frozenset({"", "unknown", "other", "none", "n/a", "na"})

_AXIS_ALIASES = {
    "species": "species_source",
    "contains": "material_composition",
    "form": "physical_form",
    "processing": "processing_method",
    "processing_state": "processing_method",
    "pct": "quantitative_threshold",
}

_BOOLEAN_REGISTER = {
    "composition_facts.contains_wrapper_or_dough": frozenset(
        {"stuffed", "filled", "wrapper", "dough"}
    ),
    "composition_facts.contains_sauce_or_broth": frozenset(
        {"sauce", "broth"}
    ),
}


@dataclass(frozen=True, slots=True)
class AxisBindingTier:
    canonicalFact: str
    sourceLane: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedAxisBinding:
    axis: str
    canonicalFact: str
    sourceLane: str
    paths: tuple[str, ...]
    tokens: frozenset[str]
    values: tuple[str, ...]
    affirmedTokens: frozenset[str]
    deniedTokens: frozenset[str]
    status: str
    tierIndex: int

    @property
    def dtoField(self) -> str:
        return ";".join(self.paths)

    def ToTrace(self) -> dict[str, str]:
        return {
            "binding_axis": self.axis,
            "binding_fact": self.canonicalFact,
            "binding_lane": self.sourceLane,
            "binding_paths": self.dtoField,
            "binding_status": self.status,
            "binding_tier": str(self.tierIndex),
            "binding_values": " | ".join(self.values)[:240],
        }


# One axis may have several concrete DTO paths when those paths are alternate
# representations of the same canonical fact.  Separate tuples are authority
# tiers, not pools to union indiscriminately.
_AXIS_BINDING_TIERS: dict[str, tuple[AxisBindingTier, ...]] = {
    "product_identity": (
        AxisBindingTier(
            "commodity_identity",
            "identity_hints",
            (
                "identity_hints.commercial_identity",
                "identity_hints.food_form",
                "identity_hints.identity_terms",
            ),
        ),
    ),
    "identity_fallback": (
        AxisBindingTier(
            "commodity_identity",
            "identity_hints",
            (
                "identity_hints.commercial_identity",
                "identity_hints.food_form",
                "identity_hints.identity_terms",
            ),
        ),
    ),
    "species_source": (
        AxisBindingTier(
            "principal_taxonomy",
            "composition_facts",
            (
                "composition_facts.principal_ingredient",
                "composition_facts.principal_ingredient_candidates",
            ),
        ),
        AxisBindingTier(
            "principal_taxonomy",
            "identity_hints",
            (
                "identity_hints.principal_ingredient_guess",
                "identity_hints.ingredient_class",
            ),
        ),
    ),
    "material_composition": (
        AxisBindingTier(
            "material_composition",
            "composition_facts",
            (
                "composition_facts.principal_ingredient",
                "composition_facts.ingredient_classes",
                "composition_facts.ingredient_entries",
                "composition_facts.composition_terms",
            ),
        ),
    ),
    "physical_form": (
        AxisBindingTier(
            "physical_form",
            "composition_facts",
            ("composition_facts.physical_form",),
        ),
        AxisBindingTier(
            "physical_form_boolean",
            "composition_facts",
            (
                "composition_facts.contains_wrapper_or_dough",
                "composition_facts.contains_sauce_or_broth",
            ),
        ),
        AxisBindingTier(
            "physical_form",
            "identity_hints",
            (
                "identity_hints.physical_form",
                "identity_hints.food_form",
                "identity_hints.product_form_terms",
            ),
        ),
    ),
    "preservation_state": (
        AxisBindingTier(
            "preservation_state",
            "composition_facts",
            ("composition_facts.preservation_state",),
        ),
        AxisBindingTier(
            "preservation_state",
            "identity_hints",
            ("identity_hints.preservation_state",),
        ),
    ),
    "processing_method": (
        AxisBindingTier(
            "processing_state",
            "composition_facts",
            ("composition_facts.processing_state",),
        ),
        AxisBindingTier(
            "processing_state",
            "identity_hints",
            ("identity_hints.processing_state",),
        ),
    ),
    "condition_quality": (
        AxisBindingTier(
            "condition_quality",
            "composition_facts",
            (
                "composition_facts.processing_state",
                "composition_facts.preservation_state",
            ),
        ),
        AxisBindingTier(
            "condition_quality",
            "identity_hints",
            (
                "identity_hints.processing_state",
                "identity_hints.preservation_state",
            ),
        ),
    ),
    "quantitative_threshold": (
        AxisBindingTier(
            "ingredient_percentages",
            "composition_facts",
            ("composition_facts.ingredient_percentages",),
        ),
    ),
    "packaging_presentation": (
        AxisBindingTier(
            "packaging_presentation",
            "identity_hints",
            ("identity_hints.product_form_terms",),
        ),
    ),
    "intended_use_function": (
        AxisBindingTier(
            "intended_use",
            "identity_hints",
            ("identity_hints.intended_use",),
        ),
    ),
    # Exclusions may target either commodity identity or a declared material.
    # The scope remains narrow: no reconstructed OCR, encyclopedia, NTD, or
    # global token pool is admitted.
    "exclusion_boundary": (
        AxisBindingTier(
            "exclusion_fact",
            "canonical_product_facts",
            (
                "identity_hints.commercial_identity",
                "identity_hints.food_form",
                "identity_hints.identity_terms",
                "composition_facts.principal_ingredient",
                "composition_facts.ingredient_classes",
                "composition_facts.ingredient_entries",
            ),
        ),
    ),
    # Current ProductUnderstandingFacts has no authoritative typed answer for
    # these axes.  They intentionally resolve to unsupported/SILENCE.
    "demographic_target": (),
    "dimension_capacity": (),
    "parts_accessories": (),
    "residual_other": (),
    "technical_specification": (),
}


def CanonicalAxisName(axis: str) -> str:
    value = str(axis or "").strip()
    return _AXIS_ALIASES.get(value, value)


def _meaningful(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _SENTINELS
    if isinstance(value, Mapping):
        return any(_meaningful(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_meaningful(v) for v in value)
    return True


def _summarize(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _relevant_paths(
    paths: tuple[str, ...],
    questionTokens: frozenset[str],
) -> tuple[str, ...]:
    if not questionTokens:
        return paths
    selected: list[str] = []
    for path in paths:
        register = _BOOLEAN_REGISTER.get(path)
        if register is not None and not (register & questionTokens):
            continue
        selected.append(path)
    return tuple(selected)


def ResolveAxisFieldBinding(
    axis: str,
    productFacts: Mapping[str, Any] | None,
    *,
    questionTokens: frozenset[str] | set[str] = frozenset(),
) -> ResolvedAxisBinding:
    """Resolve one legal axis to one populated canonical fact tier."""
    canonical_axis = CanonicalAxisName(axis)
    tiers = _AXIS_BINDING_TIERS.get(canonical_axis)
    if tiers is None or not tiers:
        return ResolvedAxisBinding(
            axis=canonical_axis,
            canonicalFact="",
            sourceLane="",
            paths=(),
            tokens=frozenset(),
            values=(),
            affirmedTokens=frozenset(),
            deniedTokens=frozenset(),
            status="unsupported",
            tierIndex=-1,
        )

    questions = frozenset(str(token).strip().lower() for token in questionTokens)

    # A dedicated boolean register is more specific than a populated free-text
    # form. For "stuffed?", the wrapper field is therefore the canonical
    # binding even when ``physical_form`` also contains "thick noodles".
    # False remains an answer value, but the polarity evaluator still decides
    # whether the available provenance is strong enough to conclude X.
    boolean_paths = tuple(
        path
        for tier in tiers
        for path in tier.paths
        if (
            (register := _BOOLEAN_REGISTER.get(path)) is not None
            and bool(register & questions)
            and _meaningful(_dig(productFacts, path))
        )
    )
    if boolean_paths:
        tokens: set[str] = set()
        affirmed: set[str] = set()
        denied: set[str] = set()
        for path in boolean_paths:
            register = _BOOLEAN_REGISTER[path]
            value = _dig(productFacts, path)
            if value is True:
                affirmed.update(register)
                tokens.update(register)
            elif value is False:
                denied.update(register)
        return ResolvedAxisBinding(
            axis=canonical_axis,
            canonicalFact="physical_form_boolean",
            sourceLane="composition_facts",
            paths=boolean_paths,
            tokens=frozenset(tokens),
            values=tuple(
                f"{path}={_summarize(_dig(productFacts, path))}"
                for path in boolean_paths
            ),
            affirmedTokens=frozenset(affirmed),
            deniedTokens=frozenset(denied),
            status="answered",
            tierIndex=0,
        )

    fallback_paths = _relevant_paths(tiers[0].paths, questions)
    for index, tier in enumerate(tiers):
        paths = _relevant_paths(tier.paths, questions)
        populated = tuple(
            path for path in paths if _meaningful(_dig(productFacts, path))
        )
        if not populated:
            continue
        dto_field = ";".join(populated)
        tokens = set(_field_tokens(productFacts, dto_field))
        affirmed: set[str] = set()
        denied: set[str] = set()
        for path in populated:
            register = _BOOLEAN_REGISTER.get(path)
            if register is None:
                continue
            if _dig(productFacts, path) is True:
                affirmed.update(register)
                tokens.update(register)
            elif _dig(productFacts, path) is False:
                denied.update(register)
        return ResolvedAxisBinding(
            axis=canonical_axis,
            canonicalFact=tier.canonicalFact,
            sourceLane=tier.sourceLane,
            paths=populated,
            tokens=frozenset(tokens),
            values=tuple(
                f"{path}={_summarize(_dig(productFacts, path))[:120]}"
                for path in populated
            ),
            affirmedTokens=frozenset(affirmed),
            deniedTokens=frozenset(denied),
            status="answered",
            tierIndex=index,
        )

    return ResolvedAxisBinding(
        axis=canonical_axis,
        canonicalFact=tiers[0].canonicalFact,
        sourceLane=tiers[0].sourceLane,
        paths=fallback_paths,
        tokens=frozenset(),
        values=(),
        affirmedTokens=frozenset(),
        deniedTokens=frozenset(),
        status="empty",
        tierIndex=0,
    )


def CompilerFieldBindings() -> dict[str, str]:
    """Flatten canonical path specs for deterministic offline compilation.

    Runtime still selects one populated tier via ``ResolveAxisFieldBinding``.
    The flattened compiler value preserves all legal fallback paths in the
    sidecar without creating a second binding registry.
    """
    out: dict[str, str] = {}
    for axis, tiers in _AXIS_BINDING_TIERS.items():
        paths: list[str] = []
        for tier in tiers:
            for path in tier.paths:
                if path not in paths:
                    paths.append(path)
        out[axis] = ";".join(paths)
    for alias, canonical in _AXIS_ALIASES.items():
        out[alias] = out.get(canonical, "")
    return out
