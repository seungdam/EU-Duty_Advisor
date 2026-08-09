from __future__ import annotations

from bussiness_logic.classification.rules.branch_context_evaluator import (
    ObserveBranchContext,
)
from bussiness_logic.classification.services.staged_classification import (
    StagedClassificationTool,
)


def _noodle_facts(
    *,
    explicit_not_stuffed: bool,
    cooked: bool = False,
    otherwise_prepared: bool | None = False,
) -> dict:
    form_terms = ["cut noodles"]
    if explicit_not_stuffed:
        form_terms.append("not stuffed")
    state = "cooked" if cooked else "uncooked"
    component_state = state
    if otherwise_prepared is True:
        component_state += "; otherwise prepared"
    elif otherwise_prepared is False:
        component_state += "; not otherwise prepared"
    else:
        component_state += "; preparation detail unknown"
    return {
        "identity_hints": {
            "commercial_identity": "Korean wheat noodles",
            "food_form": "noodles",
            "identity_terms": ["pasta", "wheat noodles"],
            "processing_state": state,
            "physical_form": "flat cut noodles",
            "product_form_terms": form_terms,
            "preservation_state": "chilled",
        },
        "composition_facts": {
            "processing_state": component_state,
            "physical_form": "flat cut noodles",
            "preservation_state": "chilled",
            "contains_wrapper_or_dough": False,
        },
    }


def test_uncooked_non_stuffed_context_is_observed_as_o() -> None:
    observed = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(explicit_not_stuffed=True),
    )

    assert observed["verdict"] == "O"
    assert {item["axis"] for item in observed["conditions"]} == {
        "processing_method",
        "physical_form",
        "product_identity",
    }
    assert observed["source_clause_count"] == 4
    assert observed["compiled_clause_count"] == 4
    assert observed["coverage_complete"] is True


def test_missing_otherwise_prepared_fact_keeps_context_silent() -> None:
    observed = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(
            explicit_not_stuffed=True,
            otherwise_prepared=None,
        ),
    )

    assert observed["verdict"] == "SILENCE"
    preparation = next(
        item for item in observed["conditions"]
        if item["value"] == "otherwise prepared"
    )
    assert preparation["expected"] == "not"
    assert preparation["verdict"] == "SILENCE"
    assert preparation["reason"] == "otherwise_prepared_unanswered"


def test_exact_canonical_uncooked_state_does_not_answer_otherwise_prepared() -> None:
    facts = _noodle_facts(
        explicit_not_stuffed=True,
        otherwise_prepared=None,
    )
    facts["composition_facts"]["processing_state"] = "uncooked"
    facts["identity_hints"]["product_form_terms"].append("uncooked")

    observed = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        facts,
    )

    assert observed["verdict"] == "SILENCE"
    preparation = next(
        item for item in observed["conditions"]
        if item["value"] == "otherwise prepared"
    )
    assert preparation["verdict"] == "SILENCE"
    assert preparation["reason"] == "otherwise_prepared_unanswered"

    status, detail = StagedClassificationTool._consume_context_observation(
        observed,
    )
    questions = StagedClassificationTool._question_options(
        [{
            "code": "190219",
            "descr": "Other",
            "decision": "",
            "decision_detail": [],
            "predicate_results": [],
            "residual": True,
            "context_scope": observed["context_scope"],
            "context_decision": status,
            "context_detail": detail,
        }],
        level="hs6",
        parents=["1902"],
        bti_summons=[],
    )
    assert len(questions) == 1
    assert questions[0]["condition_value"] == '["otherwise prepared"]'


def test_otherwise_prepared_product_violates_plain_uncooked_context() -> None:
    observed = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(
            explicit_not_stuffed=True,
            otherwise_prepared=True,
        ),
    )

    assert observed["verdict"] == "X"
    preparation = next(
        item for item in observed["conditions"]
        if item["value"] == "otherwise prepared"
    )
    assert preparation["verdict"] == "X"


def test_missing_non_stuffed_fact_keeps_context_silent() -> None:
    observed = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(explicit_not_stuffed=False),
    )

    assert observed["verdict"] == "SILENCE"
    form = next(
        item for item in observed["conditions"]
        if item["axis"] == "physical_form"
    )
    assert form["verdict"] == "SILENCE"
    assert form["canonical_field"] == (
        "composition_facts.contains_wrapper_or_dough"
    )


def test_cooked_product_violates_uncooked_context() -> None:
    observed = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(explicit_not_stuffed=True, cooked=True),
    )

    assert observed["verdict"] == "X"
    processing = next(
        item for item in observed["conditions"]
        if item["axis"] == "processing_method"
    )
    assert processing["verdict"] == "X"


def test_context_authority_selects_only_the_local_residual() -> None:
    observation = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(explicit_not_stuffed=True),
    )
    status, detail = StagedClassificationTool._consume_context_observation(
        observation,
    )
    ranked = [
        {
            "code": "190211",
            "decision": "violated",
            "residual": False,
            "context_scope": observation["context_scope"],
            "context_decision": status,
            "context_detail": detail,
        },
        {
            "code": "190219",
            "decision": "",
            "residual": True,
            "context_scope": observation["context_scope"],
            "context_decision": status,
            "context_detail": detail,
        },
        {
            "code": "190220",
            "decision": "violated",
            "residual": False,
            "context_scope": "",
            "context_decision": "",
        },
        {
            "code": "190230",
            "decision": "",
            "residual": True,
            "context_scope": "",
            "context_decision": "",
        },
        {
            "code": "190240",
            "decision": "violated",
            "residual": False,
            "context_scope": "",
            "context_decision": "",
        },
    ]

    assert StagedClassificationTool._authoritative_selection(ranked) == (
        "190219",
        "context_residual_elimination",
    )


def test_silent_context_cannot_select_a_residual() -> None:
    observation = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(explicit_not_stuffed=False),
    )
    status, detail = StagedClassificationTool._consume_context_observation(
        observation,
    )
    ranked = [{
        "code": "190219",
        "decision": "",
        "residual": True,
        "context_scope": observation["context_scope"],
        "context_decision": status,
        "context_detail": detail,
    }]

    assert status == "undecided"
    assert StagedClassificationTool._authoritative_selection(ranked) == (
        "",
        "none",
    )


def test_open_context_blocks_unscoped_parent_residual() -> None:
    observation = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(
            explicit_not_stuffed=True,
            otherwise_prepared=None,
        ),
    )
    status, detail = StagedClassificationTool._consume_context_observation(
        observation,
    )
    ranked = [
        {
            "code": "190211",
            "decision": "violated",
            "residual": False,
            "context_scope": observation["context_scope"],
            "context_decision": status,
            "context_detail": detail,
        },
        {
            "code": "190219",
            "decision": "",
            "residual": True,
            "context_scope": observation["context_scope"],
            "context_decision": status,
            "context_detail": detail,
        },
        {
            "code": "190220",
            "decision": "violated",
            "residual": False,
            "context_scope": "",
            "context_decision": "",
        },
        {
            "code": "190230",
            "decision": "",
            "residual": True,
            "context_scope": "",
            "context_decision": "",
        },
        {
            "code": "190240",
            "decision": "violated",
            "residual": False,
            "context_scope": "",
            "context_decision": "",
        },
    ]

    assert StagedClassificationTool._authoritative_selection(ranked) == (
        "",
        "none",
    )


def test_missing_otherwise_prepared_generates_one_decisive_question() -> None:
    observation = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(
            explicit_not_stuffed=True,
            otherwise_prepared=None,
        ),
    )
    status, detail = StagedClassificationTool._consume_context_observation(
        observation,
    )
    ranked = [{
        "code": "190219",
        "descr": "Other",
        "decision": "",
        "decision_detail": [],
        "predicate_results": [],
        "residual": True,
        "context_scope": observation["context_scope"],
        "context_decision": status,
        "context_detail": detail,
    }]

    questions = StagedClassificationTool._question_options(
        ranked,
        level="hs6",
        parents=["1902"],
        bti_summons=[],
    )

    assert len(questions) == 1
    assert questions[0]["candidate_code"] == "190219"
    assert questions[0]["predicate_op"] == "not_contains"
    assert questions[0]["condition_value"] == '["otherwise prepared"]'


def test_similar_context_clauses_remain_distinct_questions() -> None:
    observation = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        _noodle_facts(
            explicit_not_stuffed=False,
            otherwise_prepared=None,
        ),
    )
    status, detail = StagedClassificationTool._consume_context_observation(
        observation,
    )
    ranked = [
        {
            "code": code,
            "descr": description,
            "decision": "",
            "decision_detail": [],
            "predicate_results": [],
            "residual": code == "190219",
            "context_scope": observation["context_scope"],
            "context_decision": status,
            "context_detail": detail,
        }
        for code, description in (
            ("190211", "Containing eggs"),
            ("190219", "Other"),
        )
    ]

    questions = StagedClassificationTool._question_options(
        ranked,
        level="hs6",
        parents=["1902"],
        bti_summons=[],
    )

    assert len(questions) == 2
    assert {
        question["condition_value"]
        for question in questions
    } == {
        '["stuffed"]',
        '["otherwise prepared"]',
    }
    assert len({
        question["question_key"]
        for question in questions
    }) == 2


def test_one_context_answer_closes_both_suffix_leaves_and_selects_190230() -> None:
    facts = _noodle_facts(
        explicit_not_stuffed=True,
        otherwise_prepared=None,
    )
    observation = ObserveBranchContext(
        "Uncooked pasta, not stuffed or otherwise prepared",
        facts,
    )
    status, detail = StagedClassificationTool._consume_context_observation(
        observation,
    )
    ranked = [
        {
            "code": "190211",
            "descr": "Containing eggs",
            "decision": "",
            "decision_detail": [],
            "predicate_results": [],
            "residual": False,
            "context_scope": observation["context_scope"],
            "context_decision": status,
            "context_detail": detail,
        },
        {
            "code": "190219",
            "descr": "Other",
            "decision": "",
            "decision_detail": [],
            "predicate_results": [],
            "residual": True,
            "context_scope": observation["context_scope"],
            "context_decision": status,
            "context_detail": detail,
        },
        {
            "code": "190220",
            "descr": "Stuffed pasta",
            "decision": "violated",
            "decision_detail": [],
            "predicate_results": [],
            "residual": False,
            "context_scope": "",
            "context_decision": "",
            "context_detail": [],
        },
        {
            "code": "190230",
            "descr": "Other pasta",
            "decision": "",
            "decision_detail": [],
            "predicate_results": [],
            "residual": True,
            "context_scope": "",
            "context_decision": "",
            "context_detail": [],
        },
        {
            "code": "190240",
            "descr": "Couscous",
            "decision": "violated",
            "decision_detail": [],
            "predicate_results": [],
            "residual": False,
            "context_scope": "",
            "context_decision": "",
            "context_detail": [],
        },
    ]
    questions = StagedClassificationTool._question_options(
        ranked,
        level="hs6",
        parents=["1902"],
        bti_summons=[],
    )
    assert len(questions) == 1

    facts["_classification_answer_facts"] = [{
        "answer_id": "answer-1",
        "question_key": questions[0]["question_key"],
        "answer": "yes",
        "answered_at": "2026-07-26T00:00:00+09:00",
    }]
    StagedClassificationTool._apply_answer_overlays(
        ranked,
        facts,
        level="hs6",
    )

    assert ranked[0]["context_decision"] == "violated"
    assert ranked[1]["context_decision"] == "violated"
    assert StagedClassificationTool._authoritative_selection(ranked) == (
        "190230",
        "residual_elimination",
    )


def test_residual_only_context_is_not_a_product_identity_question() -> None:
    observed = ObserveBranchContext(
        "Other > Other",
        _noodle_facts(explicit_not_stuffed=False),
    )

    assert observed["verdict"] == "O"
    assert observed["conditions"][0]["reason"] == "residual_context_no_gate"


def test_fish_paste_violates_whole_or_pieces_not_minced_context() -> None:
    observed = ObserveBranchContext(
        "Fish, whole or in pieces, but not minced",
        {
            "identity_hints": {
                "commercial_identity": "vegetable fish-cake bar",
                "food_form": "fish cake",
                "identity_terms": ["fish cake", "fish paste", "surimi"],
            },
            "composition_facts": {
                "principal_ingredient": "fish paste",
                "principal_ingredient_candidates": [{
                    "ingredient_name": "fish paste",
                }],
                "physical_form": "bar of fish paste",
            },
        },
    )

    assert observed["verdict"] == "X"
    minced = next(
        item for item in observed["conditions"]
        if item["value"] == "minced"
    )
    assert minced["verdict"] == "X"
