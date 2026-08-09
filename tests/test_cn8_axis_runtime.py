from __future__ import annotations

from bussiness_logic.classification.services import axis_verdict
from bussiness_logic.classification.rules import species_taxonomy
from bussiness_logic.classification.services.axis_verdict import (
    ProjectDecisionRowsForAxis,
)
from bussiness_logic.classification.services.staged_classification import (
    StagedClassificationTool,
    _ApplyGri6,
)


def _facts(*, form: str = "", state: str = "") -> dict:
    return {
        "identity_hints": {
            "commercial_identity": form,
            "food_form": form,
            "product_form_terms": [form] if form else [],
            "physical_form": form,
            "processing_state": state,
            "preservation_state": state,
        },
        "composition_facts": {},
    }


def test_cn8_axis_stamp_uses_cn8_map(monkeypatch) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{"19022010": {"axis": "physical_form"}}],
    )
    ranked = [{
        "code": "19022010",
        "descr": "Stuffed",
        "decision": "undecided",
        "decision_detail": [],
    }]

    assert axis_verdict.StampCn8AxisVerdicts(
        ranked,
        _facts(form="stuffed dumpling"),
    ) == 1
    assert ranked[0]["decision"] == "confirmed"
    assert ranked[0]["decision_detail"][-1]["why"] == "cn8_axis_map"


def test_cn8_axis_stamp_emits_silence_for_empty_canonical_field(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{"19022010": {"axis": "physical_form"}}],
    )
    ranked = [{
        "code": "19022010",
        "descr": "Stuffed",
        "decision": "undecided",
        "decision_detail": [],
    }]

    axis_verdict.StampCn8AxisVerdicts(ranked, _facts())

    assert ranked[0]["decision"] == "undecided"
    assert ranked[0]["decision_detail"][-1]["verdict"] == "silent"


def test_hs4_exclusion_boundary_nesoi_remains_residual(monkeypatch) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_axmap_cache",
        [{"2106": {"axis": "exclusion_boundary"}}],
    )
    ranked = [{
        "code": "2106",
        "descr": "Food preparations not elsewhere specified or included",
        "decision": "violated",
        "decision_detail": [],
        "residual": False,
    }]

    axis_verdict.StampHs4AxisVerdicts(
        ranked,
        _facts(form="seasoned cockles"),
    )

    assert ranked[0]["decision"] == "undecided"
    assert ranked[0]["residual"] is True
    assert ranked[0]["decision_detail"][-1]["verdict"] == "residual"


def test_cn8_nesoi_sentence_is_not_implicitly_residual(monkeypatch) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{"99999999": {"axis": "product_identity"}}],
    )
    ranked = [{
        "code": "99999999",
        "descr": "Named preparation not elsewhere specified or included",
        "decision": "undecided",
        "decision_detail": [],
        "residual": False,
    }]

    axis_verdict.StampCn8AxisVerdicts(
        ranked,
        _facts(form="named preparation"),
    )

    assert ranked[0]["residual"] is False


def test_hs6_species_axis_rejects_broad_identity_projection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_sub_axmap_cache",
        [{"030616": {"axis": "species_source"}}],
    )
    primary_axis, projected = ProjectDecisionRowsForAxis(
        "hs6",
        "030616",
        [
            {"cond_type": "product_identity", "value": '["shrimp"]'},
            {"cond_type": "species_source", "value": '["Pandalus"]'},
        ],
    )

    assert primary_axis == "species_source"
    assert [row["cond_type"] for row in projected] == ["species_source"]


def test_hs6_generic_shrimp_is_silent_for_cold_water_species(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_sub_axmap_cache",
        [{"030616": {"axis": "species_source"}}],
    )
    taxonomy = species_taxonomy.ExactSpeciesTaxonomy({
        "exact_concepts": [
            {"id": "shrimp", "labels": ["shrimp"], "parents": []},
            {
                "id": "pandalus_shrimp",
                "labels": ["pandalus"],
                "parents": ["shrimp"],
            },
            {
                "id": "crangon_shrimp",
                "labels": ["crangon", "crangon crangon"],
                "parents": ["shrimp"],
            },
        ],
    })
    monkeypatch.setattr(
        species_taxonomy,
        "GetExactSpeciesTaxonomy",
        lambda: taxonomy,
    )
    ranked = [{
        "code": "030616",
        "descr": (
            "Cold-water shrimps and prawns "
            "(Pandalus spp., Crangon crangon)"
        ),
        "decision": "undecided",
        "decision_detail": [],
    }]
    facts = _facts()
    facts["composition_facts"]["principal_ingredient"] = "shrimp"

    axis_verdict.StampHs6AxisVerdicts(ranked, facts)

    assert ranked[0]["decision"] == "undecided"
    assert ranked[0]["decision_detail"][-1]["verdict"] == "silent"
    assert (
        ranked[0]["decision_detail"][-1]["taxonomy_reason"]
        == "fact_is_only_question_ancestor"
    )


def test_cn8_species_axis_rejects_broad_identity_projection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{"03047110": {"axis": "species_source"}}],
    )
    primary_axis, projected = ProjectDecisionRowsForAxis(
        "cn8",
        "03047110",
        [
            {"cond_type": "product_identity", "value": '["cod"]'},
            {
                "cond_type": "species_source",
                "value": '["Gadus macrocephalus"]',
            },
        ],
    )

    assert primary_axis == "species_source"
    assert [row["cond_type"] for row in projected] == ["species_source"]


def test_cn8_material_axis_keeps_identity_family_projection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{"19021910": {"axis": "material_composition"}}],
    )
    primary_axis, projected = ProjectDecisionRowsForAxis(
        "cn8",
        "19021910",
        [
            {"cond_type": "product_identity", "value": '["pasta"]'},
            {
                "cond_type": "material_composition",
                "value": '["common wheat"]',
            },
        ],
    )

    assert primary_axis == "material_composition"
    assert [row["cond_type"] for row in projected] == [
        "product_identity",
        "material_composition",
    ]


def test_cn8_unsupported_axis_emits_silence_without_lexical_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{"96091090": {"axis": "dimension_capacity"}}],
    )
    ranked = [{
        "code": "96091090",
        "descr": "With a diameter exceeding 10 mm",
        "decision": "undecided",
        "decision_detail": [],
    }]

    axis_verdict.StampCn8AxisVerdicts(
        ranked,
        _facts(form="pencil diameter 12 mm"),
    )

    assert ranked[0]["decision"] == "undecided"
    assert ranked[0]["decision_detail"][-1]["verdict"] == "silent"
    assert "canonical_axis_unsupported" in ranked[0]["decision_detail"][-1]["why"]


def test_cn8_residual_is_complement_not_direct_confirmation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{"19022090": {"axis": "physical_form"}}],
    )
    ranked = [{
        "code": "19022090",
        "descr": "Other",
        "decision": "confirmed",
        "decision_detail": [],
        "residual": True,
    }]

    axis_verdict.StampCn8AxisVerdicts(
        ranked,
        _facts(form="other stuffed product"),
    )

    assert ranked[0]["decision"] == "undecided"
    assert ranked[0]["residual"] is True
    assert ranked[0]["decision_detail"][-1]["verdict"] == "residual"


def test_cn8_gri6_never_crosses_direct_parent() -> None:
    ranked = [
        {
            "code": "19022010",
            "descr": "Named A",
            "decision": "confirmed",
        },
        {
            "code": "19022090",
            "descr": "Other",
            "decision": "confirmed",
            "residual": True,
        },
        {
            "code": "19023010",
            "descr": "Named B",
            "decision": "confirmed",
        },
    ]

    _ApplyGri6(ranked, _facts(), prefix_len=8)

    assert ranked[0].get("gri3") == "gri6a_most_specific"
    assert not ranked[2].get("gri3")


def test_cn8_suffix_silence_blocks_leaf_selection() -> None:
    ranked = [{
        "code": "03021120",
        "descr": "Named leaf",
        "decision": "confirmed",
        "context_scope": "Salmonidae",
        "context_decision": "undecided",
    }]

    winner, reason = StagedClassificationTool._authoritative_selection(ranked)

    assert winner == ""
    assert reason == "none"


def test_cn8_suffix_residual_requires_confirmed_context() -> None:
    ranked = [
        {
            "code": "03021120",
            "descr": "Named leaf",
            "decision": "violated",
            "context_scope": "Salmonidae",
            "context_decision": "undecided",
        },
        {
            "code": "03021190",
            "descr": "Other",
            "decision": "undecided",
            "residual": True,
            "context_scope": "Salmonidae",
            "context_decision": "undecided",
        },
    ]

    winner, reason = StagedClassificationTool._authoritative_selection(ranked)

    assert winner == ""
    assert reason == "none"


def test_cn8_context_falls_back_to_runtime_axis_map(monkeypatch) -> None:
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{
            "03021120": {
                "axis": "species_source",
                "context_scope": "Salmonidae",
            },
        }],
    )
    tool = StagedClassificationTool()

    ranked = tool._rank_sibling_group(
        [{
            "row": {
                "code": "03021120",
                "option_label_en": "Oncorhynchus mykiss",
                "positive_terms": "oncorhynchus;mykiss",
                "negative_terms": "",
                "residual_other_flag": "false",
            },
            "code": "03021120",
        }],
        _facts(),
        set(),
        [],
        {},
        {},
        True,
        "cn8",
    )

    assert ranked[0]["context_scope"] == "Salmonidae"
    assert ranked[0]["context_decision"] == "undecided"
    assert ranked[0]["context_detail"][0]["verdict"] == "silent"


def test_cn8_compiled_suffix_context_uses_canonical_binding(
        monkeypatch,
) -> None:
    monkeypatch.setenv("ASAP_CONTEXT_OBSERVATION", "0")
    monkeypatch.setenv("ASAP_CONTEXT_OBSERVATION_AUTHORITY", "0")
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{
            "19022010": {
                "axis": "physical_form",
                "context_scope": "Stuffed pasta",
            },
        }],
    )
    tool = StagedClassificationTool()
    conditions = {
        "19022010": [{
            "then_code": "19022010",
            "cond_type": "physical_form",
            "op": "has_token",
            "value": '["stuffed"]',
            "source_text": "Stuffed pasta",
            "role": "qualifier",
            "grade": "nomenclature",
            "alt_group": "",
        }],
    }

    ranked = tool._rank_sibling_group(
        [{
            "row": {
                "code": "19022010",
                "option_label_en": "Cooked",
                "positive_terms": "cooked",
                "negative_terms": "",
                "residual_other_flag": "false",
            },
            "code": "19022010",
        }],
        _facts(form="stuffed dumpling"),
        set(),
        [],
        {},
        conditions,
        True,
        "cn8",
    )

    assert ranked[0]["context_scope"] == "Stuffed pasta"
    assert ranked[0]["context_decision"] == "confirmed"
    assert ranked[0]["context_detail"][0]["verdict"] == "true"


def test_staged_runtime_selects_cn8_by_axis_not_lexical(monkeypatch) -> None:
    monkeypatch.setenv("ASAP_STAGED_DECISION_TABLE", "0")
    monkeypatch.setenv("ASAP_STAGED_PREDICATES", "0")
    monkeypatch.setenv("ASAP_BTI_RECALL", "0")
    monkeypatch.setenv("ASAP_PRECEDENT_LEAD", "0")
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{
            "19022010": {
                "axis": "physical_form",
                "runtime_parent_code": "190220",
                "decision_parent_level": "hs6",
                "decision_parent_code": "190220",
            },
            "19022090": {
                "axis": "physical_form",
                "runtime_parent_code": "190220",
                "decision_parent_level": "hs6",
                "decision_parent_code": "190220",
            },
        }],
    )

    class FixtureTool(StagedClassificationTool):
        @staticmethod
        def _load_branch_rows(level, parents):
            if level == "hs6":
                return ({
                    "code": "190220",
                    "parent_code": "1902",
                    "option_label_en": "Stuffed pasta",
                    "positive_terms": "stuffed;pasta",
                    "negative_terms": "",
                    "residual_other_flag": "false",
                },)
            if level == "cn8":
                return (
                    {
                        "code": "19022010",
                        "parent_code": "190220",
                        "option_label_en": "Stuffed",
                        "positive_terms": "stuffed",
                        "negative_terms": "",
                        "residual_other_flag": "false",
                    },
                    {
                        "code": "19022090",
                        "parent_code": "190220",
                        "option_label_en": "Other",
                        "positive_terms": "other",
                        "negative_terms": "",
                        "residual_other_flag": "true",
                    },
                )
            return ()

        def _final_candidates(self, cn8_prefixes, *, top_k):
            return [
                {
                    "cn8": code,
                    "hs6": code[:6],
                    "hs4": code[:4],
                    "description": code,
                }
                for code in cn8_prefixes[:top_k]
            ]

    result = FixtureTool().classify(
        product_facts=_facts(form="stuffed dumpling"),
        routing_context={},
        start_parents=["1902"],
    )

    assert result["ok"] is True
    assert result["candidates"][0]["cn8"] == "19022010"
    cn8_stage = next(stage for stage in result["stages"] if stage["stage"] == "cn8")
    assert cn8_stage["selection_authority"] == "confirmed"
    assert cn8_stage["candidates_considered"][0]["axis_parent_code"] == "190220"


def test_cn8_axis_sees_candidate_beyond_legacy_rank_cut(monkeypatch) -> None:
    monkeypatch.setenv("ASAP_STAGED_DECISION_TABLE", "0")
    monkeypatch.setenv("ASAP_STAGED_PREDICATES", "0")
    monkeypatch.setenv("ASAP_BTI_RECALL", "0")
    monkeypatch.setenv("ASAP_PRECEDENT_LEAD", "0")
    monkeypatch.setattr(
        axis_verdict,
        "_cn8_axmap_cache",
        [{
            "19022010": {"axis": "physical_form"},
            "19022090": {"axis": "physical_form"},
        }],
    )

    class FixtureTool(StagedClassificationTool):
        @staticmethod
        def _load_branch_rows(level, parents):
            if level == "hs6":
                return ({
                    "code": "190220",
                    "parent_code": "1902",
                    "option_label_en": "Stuffed pasta",
                    "positive_terms": "stuffed;pasta",
                    "negative_terms": "",
                    "residual_other_flag": "false",
                },)
            if level == "cn8":
                return (
                    {
                        "code": "19022090",
                        "parent_code": "190220",
                        "option_label_en": "Powder",
                        "positive_terms": "powder",
                        "negative_terms": "",
                        "residual_other_flag": "false",
                    },
                    {
                        "code": "19022010",
                        "parent_code": "190220",
                        "option_label_en": "Stuffed",
                        "positive_terms": "stuffed",
                        "negative_terms": "",
                        "residual_other_flag": "false",
                    },
                )
            return ()

        def _rank_sibling_group(self, items, *args, **kwargs):
            ranked = super()._rank_sibling_group(items, *args, **kwargs)
            if kwargs.get("level") == "cn8":
                return sorted(
                    ranked,
                    key=lambda row: row["code"],
                    reverse=True,
                )
            return ranked

        def _final_candidates(self, cn8_prefixes, *, top_k):
            return [
                {
                    "cn8": code,
                    "hs6": code[:6],
                    "hs4": code[:4],
                    "description": code,
                }
                for code in cn8_prefixes[:top_k]
            ]

    result = FixtureTool(rank_top_k=1).classify(
        product_facts=_facts(form="stuffed dumpling"),
        routing_context={},
        start_parents=["1902"],
    )

    assert result["ok"] is True
    assert result["candidates"][0]["cn8"] == "19022010"
