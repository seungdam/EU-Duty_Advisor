from __future__ import annotations

import json

import pytest

from bussiness_logic.classification.services.semantic_chapter_router import (
    BuildHs2EvidenceProjection,
    SemanticChapterRouter,
)
from bussiness_logic.product.model.product_understanding import (
    DistilledIdentityFacts,
    EncyclopediaEntryDto,
    EncyclopediaEvidenceSet,
)
from bussiness_logic.product.services import coi_loader, identity_hint_agent
from bussiness_logic.product.services.coi_loader import (
    FindFormForProduct,
    InjectIntoProductUnderstanding,
)
from bussiness_logic.product.services.identity_hint_guard import (
    BuildIdentityGuardEvidence,
    ClearIdentityHintGuardCache,
    ValidateIdentityHintCandidate,
)


COD_PRODUCT = "[베베피쉬] 이유식용 국산 다진 대구살 90g (냉동)"
SHRIMP_PRODUCT = "[베베피쉬] 이유식용 국산 다진 새우살 90g (냉동)"
LABEL_FACTS = (
    "식품유형: 기타 수산물가공품(가열하여 섭취하는 냉동식품)",
    "보관방법: -18°C 이하 냉동보관",
)


@pytest.fixture(autouse=True)
def _coi_forms(monkeypatch):
    codKey = "coi_cod.json"
    shrimpKey = "coi_shrimp.json"
    mapping = {
        COD_PRODUCT: (codKey,),
        SHRIMP_PRODUCT: (shrimpKey,),
    }
    forms = {
        codKey: {
            "principal_candidates": ["Pacific cod"],
            "entries": [
                {
                    "order_index": 1,
                    "name_ko": "대구",
                    "name_en": "Pacific cod",
                    "sub_ingredients": ["Pacific cod"],
                    "role": "other",
                }
            ],
        },
        shrimpKey: {
            "principal_candidates": ["북쪽분홍새우 Alaskan pink shrimp"],
            "entries": [
                {
                    "order_index": 1,
                    "name_ko": "새우",
                    "name_en": "Alaskan pink shrimp",
                    "sub_ingredients": [
                        "북쪽분홍새우 Alaskan pink shrimp"
                    ],
                    "role": "other",
                }
            ],
        },
    }
    monkeypatch.setattr(coi_loader, "LoadCoiProductMap", lambda: mapping)
    monkeypatch.setattr(coi_loader, "LoadCoiForms", lambda: forms)


class _IdentityResponse:
    generatedText = json.dumps(
        {
            "name_en": "frozen prepared minced cod",
            "identity_head": "frozen prepared fish product",
            "principal_ingredient": "shrimp",
            "processing_state": "prepared",
            "preservation_state": "ambient",
            "physical_form": "whole",
            "intended_use": "baby food",
            "domain_hints": ["I"],
            "confidence": 0.9,
            "needs_review": False,
        }
    )


class _CountingIdentityAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def Generate(self, request: object) -> _IdentityResponse:
        del request
        self.calls += 1
        return _IdentityResponse()


class _SemanticResponse:
    generatedText = json.dumps(
        {
            "decision_status": "selected",
            "candidates": [
                {
                    "chapter": "03",
                    "support_status": "supported",
                    "reason": "Frozen minced cod with no supported preparation state.",
                    "fact_paths": [
                        "composition_facts.principal_ingredient",
                        "composition_facts.ingredient_entries",
                    ],
                    "authority_fields": ["chapter_including"],
                    "missing_facts": [],
                }
            ],
            "rejected_chapters": [
                {
                    "chapter": "16",
                    "reason": "No explicit preparation evidence.",
                }
            ],
        }
    )
    modelName = "fake-semantic-router"
    runtimePath = "test"
    responseId = "response-1"


class _SemanticAdapter:
    def Generate(self, request: object) -> _SemanticResponse:
        del request
        return _SemanticResponse()


def _Evidence() -> EncyclopediaEvidenceSet:
    return EncyclopediaEvidenceSet(
        encyclopediaEvidenceId="ency-cod",
        productId="prod-cod",
        query="대구살",
        configured=True,
        entries=(
            EncyclopediaEntryDto(
                title="Daegu subway fire",
                description="A fire in the Daegu subway system.",
                link="https://example.test/daegu-fire",
                contentHash="bad-entity",
                grade="strong",
            ),
        ),
        qualityStatus="raw_entries",
    )


def _Distilled() -> DistilledIdentityFacts:
    return DistilledIdentityFacts(
        distilledIdentityId="dist-cod",
        productId="prod-cod",
        sourceEncyclopediaEvidenceId="ency-cod",
    )


def test_bebefish_normalized_coi_forms_are_product_specific() -> None:
    cod = FindFormForProduct(COD_PRODUCT)
    shrimp = FindFormForProduct(SHRIMP_PRODUCT)

    assert cod is not None
    assert shrimp is not None
    assert cod["principal_candidates"] == ["Pacific cod"]
    assert [entry["name_ko"] for entry in cod["entries"]] == ["대구"]
    assert shrimp["principal_candidates"] == ["북쪽분홍새우 Alaskan pink shrimp"]
    assert [entry["name_ko"] for entry in shrimp["entries"]] == ["새우"]


def test_guard_rejects_legal_state_pollution_and_bad_encyclopedia_entity() -> None:
    evidence = BuildIdentityGuardEvidence(
        productName=COD_PRODUCT,
        factTexts=LABEL_FACTS,
        encyclopediaEvidence=_Evidence(),
    )
    result = ValidateIdentityHintCandidate(
        parsed=json.loads(_IdentityResponse.generatedText),
        evidence=evidence,
    )

    assert evidence.acceptedEncyclopediaTitles == ()
    assert evidence.rejectedEncyclopediaTitles == ("Daegu subway fire",)
    assert result.candidate["principal_ingredient"] == "Pacific cod"
    assert result.candidate["processing_state"] == ""
    assert result.candidate["preservation_state"] == "frozen"
    assert result.candidate["physical_form"] == "minced"
    assert result.candidate["identity_head"] == "fish"
    assert result.candidate["name_en"] == "frozen minced cod"
    assert result.needsReview is True
    assert any(
        reason.startswith("unsupported_processing_state:prepared")
        for reason in result.reasons
    )


def test_identity_agent_reuses_guarded_result_for_same_evidence(monkeypatch) -> None:
    ClearIdentityHintGuardCache()
    monkeypatch.setenv("ASAP_IDENTITY_HINT_GUARD", "1")
    adapter = _CountingIdentityAdapter()
    monkeypatch.setattr(identity_hint_agent, "_chapter_context", lambda: "")
    monkeypatch.setattr(
        identity_hint_agent,
        "_vocab_grounded_text",
        lambda value, limit_tokens: str(value or "").strip(),
    )
    monkeypatch.setattr(
        identity_hint_agent,
        "_vocab_grounded_terms",
        lambda values, limit: tuple(
            str(value).strip() for value in values if str(value).strip()
        )[:limit],
    )
    monkeypatch.setattr(
        identity_hint_agent,
        "_grounded_typed_field",
        lambda value: str(value or "").strip().lower(),
    )

    outputs = [
        identity_hint_agent.IdentityHintAgent(adapter).BuildIdentityFacts(
            productName=COD_PRODUCT,
            distilledIdentity=_Distilled(),
            encyclopediaEvidence=_Evidence(),
            factTexts=LABEL_FACTS,
        )
        for _ in range(5)
    ]

    assert adapter.calls == 1
    assert all(output == outputs[0] for output in outputs)
    assert outputs[0]["processing_state"] == ""
    assert outputs[0]["preservation_state"] == "frozen"
    assert outputs[0]["physical_form"] == "minced"
    assert outputs[0]["food_form"] == "fish"
    assert outputs[0]["principal_ingredient_guess"] == "pacific cod"


def test_identity_guard_is_opt_in(monkeypatch) -> None:
    ClearIdentityHintGuardCache()
    monkeypatch.setenv("ASAP_IDENTITY_HINT_GUARD", "0")
    adapter = _CountingIdentityAdapter()
    monkeypatch.setattr(identity_hint_agent, "_chapter_context", lambda: "")
    monkeypatch.setattr(
        identity_hint_agent,
        "_vocab_grounded_text",
        lambda value, limit_tokens: str(value or "").strip(),
    )
    monkeypatch.setattr(
        identity_hint_agent,
        "_vocab_grounded_terms",
        lambda values, limit: tuple(
            str(value).strip() for value in values if str(value).strip()
        )[:limit],
    )
    monkeypatch.setattr(
        identity_hint_agent,
        "_grounded_typed_field",
        lambda value: str(value or "").strip().lower(),
    )

    output = identity_hint_agent.IdentityHintAgent(adapter).BuildIdentityFacts(
        productName=COD_PRODUCT,
        distilledIdentity=_Distilled(),
        encyclopediaEvidence=_Evidence(),
        factTexts=LABEL_FACTS,
    )

    assert output["processing_state"] == "prepared"
    assert output["preservation_state"] == "ambient"
    assert output["physical_form"] == "whole"
    assert output["principal_ingredient_guess"] == "shrimp"
    assert output["chapter_hint_basis"] == "axis_questions"


def test_guarded_pu_projection_routes_with_only_cod_composition() -> None:
    evidence = BuildIdentityGuardEvidence(
        productName=COD_PRODUCT,
        factTexts=LABEL_FACTS,
        encyclopediaEvidence=_Evidence(),
    )
    guarded = ValidateIdentityHintCandidate(
        parsed=json.loads(_IdentityResponse.generatedText),
        evidence=evidence,
    ).candidate
    pu = {
        "product_name": COD_PRODUCT,
        "identity_hints": {
            "commercial_identity": guarded["identity_head"],
            "normalized_tariff_description": "fish; Pacific cod; frozen; minced",
            "food_form": guarded["identity_head"],
            "processing_state": guarded["processing_state"],
            "preservation_state": guarded["preservation_state"],
            "physical_form": guarded["physical_form"],
            "principal_ingredient_guess": guarded["principal_ingredient"],
            "identity_terms": ["fish", "Pacific cod"],
            "product_form_terms": ["frozen", "minced"],
        },
        "composition_facts": {
            "processing_state": "unknown",
            "ingredient_entries": [],
            "ingredient_classes": [],
        },
    }
    injected = InjectIntoProductUnderstanding(pu)
    assert injected is not None

    projection = BuildHs2EvidenceProjection(pu)
    serialized = json.dumps(projection, ensure_ascii=False).lower()
    assert "pacific cod" in serialized
    assert "shrimp" not in serialized
    assert "prepared" not in serialized
    assert projection["identity_hints"]["preservation_state"] == "frozen"
    assert projection["identity_hints"]["physical_form"] == "minced"

    rows = (
        {
            "chapter": "03",
            "chapter_title": "Fish and crustaceans",
            "chapter_including": "Fish, fresh, chilled or frozen.",
        },
        {
            "chapter": "16",
            "chapter_title": "Preparations of fish",
            "chapter_including": "Prepared or preserved fish.",
        },
    )
    decision = SemanticChapterRouter(
        chapterRowsProvider=lambda: rows,
        runtimeAdapter=_SemanticAdapter(),
    ).Route(pu)

    assert decision.selectedHs2 == "03"
    assert decision.candidateHs2 == ("03",)
    assert decision.routingBasis.method == "semantic_closed_choice_llm"
