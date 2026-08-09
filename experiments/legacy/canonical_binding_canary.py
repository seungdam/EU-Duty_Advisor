"""Legacy exploration; excluded from the CI pytest suite.

It depends on the removed ``DB.experiment_canonical_binding_canary`` prototype.
"""

from __future__ import annotations

import pytest

from DB import experiment_canonical_binding_canary as experiment
from bussiness_logic.classification.rules import species_taxonomy


@pytest.fixture()
def taxonomy(monkeypatch):
    value = species_taxonomy.ExactSpeciesTaxonomy({
        "exact_concepts": [
            {"id": "aquatic_invertebrate", "labels": [
                "aquatic invertebrate",
            ], "parents": []},
            {"id": "mollusc", "labels": ["mollusc"], "parents": [
                "aquatic_invertebrate",
            ]},
            {"id": "crustacean", "labels": ["crustacean"], "parents": [
                "aquatic_invertebrate",
            ]},
            {"id": "shrimp", "labels": ["shrimp"], "parents": [
                "crustacean",
            ]},
            {"id": "fish", "labels": ["fish"], "parents": []},
            {"id": "cod", "labels": ["cod"], "parents": ["fish"]},
            {"id": "meat", "labels": ["meat"], "parents": []},
            {"id": "pork", "labels": ["pork"], "parents": ["meat"]},
        ],
    })
    monkeypatch.setattr(
        species_taxonomy,
        "GetExactSpeciesTaxonomy",
        lambda: value,
    )
    return value


def _evaluate(question: str, facts: dict):
    evaluator = experiment._experimental_species_evaluator(
        species_taxonomy.EvaluateSpeciesQuestion
    )
    return evaluator(
        [question],
        facts,
        (
            "composition_facts.principal_ingredient",
            "composition_facts.principal_ingredient_candidates",
        ),
    )


def test_unique_fish_class_answers_unresolved_principal_taxonomy(taxonomy):
    result = _evaluate("prepared fish", {
        "composition_facts": {
            "principal_ingredient_candidates": [{
                "ingredient_name": "원재료 표시 문구",
            }],
            "ingredient_classes": ["fish", "vegetable"],
        },
    })

    assert result.verdict == "O"
    assert result.reason.startswith("unique_composition_family_fallback:")


def test_unique_crustacean_class_excludes_fish_and_confirms_crustacean(
    taxonomy,
):
    facts = {
        "composition_facts": {
            "principal_ingredient_candidates": [{
                "ingredient_name": "새우살",
            }],
            "ingredient_classes": ["crustacean", "soy_legume"],
        },
    }

    assert _evaluate("prepared fish", facts).verdict == "X"
    assert _evaluate("prepared crustacean or mollusc", facts).verdict == "O"


def test_mixed_biological_families_do_not_create_principal_authority(taxonomy):
    result = _evaluate("prepared fish", {
        "composition_facts": {
            "principal_ingredient_candidates": [{
                "ingredient_name": "mixed food",
            }],
            "ingredient_classes": ["fish", "mollusc", "meat"],
        },
    })

    assert result.verdict == "SILENCE"
    assert result.reason == "taxonomy_term_unresolved"


def test_explicit_principal_taxonomy_remains_higher_authority(taxonomy):
    result = _evaluate("fish", {
        "composition_facts": {
            "principal_ingredient": "cod",
            "ingredient_classes": ["crustacean"],
        },
    })

    assert result.verdict == "O"
    assert result.reason == "fact_is_question_or_descendant"


def test_stuffed_context_and_candidate_share_one_logical_observation():
    context_question = {
        "stage": "hs6",
        "candidate_code": "190219",
        "axis": "physical_form",
        "predicate_op": "not_contains",
        "condition_value": '["stuffed"]',
    }
    candidate_question = {
        "stage": "hs6",
        "candidate_code": "190220",
        "axis": "processing_method",
        "predicate_op": "axis_verdict",
        "description": (
            "Stuffed pasta, whether or not cooked or otherwise prepared"
        ),
    }

    assert (
        experiment._logical_question_key(context_question)
        == experiment._logical_question_key(candidate_question)
        == "physical_form:stuffed"
    )
