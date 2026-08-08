from __future__ import annotations

import pytest

from bussiness_logic.classification.rules.branch_decision_evaluator import (
    EvaluateCodeDecision,
)
from bussiness_logic.product.components.product_understanding import (
    ProductUnderstandingComponent,
)


def _processing_decision(
    expected_state: str,
    actual_state: str,
) -> tuple[str, list[dict[str, str]]]:
    return EvaluateCodeDecision(
        [{
            "cond_type": "processing_method",
            "op": "has_token",
            "value": f'["{expected_state}"]',
        }],
        {
            "identity_hints": {"processing_state": actual_state},
            "composition_facts": {"processing_state": actual_state},
        },
        frozenset(),
        [],
        lambda *_: {},
        canonical_closed_world=True,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("가열하여 섭취하는 냉동식품", "frozen requires_cooking"),
        ("Cook before eating. Keep frozen.", "frozen requires_cooking"),
        ("볶음면", "cooked"),
        ("uncooked pasta", "uncooked"),
        ("조리방법: 끓는 물에 4분", "unknown"),
    ],
)
def test_processing_state_keeps_required_cooking_distinct(
    text: str,
    expected: str,
) -> None:
    assert ProductUnderstandingComponent._BuildProcessingState(
        factTexts=(text,),
    ) == expected


def test_requires_cooking_answers_uncooked_question() -> None:
    status, detail = _processing_decision("uncooked", "requires_cooking")

    assert status == "confirmed"
    assert detail[0]["verdict"] == "true"
    assert detail[0]["why"] == "processing_state_equivalent"


def test_requires_cooking_violates_cooked_question() -> None:
    status, detail = _processing_decision("cooked", "requires_cooking")

    assert status == "violated"
    assert detail[0]["verdict"] == "false"
    assert detail[0]["why"] == "processing_state_opposite"


def test_prepared_does_not_answer_uncooked_question() -> None:
    status, detail = _processing_decision("uncooked", "prepared")

    assert status == "undecided"
    assert detail[0]["verdict"] == "undecided"
    assert detail[0]["why"] == "processing_state_unresolved"
