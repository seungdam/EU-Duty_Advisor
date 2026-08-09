from __future__ import annotations

import os

from bussiness_logic.classification.rules.branch_decision_evaluator import (
    EvaluateCodeDecision,
)
from bussiness_logic.classification.services import staged_classification as sc


def _no_quant(*_args, **_kwargs) -> dict:
    return {"verdict": "neutral"}


def _confirmed(code: str, **extra) -> dict:
    return {
        "code": code,
        "decision": "confirmed",
        "descr": "Named branch",
        "residual": False,
        **extra,
    }


def test_precedent_condition_cannot_confirm_a_silent_candidate() -> None:
    status, detail = EvaluateCodeDecision(
        [{
            "cond_type": "product_identity",
            "dto_field": "identity_hints.food_form",
            "op": "has_token",
            "value": '["stuffed dumpling"]',
            "source_text": "BTI:DE-1:stuffed dumpling",
            "grade": "precedent",
        }],
        {
            "identity_hints": {
                "food_form": "stuffed dumpling",
            },
        },
        frozenset(),
        [],
        _no_quant,
        canonical_closed_world=True,
    )

    assert status == "undecided"
    assert detail[0]["verdict"] == "skipped"
    assert detail[0]["why"] == "precedent_reference_only"


def test_precedent_authority_cannot_be_reenabled_by_environment() -> None:
    previous = os.environ.get("ASAP_PRECEDENT_ROW_AUTHORITY")
    os.environ["ASAP_PRECEDENT_ROW_AUTHORITY"] = "1"
    try:
        status, detail = EvaluateCodeDecision(
            [{
                "cond_type": "product_identity",
                "dto_field": "identity_hints.food_form",
                "op": "has_token",
                "value": '["stuffed dumpling"]',
                "source_text": "BTI:DE-1:stuffed dumpling",
                "grade": "precedent",
            }],
            {"identity_hints": {"food_form": "stuffed dumpling"}},
            frozenset(),
            [],
            _no_quant,
            canonical_closed_world=True,
        )
    finally:
        if previous is None:
            os.environ.pop("ASAP_PRECEDENT_ROW_AUTHORITY", None)
        else:
            os.environ["ASAP_PRECEDENT_ROW_AUTHORITY"] = previous

    assert status == "undecided"
    assert detail[0]["why"] == "precedent_reference_only"


def test_stall_bti_is_exposed_as_reference_only() -> None:
    questions = sc.StagedClassificationTool._question_options(
        [{
            "code": "190211",
            "descr": "Containing eggs",
            "decision": "undecided",
            "decision_detail": [{
                "cond": "material_composition",
                "op": "contains",
                "verdict": "silent",
                "field": "composition_facts.ingredient_classes",
                "value": '["egg"]',
            }],
        }],
        level="hs6",
        parents=["1902"],
        bti_summons=[{
            "level": "hs6",
            "code": "190219",
            "refs": ["BTI-1", "BTI-2"],
            "summoned_by": "BTI:BTI-1,BTI-2",
        }],
    )

    assert len(questions) == 1
    assert questions[0]["candidate_code"] == "190211"
    assert questions[0]["bti_evidence"][0]["authority"] == "reference_only"
    assert questions[0]["bti_evidence"][0]["code"] == "190219"


def test_late_axis_summary_does_not_duplicate_compiled_question() -> None:
    questions = sc.StagedClassificationTool._question_options(
        [{
            "code": "1604",
            "descr": "Prepared or preserved fish",
            "decision": "undecided",
            "decision_detail": [{
                "cond": "species_source",
                "op": "has_token",
                "verdict": "silent",
                "field": "composition_facts.principal_ingredient",
                "value": '["fish"]',
            }, {
                "cond": "species_source",
                "op": "axis_verdict",
                "verdict": "silent",
                "field": "composition_facts.principal_ingredient",
                "value": "Prepared or preserved fish",
            }],
        }],
        level="hs4",
        parents=["16"],
        bti_summons=[],
    )

    assert len(questions) == 1
    assert questions[0]["predicate_op"] == "has_token"
    assert questions[0]["condition_value"] == '["fish"]'


def test_distinct_conditions_on_the_same_axis_remain_separate_questions() -> None:
    questions = sc.StagedClassificationTool._question_options(
        [{
            "code": "190211",
            "descr": "Containing eggs and milk",
            "decision": "undecided",
            "decision_detail": [{
                "cond": "material_composition",
                "op": "has_token",
                "verdict": "silent",
                "field": "composition_facts.ingredient_classes",
                "value": '["egg"]',
            }, {
                "cond": "material_composition",
                "op": "has_token",
                "verdict": "silent",
                "field": "composition_facts.ingredient_classes",
                "value": '["milk"]',
            }],
        }],
        level="hs6",
        parents=["1902"],
        bti_summons=[],
    )

    assert len(questions) == 2
    assert {question["condition_value"] for question in questions} == {
        '["egg"]',
        '["milk"]',
    }


def test_bti_does_not_run_without_multiple_confirmed_candidates() -> None:
    previous = sc._BTI_RECALL_CACHE
    sc._BTI_RECALL_CACHE = {
        "cases": [{
            "ref": "BTI-1",
            "cn8": "19022099",
            "toks": ["stuffed", "dumpling"],
            "phrases": ["stuffed dumpling"],
        }, {
            "ref": "BTI-2",
            "cn8": "19022099",
            "toks": ["stuffed", "dumpling"],
            "phrases": ["stuffed dumpling"],
        }],
        "df": {"stuffed": 2, "dumpling": 2},
        "n_docs": 2,
        "avg_len": 2.0,
    }
    ranked = [
        _confirmed("1902"),
        {
            "code": "2104",
            "decision": "undecided",
            "descr": "Soups and broths",
            "residual": False,
        },
    ]
    try:
        losers = sc._ApplyGri3(
            ranked,
            {
                "identity_hints": {
                    "normalized_tariff_description": "stuffed dumpling",
                },
            },
        )
    finally:
        sc._BTI_RECALL_CACHE = previous

    assert losers == []
    assert all("gri3" not in row for row in ranked)
    assert all("gri_bti" not in row for row in ranked)


def test_unconfirmed_context_is_not_eligible_for_gri() -> None:
    ranked = [
        _confirmed(
            "190211",
            context_scope="Uncooked pasta, not stuffed",
            context_decision="undecided",
        ),
        _confirmed("190230"),
    ]

    losers = sc._ApplyGri6(ranked, {}, prefix_len=6)

    assert losers == []
    assert all("gri3" not in row for row in ranked)


def test_gri6_never_compares_candidates_from_different_direct_parents() -> None:
    ranked = [
        _confirmed("190211"),
        _confirmed("190219"),
        _confirmed("210410"),
        _confirmed("210420"),
    ]

    sc._ApplyGri6(ranked, {}, prefix_len=6)

    assert {row["code"] for row in ranked[:2]} == {"190211", "190219"}
    assert {row["code"] for row in ranked[2:]} == {"210410", "210420"}
    assert ranked[0]["gri3"] == "gri6c_last_numeric"
    assert ranked[2]["gri3"] == "gri6c_last_numeric"
