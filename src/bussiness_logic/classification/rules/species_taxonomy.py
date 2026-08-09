"""Exact, upward-only taxonomy reasoning for species classification axes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Mapping

from bussiness_logic.core.runtime_asset_repository import LoadSingletonAsset

_TOKEN = re.compile(r"[a-z0-9]+")


def _normalize(value: Any) -> str:
    return " ".join(_TOKEN.findall(str(value or "").lower()))


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        preferred = [
            value.get("ingredient_name"),
            value.get("name"),
            value.get("scientific_name"),
            value.get("common_name_en"),
        ]
        return [str(item) for item in preferred if str(item or "").strip()]
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_texts(item))
        return out
    return []


def _dig(obj: Mapping[str, Any] | None, path: str) -> Any:
    current: Any = obj
    for key in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


@dataclass(frozen=True, slots=True)
class TaxonomyVerdict:
    verdict: str
    reason: str
    questionConcepts: tuple[str, ...] = ()
    factConcepts: tuple[str, ...] = ()
    unknownQuestionTerms: tuple[str, ...] = ()
    unknownFactTerms: tuple[str, ...] = ()

    def ToTrace(self) -> dict[str, str]:
        return {
            "taxonomy_verdict": self.verdict,
            "taxonomy_reason": self.reason,
            "taxonomy_question_concepts": ",".join(self.questionConcepts),
            "taxonomy_fact_concepts": ",".join(self.factConcepts),
            "taxonomy_unknown_question_terms": " | ".join(
                self.unknownQuestionTerms
            )[:160],
            "taxonomy_unknown_fact_terms": " | ".join(
                self.unknownFactTerms
            )[:160],
        }


class ExactSpeciesTaxonomy:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        concepts = [
            dict(item)
            for item in (payload.get("exact_concepts") or [])
            if isinstance(item, Mapping) and str(item.get("id") or "").strip()
        ]
        self.parents = {
            str(item["id"]): tuple(
                str(parent)
                for parent in (item.get("parents") or [])
                if str(parent)
            )
            for item in concepts
        }
        self.labels = {
            str(item["id"]): tuple(
                _normalize(label)
                for label in (item.get("labels") or [])
                if _normalize(label)
            )
            for item in concepts
        }
        self.byLabel: dict[str, str] = {}
        for concept, labels in self.labels.items():
            self.byLabel[_normalize(concept)] = concept
            for label in labels:
                prior = self.byLabel.get(label)
                if prior is not None and prior != concept:
                    raise ValueError(
                        f"taxonomy label {label!r} belongs to two concepts"
                    )
                self.byLabel[label] = concept
        self._ancestorCache: dict[str, frozenset[str]] = {}

    def Ancestors(self, concept: str) -> frozenset[str]:
        cached = self._ancestorCache.get(concept)
        if cached is not None:
            return cached
        seen: set[str] = set()
        pending = list(self.parents.get(concept, ()))
        while pending:
            parent = pending.pop()
            if parent in seen:
                continue
            seen.add(parent)
            pending.extend(self.parents.get(parent, ()))
        result = frozenset(seen)
        self._ancestorCache[concept] = result
        return result

    def MostSpecific(self, concepts: Iterable[str]) -> set[str]:
        """Drop a concept when a detected descendant carries more precision."""
        selected = set(concepts)
        return {
            concept
            for concept in selected
            if not any(
                concept in self.Ancestors(other)
                for other in selected
                if other != concept
            )
        }

    def Resolve(self, values: Iterable[Any]) -> tuple[set[str], list[str]]:
        concepts: set[str] = set()
        unknown: list[str] = []
        labels = sorted(self.byLabel, key=lambda item: (-len(item.split()), -len(item)))
        for raw in values:
            normalized = _normalize(raw)
            if not normalized:
                continue
            padded = f" {normalized} "
            matched = {
                self.byLabel[label]
                for label in labels
                if f" {label} " in padded
            }
            if matched:
                concepts.update(matched)
            else:
                unknown.append(normalized)
        return concepts, list(dict.fromkeys(unknown))

    def Expand(self, values: Iterable[Any]) -> list[str]:
        concepts, _ = self.Resolve(values)
        expanded = set(concepts)
        for concept in tuple(concepts):
            expanded.update(self.Ancestors(concept))
        labels: list[str] = []
        for concept in sorted(expanded):
            conceptLabels = self.labels.get(concept) or ()
            labels.append(conceptLabels[0] if conceptLabels else concept)
        return labels

    def Evaluate(
        self,
        questionValues: Iterable[Any],
        factValues: Iterable[Any],
    ) -> TaxonomyVerdict:
        questionTexts = [
            text for value in questionValues for text in _texts(value)
        ]
        factTexts = [text for value in factValues for text in _texts(value)]
        questionConcepts, unknownQuestions = self.Resolve(questionTexts)
        factConcepts, unknownFacts = self.Resolve(factTexts)
        questionConcepts = self.MostSpecific(questionConcepts)
        factConcepts = self.MostSpecific(factConcepts)

        # Exact phrase equality is still a valid named-species answer even
        # when the curated hierarchy has not learned that species yet.
        questionNormalized = {_normalize(value) for value in questionTexts}
        factNormalized = {_normalize(value) for value in factTexts}
        if (questionNormalized - {""}) & (factNormalized - {""}):
            return TaxonomyVerdict(
                "O",
                "exact_phrase",
                tuple(sorted(questionConcepts)),
                tuple(sorted(factConcepts)),
                tuple(unknownQuestions),
                tuple(unknownFacts),
            )
        if not questionConcepts or not factConcepts:
            return TaxonomyVerdict(
                "SILENCE",
                "taxonomy_term_unresolved",
                tuple(sorted(questionConcepts)),
                tuple(sorted(factConcepts)),
                tuple(unknownQuestions),
                tuple(unknownFacts),
            )

        for fact in factConcepts:
            factClosure = {fact, *self.Ancestors(fact)}
            if questionConcepts & factClosure:
                return TaxonomyVerdict(
                    "O",
                    "fact_is_question_or_descendant",
                    tuple(sorted(questionConcepts)),
                    tuple(sorted(factConcepts)),
                    tuple(unknownQuestions),
                    tuple(unknownFacts),
                )

        # A broad fact cannot disprove a narrower question. "crustacean"
        # therefore cannot answer whether the product is specifically shrimp.
        if any(
            fact in self.Ancestors(question)
            for fact in factConcepts
            for question in questionConcepts
        ):
            return TaxonomyVerdict(
                "SILENCE",
                "fact_is_only_question_ancestor",
                tuple(sorted(questionConcepts)),
                tuple(sorted(factConcepts)),
                tuple(unknownQuestions),
                tuple(unknownFacts),
            )

        if unknownFacts:
            return TaxonomyVerdict(
                "SILENCE",
                "partially_unresolved_fact_taxonomy",
                tuple(sorted(questionConcepts)),
                tuple(sorted(factConcepts)),
                tuple(unknownQuestions),
                tuple(unknownFacts),
            )
        return TaxonomyVerdict(
            "X",
            "authoritative_taxa_disjoint",
            tuple(sorted(questionConcepts)),
            tuple(sorted(factConcepts)),
            tuple(unknownQuestions),
            tuple(unknownFacts),
        )


@lru_cache(maxsize=1)
def GetExactSpeciesTaxonomy() -> ExactSpeciesTaxonomy:
    payload = LoadSingletonAsset("species_taxonomy")
    return ExactSpeciesTaxonomy(payload)


def ExpandExactTaxonomy(terms: Iterable[Any]) -> list[str]:
    return GetExactSpeciesTaxonomy().Expand(terms)


def EvaluateSpeciesQuestion(
    questionValues: Iterable[Any],
    productFacts: Mapping[str, Any] | None,
    paths: Iterable[str],
) -> TaxonomyVerdict:
    factValues = [
        _dig(productFacts, path)
        for path in paths
        if str(path or "").strip()
    ]
    return GetExactSpeciesTaxonomy().Evaluate(questionValues, factValues)
