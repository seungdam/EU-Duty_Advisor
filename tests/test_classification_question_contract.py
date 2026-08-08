from bussiness_logic.classification.rules.question_contract import (
    ApplyClassificationAnswers,
    BuildClassificationQuestionKey,
)


def _detail(value: str = '["egg"]', op: str = "contains") -> dict:
    return {
        "cond": "material_composition",
        "op": op,
        "verdict": "silent",
        "field": "composition_facts.ingredient_classes",
        "value": value,
        "why": "field_empty",
    }


def test_question_key_is_stable_and_scope_sensitive() -> None:
    common = {
        "stage": "hs6",
        "parentCode": "1902",
        "candidateCode": "190211",
        "axis": "material_composition",
        "canonicalField": "composition_facts.ingredient_classes",
        "conditionValue": '["egg"]',
        "predicateOp": "contains",
    }
    first = BuildClassificationQuestionKey(**common)
    assert first == BuildClassificationQuestionKey(**common)
    assert first != BuildClassificationQuestionKey(
        **{**common, "candidateCode": "190219"}
    )


def test_yes_answer_confirms_exact_question() -> None:
    detail = _detail()
    key = BuildClassificationQuestionKey(
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
        axis="material_composition",
        canonicalField="composition_facts.ingredient_classes",
        conditionValue='["egg"]',
        predicateOp="contains",
    )
    status, details, keys = ApplyClassificationAnswers(
        decisionStatus="undecided",
        decisionDetail=[detail],
        productFacts={
            "_classification_answer_facts": [{
                "question_key": key,
                "answer": "yes",
                "answer_id": "qa_001",
                "answered_at": "2026-07-24T00:00:00+09:00",
            }]
        },
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
    )
    assert status == "confirmed"
    assert keys == [key]
    assert details[-1]["op"] == "user_answer"
    assert details[-1]["verdict"] == "true"


def test_answer_cannot_leak_to_sibling() -> None:
    key = BuildClassificationQuestionKey(
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
        axis="material_composition",
        canonicalField="composition_facts.ingredient_classes",
        conditionValue='["egg"]',
        predicateOp="contains",
    )
    status, details, keys = ApplyClassificationAnswers(
        decisionStatus="undecided",
        decisionDetail=[_detail()],
        productFacts={
            "_classification_answer_facts": [{
                "question_key": key,
                "answer": "yes",
            }]
        },
        stage="hs6",
        parentCode="1902",
        candidateCode="190219",
    )
    assert status == "undecided"
    assert keys == []
    assert all(item.get("op") != "user_answer" for item in details)


def test_unknown_answer_remains_silence() -> None:
    detail = _detail()
    key = BuildClassificationQuestionKey(
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
        axis="material_composition",
        canonicalField="composition_facts.ingredient_classes",
        conditionValue='["egg"]',
        predicateOp="contains",
    )
    status, details, keys = ApplyClassificationAnswers(
        decisionStatus="undecided",
        decisionDetail=[detail],
        productFacts={
            "_classification_answer_facts": [{
                "question_key": key,
                "answer": "unknown",
            }]
        },
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
    )
    assert status == "undecided"
    assert keys == [key]
    assert details[-1]["verdict"] == "silent"


def test_question_key_distinguishes_predicate_polarity() -> None:
    common = {
        "stage": "hs6",
        "parentCode": "1902",
        "candidateCode": "190211",
        "axis": "material_composition",
        "canonicalField": "composition_facts.ingredient_classes",
        "conditionValue": '["egg"]',
    }
    assert BuildClassificationQuestionKey(
        **common,
        predicateOp="contains",
    ) != BuildClassificationQuestionKey(
        **common,
        predicateOp="not_contains",
    )


def test_yes_to_positive_question_violates_not_contains_predicate() -> None:
    detail = _detail(op="not_contains")
    key = BuildClassificationQuestionKey(
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
        axis="material_composition",
        canonicalField="composition_facts.ingredient_classes",
        conditionValue='["egg"]',
        predicateOp="not_contains",
    )
    status, details, keys = ApplyClassificationAnswers(
        decisionStatus="undecided",
        decisionDetail=[detail],
        productFacts={
            "_classification_answer_facts": [{
                "question_key": key,
                "answer": "yes",
            }]
        },
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
    )
    assert status == "violated"
    assert keys == [key]
    assert details[-1]["verdict"] == "false"


def test_no_to_positive_question_confirms_not_contains_predicate() -> None:
    detail = _detail(op="not_contains")
    key = BuildClassificationQuestionKey(
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
        axis="material_composition",
        canonicalField="composition_facts.ingredient_classes",
        conditionValue='["egg"]',
        predicateOp="not_contains",
    )
    status, details, _ = ApplyClassificationAnswers(
        decisionStatus="undecided",
        decisionDetail=[detail],
        productFacts={
            "_classification_answer_facts": [{
                "question_key": key,
                "answer": "no",
            }]
        },
        stage="hs6",
        parentCode="1902",
        candidateCode="190211",
    )
    assert status == "confirmed"
    assert details[-1]["verdict"] == "true"


def test_answer_replay_preserves_alt_group_or_semantics() -> None:
    first = {
        **_detail(value='["uncooked"]'),
        "alt_group": "preparation",
    }
    second = {
        **_detail(value='["steamed"]'),
        "alt_group": "preparation",
    }
    facts = []
    for detail, answer, answer_id in (
        (first, "yes", "qa_001"),
        (second, "no", "qa_002"),
    ):
        facts.append({
            "question_key": BuildClassificationQuestionKey(
                stage="hs6",
                parentCode="0710",
                candidateCode="071080",
                axis="material_composition",
                canonicalField="composition_facts.ingredient_classes",
                conditionValue=detail["value"],
                predicateOp="contains",
            ),
            "answer": answer,
            "answer_id": answer_id,
        })

    status, _, keys = ApplyClassificationAnswers(
        decisionStatus="undecided",
        decisionDetail=[first, second],
        productFacts={"_classification_answer_facts": facts},
        stage="hs6",
        parentCode="0710",
        candidateCode="071080",
    )

    assert status == "confirmed"
    assert len(keys) == 2


def test_generic_not_contains_question_asks_positive_fact() -> None:
    from bussiness_logic.classification.services.staged_classification import (
        StagedClassificationTool,
    )

    questions = StagedClassificationTool._question_options(
        [{
            "code": "190211",
            "descr": "Containing eggs",
            "decision": "undecided",
            "decision_detail": [{
                "cond": "exclusion_boundary",
                "op": "not_contains",
                "verdict": "silent",
                "field": "composition_facts.ingredient_classes",
                "value": '["egg"]',
            }],
        }],
        level="hs6",
        parents=["1902"],
        bti_summons=[],
    )

    assert questions[0]["question_text"] == (
        "제품에 다음 분류 특성 또는 값이 존재합니까: egg?"
    )


def test_gri3_and_gri6_d_fallback_questions_replay_as_axis_verdicts() -> None:
    from bussiness_logic.classification.services.staged_classification import (
        StagedClassificationTool,
    )

    tool = StagedClassificationTool()
    for level, code, expected_rule in (
        ("hs4", "1902", "gri3d"),
        ("hs6", "190230", "gri6d"),
        ("cn8", "19023090", "gri6d"),
    ):
        row = {
            "code": code,
            "descr": "Other prepared pasta",
            "decision": "undecided",
            "residual": True,
            "decision_detail": [{
                "cond": "processing_method",
                "op": "axis_verdict",
                "verdict": "residual",
                "why": "axis_map:complement_only",
                "value": "Other prepared pasta",
            }],
        }
        assert tool._question_options(
            [dict(row)],
            level=level,
            parents=[code[:-2]],
            bti_summons=[],
        ) == []

        ranked = [dict(row)]
        assert tool._append_gri_d_question_details(
            ranked,
            level=level,
        ) == 1
        questions = tool._question_options(
            ranked,
            level=level,
            parents=[code[:-2]],
            bti_summons=[],
        )

        assert len(questions) == 1
        assert questions[0]["candidate_code"] == code
        assert questions[0]["predicate_op"] == "axis_verdict"
        assert questions[0]["required_facts"][0]["why"].startswith(
            f"{expected_rule}:"
        )

        replayed = [dict(row)]
        tool._apply_answer_overlays(
            replayed,
            {
                "_classification_answer_facts": [{
                    "question_key": questions[0]["question_key"],
                    "answer": "yes",
                }],
            },
            level=level,
        )
        assert replayed[0]["decision"] == "confirmed"


def test_gri_d_fallback_survives_when_every_branch_is_violated() -> None:
    from bussiness_logic.classification.services.staged_classification import (
        StagedClassificationTool,
    )

    tool = StagedClassificationTool()
    for level, codes in (
        ("hs4", ("1902", "1905")),
        ("hs6", ("190219", "190230")),
        ("cn8", ("19023010", "19023090")),
    ):
        context_scope = (
            "Prepared pasta branch"
            if level == "hs6"
            else ""
        )
        ranked = [
            {
                "code": code,
                "descr": f"Branch {code}",
                "decision": "violated",
                "residual": False,
                "decision_detail": [{
                    "cond": "product_identity",
                    "op": "has_token",
                    "verdict": "false",
                    "field": "identity_hints.commercial_identity",
                    "why": "canonical_field_mismatch",
                    "value": f'["branch {code}"]',
                }],
                "context_scope": context_scope,
                "context_decision": (
                    "violated"
                    if context_scope
                    else ""
                ),
                "context_detail": (
                    [{
                        "cond": "branch_context",
                        "op": "context_observation",
                        "verdict": "false",
                        "field": "",
                        "why": "context_observation:mismatch",
                        "value": f'["{context_scope}"]',
                    }]
                    if context_scope
                    else []
                ),
            }
            for code in codes
        ]

        assert tool._append_gri_d_question_details(
            ranked,
            level=level,
        ) == len(codes)
        questions = tool._question_options(
            ranked,
            level=level,
            parents=[codes[0][:-2]],
            bti_summons=[],
        )

        assert len(questions) == len(codes)
        assert all(
            question["required_facts"][0]["why"].startswith(
                ("gri3d:", "gri6d:"),
            )
            for question in questions
        )

        tool._apply_answer_overlays(
            ranked,
            {
                "_classification_answer_facts": [
                    {
                        "question_key": questions[0]["question_key"],
                        "answer": "yes",
                    },
                    {
                        "question_key": questions[1]["question_key"],
                        "answer": "no",
                    },
                ],
            },
            level=level,
        )
        assert ranked[0]["decision"] == "confirmed"
        if context_scope:
            assert ranked[0]["context_decision"] == "confirmed"
        assert ranked[1]["decision"] == "violated"


def test_classify_exposes_gri3_and_gri6_d_questions_when_authority_is_empty(
    monkeypatch,
) -> None:
    from bussiness_logic.classification.services import axis_verdict
    from bussiness_logic.classification.services.staged_classification import (
        StagedClassificationTool,
    )

    monkeypatch.setenv("ASAP_STAGED_DECISION_TABLE", "0")
    monkeypatch.setenv("ASAP_STAGED_PREDICATES", "0")
    monkeypatch.setenv("ASAP_BTI_RECALL", "0")
    monkeypatch.setenv("ASAP_PRECEDENT_LEAD", "0")
    monkeypatch.setattr(axis_verdict, "StampHs4AxisVerdicts", lambda *_args: 0)
    monkeypatch.setattr(axis_verdict, "StampHs6AxisVerdicts", lambda *_args: 0)
    monkeypatch.setattr(axis_verdict, "StampCn8AxisVerdicts", lambda *_args: 0)

    for level, parent, codes, expected_rule in (
        ("hs4", "19", ("1902", "1905"), "gri3d_question_required"),
        ("hs6", "1902", ("190219", "190230"), "gri6d_question_required"),
        ("cn8", "190230", ("19023010", "19023090"), "gri6d_question_required"),
    ):
        class FixtureTool(StagedClassificationTool):
            @staticmethod
            def _load_branch_rows(candidate_level, parents):
                if candidate_level != level or tuple(parents) != (parent,):
                    return ()
                return tuple(
                    {
                        "code": code,
                        "parent_code": parent,
                        "option_label_en": f"Branch {code}",
                    }
                    for code in codes
                )

            def _branch_rank(self, branch_rows, *_args, **_kwargs):
                return [
                    {
                        "code": str(row["code"]),
                        "descr": str(row["option_label_en"]),
                        "incl": "",
                        "excl": "",
                        "residual": False,
                        "score": 0.0,
                        "matched": [],
                        "neg_matched": [],
                        "predicate_results": [],
                        "decision": "undecided",
                        "decision_detail": [],
                        "context_scope": "",
                        "context_decision": "",
                        "context_detail": [],
                        "context_observation": {},
                        "quantitative_verdict": {"verdict": "neutral"},
                    }
                    for row in branch_rows
                ]

        result = FixtureTool().classify(
            product_facts={},
            routing_context={},
            start_parents=[parent],
        )

        assert result["classification_status"] == "needs_more_facts"
        assert result["unresolved_stage"] == level
        assert result["pending_user_questions"]
        assert result["stages"][-1]["decision_fallback"] == expected_rule


def test_hs6_context_uses_question_contract_without_offline_csv() -> None:
    from bussiness_logic.classification.services.staged_classification import (
        StagedClassificationTool,
    )

    tool = StagedClassificationTool()
    items = [
        {
            "code": "160551",
            "row": {
                "code": "160551",
                "parent_code": "1605",
                "branch_context": "Octopus, prepared or preserved",
                "option_label_en": "Octopus",
                "residual_other_flag": "false",
            },
        },
        {
            "code": "160552",
            "row": {
                "code": "160552",
                "parent_code": "1605",
                "branch_context": "Scallops, prepared or preserved",
                "option_label_en": "Scallops",
                "residual_other_flag": "false",
            },
        },
    ]

    ranked = tool._rank_sibling_group(
        items,
        {},
        set(),
        [],
        discrete_only=True,
        level="hs6",
    )
    questions = tool._question_options(
        ranked,
        level="hs6",
        parents=["1605"],
        bti_summons=[],
    )

    assert ranked[0]["context_decision"] == "undecided"
    assert questions[0]["question_text"] == (
        "제품이 다음 HS6 분류 범위에 해당합니까: "
        "Octopus, prepared or preserved?"
    )

    answered = tool._rank_sibling_group(
        items,
        {
            "_classification_answer_facts": [{
                "question_key": questions[0]["question_key"],
                "answer": "yes",
            }]
        },
        set(),
        [],
        discrete_only=True,
        level="hs6",
    )

    assert answered[0]["context_decision"] == "confirmed"
    assert answered[1]["context_decision"] == "undecided"

    rejected = tool._rank_sibling_group(
        items,
        {
            "_classification_answer_facts": [{
                "question_key": questions[0]["question_key"],
                "answer": "no",
            }]
        },
        set(),
        [],
        discrete_only=True,
        level="hs6",
    )
    rejected[0]["decision"] = "confirmed"

    assert rejected[0]["context_decision"] == "violated"
    assert tool._authoritative_selection([rejected[0]]) == ("", "none")


def _confirmed(code: str) -> dict:
    return {
        "code": code,
        "decision": "confirmed",
        "descr": "Named branch",
        "residual": False,
    }


def test_multiple_o_uses_scoped_bti_for_gri3_code_confirmation() -> None:
    from bussiness_logic.classification.services import staged_classification as sc

    previous = sc._BTI_RECALL_CACHE
    sc._BTI_RECALL_CACHE = {
        "cases": [
            {
                "ref": "BTI-1",
                "cn8": "19022099",
                "toks": ["stuffed", "dumpling"],
                "phrases": ["stuffed dumpling"],
            },
            {
                "ref": "BTI-2",
                "cn8": "19022099",
                "toks": ["stuffed", "dumpling"],
                "phrases": ["stuffed dumpling"],
            },
        ],
        "df": {"stuffed": 2, "dumpling": 2},
        "n_docs": 2,
        "avg_len": 2.0,
    }
    try:
        ranked = [_confirmed("1902"), _confirmed("2104")]
        losers = sc._ApplyGri3(
            ranked,
            {
                "identity_hints": {
                    "normalized_tariff_description": "stuffed dumpling"
                }
            },
        )
    finally:
        sc._BTI_RECALL_CACHE = previous
    assert ranked[0]["code"] == "1902"
    assert ranked[0]["gri3"] == "gri3_precedent"
    assert ranked[0]["gri_bti"]["authority"] == "code_confirmation"
    assert losers == ["2104"]


def test_multiple_o_uses_scoped_bti_for_gri6_code_confirmation() -> None:
    from bussiness_logic.classification.services import staged_classification as sc

    previous = sc._BTI_RECALL_CACHE
    sc._BTI_RECALL_CACHE = {
        "cases": [
            {
                "ref": "BTI-1",
                "cn8": "19021990",
                "toks": ["wheat", "noodle"],
                "phrases": ["wheat noodle"],
            },
            {
                "ref": "BTI-2",
                "cn8": "19021990",
                "toks": ["wheat", "noodle"],
                "phrases": ["wheat noodle"],
            },
        ],
        "df": {"wheat": 2, "noodle": 2},
        "n_docs": 2,
        "avg_len": 2.0,
    }
    try:
        ranked = [_confirmed("190211"), _confirmed("190219")]
        losers = sc._ApplyGri6(
            ranked,
            {
                "identity_hints": {
                    "normalized_tariff_description": "wheat noodle"
                }
            },
            prefix_len=6,
        )
    finally:
        sc._BTI_RECALL_CACHE = previous
    assert ranked[0]["code"] == "190219"
    assert ranked[0]["gri3"] == "gri6_precedent"
    assert losers == ["190211"]
