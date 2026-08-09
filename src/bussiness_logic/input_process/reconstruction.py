"""Product input reconstruction from notice, table OCR, and raw OCR evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from bussiness_logic.bridge.adapter import RuntimeAdapter
from bussiness_logic.bridge.schema import (
    LlmGenerationOptions,
    LlmRequest,
    LlmResponseFormat,
)
from bussiness_logic.artifact_paths import ExtractProductIdFromUrl
from bussiness_logic.input_process.dictionary import (
    ProductDictionaryMatch,
    ProductDictionaryRepository,
    ProductDictionaryRetriever,
)
from bussiness_logic.product.ocr.ocr_normalization import (
    OCR_FACT_LABEL_MATCHERS,
    ProductOcrFactNormalizer,
)
from bussiness_logic.utils import NormalizeWhiteSpace, NormalizeWhitespaceLines
from bussiness_logic.utils.json_files import WriteJsonAtomically
from bussiness_logic.utils.json_types import JsonObject


DEFAULT_LLM_INPUT_RECONSTRUCTION_MAX_TOKENS = 4096

PRODUCT_FACT_RECONSTRUCTION_SYSTEM_PROMPT = """
You reconstruct canonical product input facts from Korean product summary, Korean product notice, structured table VLM evidence, and raw OCR text.
Your job is evidence-backed reconstruction, not classification: recover the product label/specification facts that downstream deterministic classifiers can use.
For every accepted or unresolved fact, return a short evidence trace with source_refs, selected_span, decision_reason, status, and unresolved_reason.
Do not expose hidden chain-of-thought or long free-form reasoning. Use fixed, short evidence trace fields only.
Use structured table OCR as the main table skeleton when present, and use raw OCR or product notice text to correct OCR typos, fill missing cells, and resolve split/merged table fragments only when the evidence supports it.
Return only the schema fields requested by the structured-output contract.
product_facts is the authoritative compact classification input. Put every explicit classification-critical value here first.
reconstructed_tables preserves structured table OCR contents for audit/evidence review. Do not summarize table-like evidence away.
When vlm_table or pp_table evidence contains table-like rows, preserve compact rows in reconstructed_tables even when selected values also appear in product_facts.
Use [] for reconstructed_tables only when no table-like evidence exists, or when the only possible table row would duplicate one long ingredient/composition value without adding row structure.
For nutrition tables, return each nutrient as its own reconstructed_tables row. For label/specification tables, return each label field as its own row.
Do not return JSON null. Use an empty string, empty array, or omit the uncertain fact.
Do not infer HS, CN, TARIC, customs, legal, or regulatory conclusions.
Do not create product facts that are absent from the provided evidence.
Use product_summary evidence only for product identity, commercial description, physical form, processing/preparation hints, and brand. Do not derive ingredient or composition facts from product_summary unless an exact ingredient/composition list appears there.
Correct OCR typos only when the surrounding evidence strongly supports the correction.
Put the corrected/canonical classification value in normalized_value.
Do not expose pre-correction OCR text in any output field.
If evidence is insufficient for a corrected value, put the fact in unresolved_facts.
If evidence is insufficient or conflicting, use unresolved_facts or conflicts.
If a classification-critical fact is missing, explain it in missing_fact_reasons instead of inventing a value.
The application will generate normalized_fact_texts after validation.
Preserve table rows in reconstructed_tables even when they are not selected as product_facts.
Prefer concise product_facts for classification: product name, food/cosmetic type, physical form, processing state, preparation/use, storage state, ingredients, composition ratios, net content, and origin/manufacture country when explicit.
If a long formal ingredient/composition list appears, include it in product_facts first as 원재료명/ingredients. Do not duplicate the same long value in reconstructed_tables unless the table shape itself adds useful audit value.
For food products, product_facts must include explicit ingredient/composition rows when they appear in evidence, including component-specific ingredients for multi-component products such as dumpling plus sauce.
For mixed products, preserve explicit component boundaries when evidence provides them, for example dumpling wrapper/filling/sauce or product plus separate sauce.
If an explicit percentage or ratio appears in an ingredient/composition field, preserve it exactly in product_facts. Do not invent missing percentages.
For raw OCR text, treat section headings strictly. Ingredients/composition facts must come from Ingredient/재료/원재료 sections, not from Process/생산 유통 과정, Recommendation/활용법, or Brand/브랜드 sections.
Do not put nutrient measurements such as sodium, carbohydrates, fat, protein, kcal, or daily value percentages in product_facts; keep nutrition rows only in reconstructed_tables.
Do not include allergen warnings, same-facility/cross-contamination warnings, seller/vendor/manufacturer business-party names, expiry, package material, or marketing copy as product_facts unless they directly change customs classification.
Return atomic field_name/normalized_value pairs. Do not put a whole OCR block under a generic field.
Never use field names such as OCR observation, OCR 관찰, tile, raw OCR, table marker, or evidence id.
[tile N], [table N], source_ref, source_type, and evidence_id are collection metadata, not product facts.
Do not copy unrelated marketing copy or duplicate OCR fragments.
""".strip()

OCR_COLLECTION_MARKER_PATTERN = re.compile(
    r"(?im)^\s*\[\s*(?:tile|table|raw_ocr_tiles|structured_tables)\s*#?\d*\s*\]\s*$"
)
INLINE_OCR_COLLECTION_MARKER_PATTERN = re.compile(
    r"(?i)\[\s*(?:tile|table)\s*#?\d+\s*\]"
)
GENERIC_OCR_FIELD_PATTERN = re.compile(
    r"(?i)(?:^|\s)(?:ocr|raw\s*ocr|tile|table|관찰|관측|메타|metadata)(?:\s|$)"
)
QUANTITY_TOKEN_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(mg|g|kg|ml|l|%|kcal|㎎|㎏|㎖)",
    re.IGNORECASE,
)
VLM_TABLE_SOURCE_TYPE = "vlm_table"
LEGACY_VLM_TABLE_SOURCE_TYPE = "pp_table"
CLASSIFICATION_INGREDIENT_FIELD_MARKERS = (
    "원재료",
    "원료",
    "원제",
    "배합",
    "성분명",
    "ingredients",
    "composition",
)
NUTRITION_FIELD_MARKERS = (
    "영양",
    "나트륨",
    "탄수화물",
    "당류",
    "지방",
    "콜레스테롤",
    "단백질",
    "kcal",
    "dailyvalue",
)
INGREDIENT_SECTION_START_MARKERS = (
    "재료와성분",
    "원재료명",
    "원재료",
    "원료명",
    "전성분",
    "성분",
    "ingredients",
    "composition",
)
INGREDIENT_SECTION_STOP_MARKERS = (
    "생산유통과정",
    "활용법",
    "브랜드와생산자",
    "보관방법",
    "유통기한",
    "소비기한",
    "품목보고번호",
    "포장재질",
    "주의사항",
    "알레르기정보",
    "process",
    "recommendation",
    "brand",
)


def _IsVlmTableSourceType(sourceType: str) -> bool:
    return sourceType in {VLM_TABLE_SOURCE_TYPE, LEGACY_VLM_TABLE_SOURCE_TYPE}

PRODUCT_FACT_RECONSTRUCTION_FEW_SHOT_EXAMPLES = [
    {
        "input": {
            "evidence": [
                {
                    "evidence_id": "evidence-1",
                    "source_type": "vlm_table",
                    "text": "영양성분 나트류 320mg 탄수하물 40g 단백질 8g",
                }
            ],
        },
        "output": {
            "reconstructed_tables": [
                {
                    "table_name": "영양성분",
                    "source_refs": ["evidence-1"],
                    "rows": [
                        {
                            "field_name": "나트륨",
                            "normalized_value": "나트륨 320mg",
                            "unit": "mg",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                        {
                            "field_name": "탄수화물",
                            "normalized_value": "탄수화물 40g",
                            "unit": "g",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                        {
                            "field_name": "단백질",
                            "normalized_value": "단백질 8g",
                            "unit": "g",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                    ],
                }
            ],
            "product_facts": [],
            "evidence_traces": [],
            "missing_fact_reasons": [],
            "unresolved_facts": [],
            "conflicts": [],
            "warnings": [],
        },
    },
    {
        "input": {
            "evidence": [
                {
                    "evidence_id": "evidence-1",
                    "source_type": "vlm_table",
                    "text": "원제교명 | 정제수, 율엣, 설량, 고초가루(고추·중국산)",
                }
            ],
        },
        "output": {
            "reconstructed_tables": [
                {
                    "table_name": "제품 정보",
                    "source_refs": ["evidence-1"],
                    "rows": [
                        {
                            "field_name": "원재료명",
                            "normalized_value": "정제수, 물엿, 설탕, 고춧가루(고추:중국산)",
                            "unit": "",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        }
                    ],
                }
            ],
            "product_facts": [
                {
                    "field_name": "원재료명",
                    "normalized_value": "정제수, 물엿, 설탕, 고춧가루(고추:중국산)",
                    "source_refs": ["evidence-1"],
                    "correction_type": "llm_reconstructed",
                    "validation_status": "accepted",
                }
            ],
            "evidence_traces": [],
            "missing_fact_reasons": [],
            "unresolved_facts": [],
            "conflicts": [],
            "warnings": [],
        },
    },
    {
        "input": {
            "evidence": [
                {
                    "evidence_id": "evidence-1",
                    "source_type": "raw_ocr_tile",
                    "option_key": "option-1",
                    "text": "제품명 오봉집낙지볶음 내용량 300g(274kcal) 식품의 유형 기타수산물가공품",
                },
                {
                    "evidence_id": "evidence-2",
                    "source_type": "raw_ocr_tile",
                    "option_key": "option-2",
                    "text": "제품명 오봉집낙지볶음 내용량 500g(457kcal) 식품의 유형 기타수산물가공품",
                },
            ],
        },
        "output": {
            "reconstructed_tables": [
                {
                    "table_name": "옵션 1",
                    "source_refs": ["evidence-1"],
                    "rows": [
                        {
                            "field_name": "내용량",
                            "normalized_value": "300g(274kcal)",
                            "unit": "g",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        }
                    ],
                },
                {
                    "table_name": "옵션 2",
                    "source_refs": ["evidence-2"],
                    "rows": [
                        {
                            "field_name": "내용량",
                            "normalized_value": "500g(457kcal)",
                            "unit": "g",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-2"],
                        }
                    ],
                },
            ],
            "product_facts": [
                {
                    "field_name": "제품명",
                    "normalized_value": "오봉집낙지볶음",
                    "source_refs": ["evidence-1", "evidence-2"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                },
                {
                    "field_name": "식품의 유형",
                    "normalized_value": "기타수산물가공품",
                    "source_refs": ["evidence-1", "evidence-2"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                },
            ],
            "evidence_traces": [],
            "missing_fact_reasons": [],
            "unresolved_facts": [],
            "conflicts": [],
            "warnings": [],
        },
    },
    {
        "input": {
            "evidence": [
                {
                    "evidence_id": "evidence-1",
                    "source_type": "vlm_table",
                    "text": "영양정보 총 내용량 500g 열량 390kcal 나트륨 3,930mg 197% 탄수화물 48g 15%",
                },
                {
                    "evidence_id": "evidence-2",
                    "source_type": "raw_ocr_tile",
                    "text": "영양정보 총내용량 500g 390 kcal 나트륨 3,930 mg 197 % 탄수화물 48 g 15 %",
                },
            ],
        },
        "output": {
            "product_facts": [
                {
                    "field_name": "내용량",
                    "normalized_value": "500g",
                    "source_refs": ["evidence-1", "evidence-2"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                }
            ],
            "evidence_traces": [],
            "missing_fact_reasons": [],
            "reconstructed_tables": [
                {
                    "table_name": "영양정보",
                    "source_refs": ["evidence-1", "evidence-2"],
                    "rows": [
                        {
                            "field_name": "열량",
                            "normalized_value": "390",
                            "unit": "kcal",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1", "evidence-2"],
                        },
                        {
                            "field_name": "나트륨",
                            "normalized_value": "3930",
                            "unit": "mg",
                            "daily_value_percent": "197",
                            "source_refs": ["evidence-1", "evidence-2"],
                        },
                        {
                            "field_name": "탄수화물",
                            "normalized_value": "48",
                            "unit": "g",
                            "daily_value_percent": "15",
                            "source_refs": ["evidence-1", "evidence-2"],
                        },
                    ],
                }
            ],
            "unresolved_facts": [],
            "conflicts": [],
            "warnings": [],
        },
    },
    {
        "input": {
            "evidence": [
                {
                    "evidence_id": "evidence-1",
                    "source_type": "vlm_table",
                    "text": "총중량 360g 군만두 240g 소스 120g 식품유형 군만두 만두 식품유형 소스 소스",
                },
                {
                    "evidence_id": "evidence-2",
                    "source_type": "raw_ocr_tile",
                    "text": "원재료명(군만두) 밀가루, 당면, 대두유 원재료명(소스) 정제수, 물엿, 설탕, 고춧가루",
                },
            ],
        },
        "output": {
            "product_facts": [
                {
                    "field_name": "총중량",
                    "normalized_value": "360g (군만두 240g, 소스 120g)",
                    "source_refs": ["evidence-1"],
                    "correction_type": "llm_reconstructed",
                    "validation_status": "accepted",
                },
                {
                    "field_name": "식품유형 (군만두)",
                    "normalized_value": "만두",
                    "source_refs": ["evidence-1"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                },
                {
                    "field_name": "식품유형 (소스)",
                    "normalized_value": "소스",
                    "source_refs": ["evidence-1"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                },
                {
                    "field_name": "원재료명 (군만두)",
                    "normalized_value": "밀가루, 당면, 대두유",
                    "source_refs": ["evidence-2"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                },
                {
                    "field_name": "원재료명 (소스)",
                    "normalized_value": "정제수, 물엿, 설탕, 고춧가루",
                    "source_refs": ["evidence-2"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                },
            ],
            "evidence_traces": [],
            "missing_fact_reasons": [],
            "reconstructed_tables": [
                {
                    "table_name": "제품 정보",
                    "source_refs": ["evidence-1"],
                    "rows": [
                        {
                            "field_name": "총중량",
                            "normalized_value": "360g (군만두 240g, 소스 120g)",
                            "unit": "g",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                        {
                            "field_name": "식품유형 (군만두)",
                            "normalized_value": "만두",
                            "unit": "",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                        {
                            "field_name": "식품유형 (소스)",
                            "normalized_value": "소스",
                            "unit": "",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                    ],
                }
            ],
            "unresolved_facts": [],
            "conflicts": [],
            "warnings": [],
        },
    },
]


class InputEvidenceRecord(BaseModel):
    """상품 input 복원을 위한 원천 evidence record."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    evidenceId: str = Field(alias="evidence_id")
    sourceType: str = Field(alias="source_type")
    text: str
    sourceRef: Optional[str] = Field(default=None, alias="source_ref")
    optionKey: Optional[str] = Field(default=None, alias="option_key")
    validationStatus: str = Field(default="unverified", alias="validation_status")
    validationIssues: List[str] = Field(
        default_factory=list,
        alias="validation_issues",
    )


class InputEvidencePackage(BaseModel):
    """상품 input 복원 단계에 전달되는 evidence 묶음."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    productPageUrl: Optional[str] = Field(default=None, alias="product_page_url")
    records: List[InputEvidenceRecord] = Field(default_factory=list)


class ClassificationFact(BaseModel):
    """복원된 상품 input fact."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    fieldName: str = Field(alias="field_name", description="복원된 fact 필드명")
    normalizedValue: str = Field(default="", alias="normalized_value", description="정규화된 fact 값")
    sourceRefs: List[str] = Field(default_factory=list, alias="source_refs", description="근거 evidence ID")
    correctionType: str = Field(default="none", alias="correction_type", description="보정 방식")
    validationStatus: str = Field(default="accepted", alias="validation_status", description="검증 상태")

    @field_validator("fieldName", "normalizedValue", mode="before")
    @classmethod
    def NormalizeRequiredText(cls, value: object) -> str:
        return _NormalizeLlmScalarText(value)

    @field_validator("correctionType", mode="before")
    @classmethod
    def NormalizeCorrectionType(cls, value: object) -> str:
        return _NormalizeLlmScalarText(value, defaultValue="none") or "none"

    @field_validator("validationStatus", mode="before")
    @classmethod
    def NormalizeValidationStatus(cls, value: object) -> str:
        return _NormalizeLlmScalarText(value, defaultValue="accepted") or "accepted"

    @field_validator("sourceRefs", mode="before")
    @classmethod
    def NormalizeSourceRefs(cls, value: object) -> List[str]:
        return _NormalizeLlmTextList(value)

    def ToFactText(self) -> str:
        normalizedFieldName = NormalizeWhiteSpace(self.fieldName)
        displayValue = NormalizeWhitespaceLines(
            self.normalizedValue
        )
        if normalizedFieldName == "":
            return displayValue
        if displayValue == "":
            return normalizedFieldName
        if displayValue.startswith("{0}:".format(normalizedFieldName)):
            return displayValue
        return "{0}: {1}".format(normalizedFieldName, displayValue)


class ReconstructionTableRow(BaseModel):
    """Structured table reconstruction row preserved for UI review."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    fieldName: str = Field(alias="field_name", description="표 행 필드명")
    normalizedValue: str = Field(default="", alias="normalized_value", description="정규화된 표 행 값")
    unit: str = Field(default="", description="측정 단위")
    dailyValuePercent: str = Field(default="", alias="daily_value_percent", description="영양성분 일일 기준치 비율")
    sourceRefs: List[str] = Field(default_factory=list, alias="source_refs", description="근거 evidence ID")
    validationStatus: str = Field(default="unverified", alias="validation_status", description="표 행 검증 상태")
    validationIssues: List[str] = Field(
        default_factory=list,
        alias="validation_issues",
        description="표 행 검증 이슈",
    )

    @field_validator("fieldName", "normalizedValue", "unit", "dailyValuePercent", mode="before")
    @classmethod
    def NormalizeRowText(cls, value: object) -> str:
        return _NormalizeLlmScalarText(value)

    @field_validator("validationStatus", mode="before")
    @classmethod
    def NormalizeValidationStatus(cls, value: object) -> str:
        return _NormalizeLlmScalarText(value, defaultValue="unverified") or "unverified"

    @field_validator("sourceRefs", "validationIssues", mode="before")
    @classmethod
    def NormalizeRowTextList(cls, value: object) -> List[str]:
        return _NormalizeLlmTextList(value)


class ReconstructionTable(BaseModel):
    """Table reconstruction preserved separately from classification facts."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    tableName: str = Field(default="", alias="table_name", description="복원된 표 이름")
    sourceRefs: List[str] = Field(default_factory=list, alias="source_refs", description="표 근거 evidence ID")
    rows: List[ReconstructionTableRow] = Field(default_factory=list, description="복원된 표 행")

    @field_validator("tableName", mode="before")
    @classmethod
    def NormalizeTableName(cls, value: object) -> str:
        return _NormalizeLlmScalarText(value)

    @field_validator("sourceRefs", mode="before")
    @classmethod
    def NormalizeSourceRefs(cls, value: object) -> List[str]:
        return _NormalizeLlmTextList(value)

    @field_validator("rows", mode="before")
    @classmethod
    def NormalizeRows(cls, value: object) -> List[object]:
        return _NormalizeLlmObjectList(value)


class ProductFactReconstructionOutputFact(BaseModel):
    """LLM structured output fact contract."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    fieldName: str = Field(alias="field_name")
    normalizedValue: str = Field(alias="normalized_value")
    sourceRefs: List[str] = Field(alias="source_refs")
    correctionType: str = Field(alias="correction_type")
    validationStatus: str = Field(alias="validation_status")


class ProductFactReconstructionOutputTableRow(BaseModel):
    """LLM structured output table row contract."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    fieldName: str = Field(alias="field_name")
    normalizedValue: str = Field(alias="normalized_value")
    unit: str
    dailyValuePercent: str = Field(alias="daily_value_percent")
    sourceRefs: List[str] = Field(alias="source_refs")


class ProductFactReconstructionOutputTable(BaseModel):
    """LLM structured output table contract."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    tableName: str = Field(alias="table_name")
    sourceRefs: List[str] = Field(alias="source_refs")
    rows: List[ProductFactReconstructionOutputTableRow]


class ReconstructionEvidenceTrace(BaseModel):
    """Evidence trace for one reconstructed or unresolved fact."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    fieldName: str = Field(default="", alias="field_name")
    normalizedValue: str = Field(default="", alias="normalized_value")
    sourceRefs: List[str] = Field(default_factory=list, alias="source_refs")
    selectedSpan: str = Field(default="", alias="selected_span")
    decisionReason: str = Field(default="", alias="decision_reason")
    status: str = Field(default="", alias="status")
    unresolvedReason: str = Field(default="", alias="unresolved_reason")

    @field_validator(
        "fieldName",
        "normalizedValue",
        "selectedSpan",
        "decisionReason",
        "status",
        "unresolvedReason",
        mode="before",
    )
    @classmethod
    def NormalizeTraceText(cls, value: object) -> str:
        return _NormalizeLlmScalarText(value)

    @field_validator("sourceRefs", mode="before")
    @classmethod
    def NormalizeSourceRefs(cls, value: object) -> List[str]:
        return _NormalizeLlmTextList(value)


class ReconstructionMissingFactReason(BaseModel):
    """Evidence-bound reason for a fact the LLM could not reconstruct."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    factName: str = Field(default="", alias="fact_name")
    reason: str = ""
    sourceRefs: List[str] = Field(default_factory=list, alias="source_refs")

    @field_validator("factName", "reason", mode="before")
    @classmethod
    def NormalizeReasonText(cls, value: object) -> str:
        return _NormalizeLlmScalarText(value)

    @field_validator("sourceRefs", mode="before")
    @classmethod
    def NormalizeSourceRefs(cls, value: object) -> List[str]:
        return _NormalizeLlmTextList(value)


class ProductFactReconstructionOutput(BaseModel):
    """LLM structured output contract before internal validation metadata."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    productFacts: List[ProductFactReconstructionOutputFact] = Field(
        alias="product_facts"
    )
    reconstructedTables: List[ProductFactReconstructionOutputTable] = Field(
        alias="reconstructed_tables"
    )
    unresolvedFacts: List[ProductFactReconstructionOutputFact] = Field(
        alias="unresolved_facts"
    )
    evidenceTraces: List[ReconstructionEvidenceTrace] = Field(
        alias="evidence_traces"
    )
    missingFactReasons: List[ReconstructionMissingFactReason] = Field(
        alias="missing_fact_reasons"
    )
    conflicts: List[str]
    warnings: List[str]


def _StripOcrCollectionMarkers(text: str) -> str:
    withoutInlineMarkers = INLINE_OCR_COLLECTION_MARKER_PATTERN.sub(
        "",
        text or "",
    )
    lines = [
        line
        for line in NormalizeWhitespaceLines(
            withoutInlineMarkers,
        ).splitlines()
        if OCR_COLLECTION_MARKER_PATTERN.fullmatch(line.strip()) is None
    ]
    return NormalizeWhitespaceLines("\n".join(lines))


def _ExtractQuantityTokens(text: str) -> set[tuple[str, str]]:
    unitAliases = {"㎎": "mg", "㎏": "kg", "㎖": "ml"}
    return {
        (
            value.replace(",", ""),
            unitAliases.get(unit.lower(), unit.lower()),
        )
        for value, unit in QUANTITY_TOKEN_PATTERN.findall(text or "")
    }


def _HasMeaningfulNonQuantityText(text: str) -> bool:
    remainingText = QUANTITY_TOKEN_PATTERN.sub("", text or "")
    return re.search(r"[A-Za-z가-힣]{2,}", remainingText) is not None


def _ExtractMeaningfulEvidenceTokens(text: str, *, minLength: int = 2) -> set[str]:
    remainingText = QUANTITY_TOKEN_PATTERN.sub(" ", text or "")
    ignoredTokens = {"mg", "g", "kg", "ml", "l", "kcal"}
    return {
        token.lower()
        for token in re.findall(r"[0-9A-Za-z가-힣]+", remainingText)
        if len(token) >= minLength
        and not token.isdigit()
        and token.lower() not in ignoredTokens
    }


def _IsTokenCoveredByEvidenceParts(token: str, evidenceTokens: set[str]) -> bool:
    if token in evidenceTokens:
        return True
    return any(
        token[:splitIndex] in evidenceTokens and token[splitIndex:] in evidenceTokens
        for splitIndex in range(1, len(token))
    )


def _IsTokenTextuallyCovered(token: str, evidenceTokens: set[str]) -> bool:
    if _IsTokenCoveredByEvidenceParts(token, evidenceTokens):
        return True
    return any(token in evidenceToken for evidenceToken in evidenceTokens)


def _CompactEvidenceText(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", text or "").lower()


def _ExtractIngredientSectionText(text: str) -> str:
    sectionLines: List[str] = []
    inIngredientSection = False
    for line in NormalizeWhitespaceLines(text).splitlines():
        normalizedLine = NormalizeWhiteSpace(line)
        if normalizedLine == "":
            continue
        compactLine = _CompactEvidenceText(normalizedLine)
        if not inIngredientSection and any(
            marker in compactLine
            for marker in INGREDIENT_SECTION_START_MARKERS
        ):
            inIngredientSection = True
            sectionLines.append(normalizedLine)
            continue
        if inIngredientSection and any(
            marker in compactLine
            for marker in INGREDIENT_SECTION_STOP_MARKERS
        ):
            break
        if inIngredientSection:
            sectionLines.append(normalizedLine)
    return NormalizeWhitespaceLines("\n".join(sectionLines))


def _HasIngredientMarketingSectionBoundary(text: str) -> bool:
    compactText = _CompactEvidenceText(text)
    hasIngredientHeading = any(
        marker in compactText
        for marker in ("재료와성분", "ingredients")
    )
    hasMarketingStopHeading = any(
        marker in compactText
        for marker in (
            "생산유통과정",
            "활용법",
            "브랜드와생산자",
            "process",
            "recommendation",
            "brand",
        )
    )
    return hasIngredientHeading and hasMarketingStopHeading


def _NormalizeLlmScalarText(value: object, *, defaultValue: str = "") -> str:
    if value is None:
        return defaultValue
    if isinstance(value, str):
        return NormalizeWhitespaceLines(value)
    if isinstance(value, (int, float, bool)):
        return NormalizeWhiteSpace(str(value))
    return NormalizeWhitespaceLines(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _NormalizeLlmTextList(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalizedValue = NormalizeWhiteSpace(value)
        return [normalizedValue] if normalizedValue else []
    if not isinstance(value, list):
        return []
    normalizedValues: List[str] = []
    for item in value:
        normalizedItem = _NormalizeLlmScalarText(item)
        if normalizedItem:
            normalizedValues.append(normalizedItem)
    return list(dict.fromkeys(normalizedValues))


def _NormalizeLlmObjectList(value: object) -> List[object]:
    return value if isinstance(value, list) else []


def _IsGenericOcrFieldName(fieldName: str) -> bool:
    normalizedFieldName = NormalizeWhiteSpace(fieldName).lower()
    if normalizedFieldName == "":
        return True
    compactFieldName = normalizedFieldName.replace(" ", "")
    if compactFieldName in {
        "ocr관찰정보",
        "ocr관찰함량/용량후보",
        "rawocr",
        "rawocrtext",
        "tile",
        "table",
    }:
        return True
    return GENERIC_OCR_FIELD_PATTERN.search(normalizedFieldName) is not None


def _SplitFieldText(text: str) -> Optional[tuple[str, str]]:
    normalizedText = _StripOcrCollectionMarkers(text)
    for separator in [":", "："]:
        if separator in normalizedText:
            fieldName, fieldValue = normalizedText.split(separator, 1)
            fieldName = NormalizeWhiteSpace(fieldName)
            fieldValue = NormalizeWhitespaceLines(fieldValue)
            if fieldName and fieldValue:
                return fieldName, fieldValue
    return _SplitKnownFieldText(normalizedText)


def _SplitKnownFieldText(text: str) -> Optional[tuple[str, str]]:
    normalizedText = _StripOcrCollectionMarkers(text).strip(" :：·-*[]()")
    if normalizedText == "":
        return None
    normalizedLowerText = NormalizeWhiteSpace(normalizedText).lower()
    compactLowerText = normalizedLowerText.replace(" ", "")
    for (
        fieldLabel,
        normalizedFieldLabel,
        compactFieldLabel,
    ) in OCR_FACT_LABEL_MATCHERS:
        if normalizedLowerText.startswith(normalizedFieldLabel):
            fieldValue = normalizedText[len(fieldLabel) :].lstrip(" :：·-*[]()")
            if fieldValue:
                return fieldLabel, NormalizeWhitespaceLines(fieldValue)
        if compactLowerText.startswith(compactFieldLabel):
            compactValue = compactLowerText[len(compactFieldLabel) :]
            if compactValue:
                return fieldLabel, NormalizeWhitespaceLines(
                    normalizedText[len(fieldLabel) :].lstrip(" :：·-*[]()")
                    or compactValue,
                )
    return None


class InputReconstructionResult(BaseModel):
    """상품 input 복원 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    reconstructedTables: List[ReconstructionTable] = Field(
        default_factory=list,
        alias="reconstructed_tables",
        description="UI 검토용 복원 표",
    )
    productFacts: List[ClassificationFact] = Field(
        default_factory=list,
        alias="product_facts",
        description="분류 입력용 구조화 fact",
    )
    unresolvedFacts: List[ClassificationFact] = Field(
        default_factory=list,
        alias="unresolved_facts",
        description="근거 부족 또는 충돌 fact",
    )
    evidenceTraces: List[ReconstructionEvidenceTrace] = Field(
        default_factory=list,
        alias="evidence_traces",
        description="LLM reconstruction fact별 근거 trace",
    )
    missingFactReasons: List[ReconstructionMissingFactReason] = Field(
        default_factory=list,
        alias="missing_fact_reasons",
        description="복원 불가 fact별 근거 부족 사유",
    )
    conflicts: List[str] = Field(default_factory=list, description="fact 충돌 목록")
    normalizedFactTexts: List[str] = Field(
        default_factory=list,
        alias="normalized_fact_texts",
        description="분류 입력용 정규화 텍스트",
    )
    warnings: List[str] = Field(default_factory=list, description="복원 경고 목록")
    usedLlmReconstruction: bool = Field(
        default=False,
        alias="used_llm_reconstruction",
        description="LLM reconstruction 사용 여부",
    )
    fallbackReason: Optional[str] = Field(default=None, alias="fallback_reason", description="fallback 사유")
    dictionaryMatches: List[ProductDictionaryMatch] = Field(
        default_factory=list,
        alias="dictionary_matches",
        description="사전 기반 fact 매칭 결과",
    )
    sourceRefLabels: Dict[str, str] = Field(
        default_factory=dict,
        alias="source_ref_labels",
        description="evidence ID 표시 라벨",
    )
    sourceEvidencePreview: List[Dict[str, str]] = Field(
        default_factory=list,
        alias="source_evidence_preview",
        description="근거 evidence 미리보기",
    )

    @field_validator(
        "reconstructedTables",
        "productFacts",
        "unresolvedFacts",
        "evidenceTraces",
        "missingFactReasons",
        mode="before",
    )
    @classmethod
    def NormalizeObjectLists(cls, value: object) -> List[object]:
        return _NormalizeLlmObjectList(value)

    @field_validator("conflicts", "warnings", mode="before")
    @classmethod
    def NormalizeIssueTexts(cls, value: object) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if NormalizeWhiteSpace(value) else []
        if isinstance(value, Mapping):
            return [json.dumps(value, ensure_ascii=False, sort_keys=True)]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            normalizedValues: List[str] = []
            for item in value:
                if isinstance(item, str):
                    normalizedItem = NormalizeWhitespaceLines(item)
                elif isinstance(item, Mapping):
                    normalizedItem = json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                else:
                    normalizedItem = NormalizeWhiteSpace(str(item))
                if normalizedItem:
                    normalizedValues.append(normalizedItem)
            return normalizedValues
        normalizedValue = NormalizeWhiteSpace(str(value))
        return [normalizedValue] if normalizedValue else []


class _BoundModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="ignore",
    )


class _BoundNoticeField(_BoundModel):
    fieldName: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("fieldName", "field_name"),
    )
    fieldValue: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("fieldValue", "field_value"),
    )


class _BoundNoticeOption(_BoundModel):
    optionName: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("optionName", "option_name"),
    )
    fields: List[_BoundNoticeField] = Field(default_factory=list)


class _BoundParsedProductPage(_BoundModel):
    productName: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("productName", "product_name"),
    )
    shortDescription: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("shortDescription", "short_description"),
    )
    brandName: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("brandName", "brand_name"),
    )
    productNoticeFields: List[_BoundNoticeField] = Field(
        default_factory=list,
        validation_alias=AliasChoices("productNoticeFields", "product_notice_fields"),
    )
    productNoticeOptions: List[_BoundNoticeOption] = Field(
        default_factory=list,
        validation_alias=AliasChoices("productNoticeOptions", "product_notice_options"),
    )


class _BoundCollectionResult(_BoundModel):
    productPageUrl: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("productPageUrl", "product_page_url"),
    )
    parsedProductPage: _BoundParsedProductPage = Field(
        default_factory=_BoundParsedProductPage,
        validation_alias=AliasChoices("parsedProductPage", "parsed_product_page"),
    )


class _BoundOcrTable(_BoundModel):
    plainText: str = Field(
        default="",
        validation_alias=AliasChoices("plainText", "plain_text"),
    )
    validationStatus: str = Field(
        default="unverified",
        validation_alias=AliasChoices(
            "validationStatus",
            "validation_status",
        ),
    )
    validationIssues: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "validationIssues",
            "validation_issues",
        ),
    )


class _BoundRawTileText(_BoundModel):
    tileIndex: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("tileIndex", "tile_index"),
    )
    text: str = ""


class _BoundStructuredOcr(_BoundModel):
    tables: List[_BoundOcrTable] = Field(default_factory=list)
    rawTileTexts: List[_BoundRawTileText] = Field(
        default_factory=list,
        validation_alias=AliasChoices("rawTileTexts", "raw_tile_texts"),
    )


class _BoundOcrImageResult(_BoundModel):
    structuredOcr: _BoundStructuredOcr = Field(
        default_factory=_BoundStructuredOcr,
        validation_alias=AliasChoices("structuredOcr", "structured_ocr"),
    )


class ProductInputEvidenceBuilder:
    """Kurly 수집/OCR 산출물을 input reconstruction evidence로 변환한다."""

    def BuildFromPipelineParts(
        self,
        collectionResult: object,
        ocrImageResults: Sequence[object],
        combinedOcrText: str,
    ) -> InputEvidencePackage:
        records: List[InputEvidenceRecord] = []
        boundCollectionResult = _BoundCollectionResult.model_validate(collectionResult)
        boundOcrImageResults = [
            _BoundOcrImageResult.model_validate(ocrImageResult)
            for ocrImageResult in ocrImageResults
        ]
        self._AppendProductSummaryEvidence(
            records,
            boundCollectionResult.parsedProductPage,
        )
        self._AppendNoticeEvidence(records, boundCollectionResult.parsedProductPage)
        self._AppendOcrEvidence(
            records,
            boundOcrImageResults,
            boundCollectionResult.parsedProductPage,
        )
        if not any(record.sourceType.startswith("raw_ocr") for record in records):
            self._AppendRecord(
                records,
                sourceType="raw_ocr",
                text=combinedOcrText,
                sourceRef="combined_ocr_text",
            )
        return InputEvidencePackage(
            productPageUrl=boundCollectionResult.productPageUrl,
            records=records,
        )

    def _AppendProductSummaryEvidence(
        self,
        records: List[InputEvidenceRecord],
        parsedProductPage: _BoundParsedProductPage,
    ) -> None:
        summaryFields = [
            ("product_name", parsedProductPage.productName),
            ("short_description", parsedProductPage.shortDescription),
            ("brand_name", parsedProductPage.brandName),
        ]
        for fieldName, fieldValue in summaryFields:
            if fieldValue is None:
                continue
            self._AppendRecord(
                records,
                sourceType="product_summary",
                text="{0}: {1}".format(fieldName, fieldValue),
                sourceRef=fieldName,
            )

    def _AppendNoticeEvidence(
        self,
        records: List[InputEvidenceRecord],
        parsedProductPage: _BoundParsedProductPage,
    ) -> None:
        for optionIndex, noticeOption in enumerate(
            parsedProductPage.productNoticeOptions,
            start=1,
        ):
            if noticeOption.optionName is not None:
                self._AppendRecord(
                    records,
                    sourceType="notice_option",
                    text=noticeOption.optionName,
                    sourceRef="notice-option-{0}".format(optionIndex),
                    optionKey="option-{0}".format(optionIndex),
                )
            for fieldIndex, field in enumerate(noticeOption.fields, start=1):
                self._AppendNoticeFieldRecord(
                    records,
                    field,
                    "notice-option-{0}-field-{1}".format(optionIndex, fieldIndex),
                    optionKey="option-{0}".format(optionIndex),
                )
        if parsedProductPage.productNoticeOptions:
            return
        for fieldIndex, field in enumerate(
            parsedProductPage.productNoticeFields,
            start=1,
        ):
            self._AppendNoticeFieldRecord(
                records,
                field,
                "notice-field-{0}".format(fieldIndex),
            )

    def _AppendNoticeFieldRecord(
        self,
        records: List[InputEvidenceRecord],
        field: _BoundNoticeField,
        sourceRef: str,
        optionKey: Optional[str] = None,
    ) -> None:
        if field.fieldName is None and field.fieldValue is None:
            return
        if field.fieldName is not None and field.fieldValue is not None:
            text = "{0}: {1}".format(field.fieldName, field.fieldValue)
        else:
            text = field.fieldName if field.fieldName is not None else field.fieldValue
        self._AppendRecord(
            records,
            sourceType="notice_field",
            text=text or "",
            sourceRef=sourceRef,
            optionKey=optionKey,
        )

    def _AppendOcrEvidence(
        self,
        records: List[InputEvidenceRecord],
        ocrImageResults: Sequence[_BoundOcrImageResult],
        parsedProductPage: _BoundParsedProductPage,
    ) -> None:
        for imageIndex, imageResult in enumerate(ocrImageResults, start=1):
            for tableIndex, table in enumerate(imageResult.structuredOcr.tables, start=1):
                if table.validationStatus != "verified":
                    continue
                self._AppendRecord(
                    records,
                    sourceType=VLM_TABLE_SOURCE_TYPE,
                    text=table.plainText,
                    sourceRef="image-{0}-table-{1}".format(imageIndex, tableIndex),
                    optionKey=self._MatchOptionKey(
                        table.plainText,
                        parsedProductPage.productNoticeOptions,
                    ),
                    validationStatus=table.validationStatus,
                    validationIssues=table.validationIssues,
                )
            for rawTileText in imageResult.structuredOcr.rawTileTexts:
                self._AppendRecord(
                    records,
                    sourceType="raw_ocr_tile",
                    text=rawTileText.text,
                    sourceRef="image-{0}-tile-{1}".format(
                        imageIndex,
                        rawTileText.tileIndex if rawTileText.tileIndex is not None else 1,
                    ),
                    optionKey=self._MatchOptionKey(
                        rawTileText.text,
                        parsedProductPage.productNoticeOptions,
                    ),
                )

    def _AppendRecord(
        self,
        records: List[InputEvidenceRecord],
        sourceType: str,
        text: str,
        sourceRef: Optional[str],
        optionKey: Optional[str] = None,
        validationStatus: str = "unverified",
        validationIssues: Optional[Sequence[str]] = None,
    ) -> None:
        normalizedText = _StripOcrCollectionMarkers(text)
        if normalizedText == "":
            return
        records.append(
            InputEvidenceRecord(
                evidenceId="evidence-{0}".format(len(records) + 1),
                sourceType=sourceType,
                text=normalizedText,
                sourceRef=sourceRef,
                optionKey=optionKey,
                validationStatus=validationStatus,
                validationIssues=list(validationIssues or []),
            )
        )

    def _MatchOptionKey(
        self,
        text: str,
        options: Sequence[_BoundNoticeOption],
    ) -> Optional[str]:
        normalizedText = NormalizeWhiteSpace(text).lower().replace(" ", "")
        matchedOptionKeys: List[str] = []
        for optionIndex, option in enumerate(options, start=1):
            anchors = self._BuildOptionAnchors(option)
            if anchors and any(anchor in normalizedText for anchor in anchors):
                matchedOptionKeys.append("option-{0}".format(optionIndex))
        if len(matchedOptionKeys) == 1:
            return matchedOptionKeys[0]

        optionTokenSets = [
            self._BuildOptionMatchTokens(option)
            for option in options
        ]
        repeatedTokens = {
            token
            for token in set().union(*optionTokenSets)
            if sum(token in tokenSet for tokenSet in optionTokenSets) > 1
        }
        scoredMatches: List[tuple[int, str]] = []
        for optionIndex, tokenSet in enumerate(optionTokenSets, start=1):
            distinctiveTokens = [
                token for token in tokenSet if token not in repeatedTokens
            ]
            score = sum(
                len(token)
                for token in distinctiveTokens
                if token in normalizedText
            )
            if score > 0:
                scoredMatches.append((score, "option-{0}".format(optionIndex)))
        if not scoredMatches:
            return None
        scoredMatches.sort(reverse=True)
        if len(scoredMatches) > 1 and scoredMatches[0][0] == scoredMatches[1][0]:
            return None
        return scoredMatches[0][1]

    def _BuildOptionAnchors(self, option: _BoundNoticeOption) -> set[str]:
        rawValues = [
            option.optionName or "",
            *[
                field.fieldValue or ""
                for field in option.fields
                if field.fieldValue is not None
            ],
        ]
        anchors = {
            NormalizeWhiteSpace(value).lower().replace(" ", "")
            for value in rawValues
            if len(NormalizeWhiteSpace(value)) >= 3
        }
        anchors.update(
            "{0}{1}".format(value, unit)
            for rawValue in rawValues
            for value, unit in _ExtractQuantityTokens(rawValue)
        )
        return anchors

    def _BuildOptionMatchTokens(self, option: _BoundNoticeOption) -> set[str]:
        rawValues = [
            option.optionName or "",
            *[
                field.fieldValue or ""
                for field in option.fields
                if field.fieldValue is not None
            ],
        ]
        tokens: set[str] = set()
        for rawValue in rawValues:
            normalizedValue = NormalizeWhiteSpace(rawValue).lower()
            compactValue = _CompactEvidenceText(normalizedValue)
            if len(compactValue) >= 3:
                tokens.add(compactValue)
            tokens.update(
                token
                for token in re.findall(r"[0-9a-z가-힣]+", normalizedValue)
                if len(token) >= 2
            )
        return tokens


class ProductFactReconstructionValidator:
    """LLM 또는 deterministic 복원 결과의 source reference를 검증한다."""

    def Validate(
        self,
        result: InputReconstructionResult,
        evidencePackage: InputEvidencePackage,
    ) -> InputReconstructionResult:
        validEvidenceIds = {record.evidenceId for record in evidencePackage.records}
        evidenceById = {
            record.evidenceId: record
            for record in evidencePackage.records
        }
        productFacts: List[ClassificationFact] = []
        unresolvedFacts: List[ClassificationFact] = []
        warnings = list(result.warnings)
        reconstructedTables = self._CleanReconstructedTables(
            result.reconstructedTables,
            validEvidenceIds=validEvidenceIds,
            evidenceById=evidenceById,
            warnings=warnings,
        )
        evidenceTraces = self._CleanEvidenceTraces(
            result.evidenceTraces,
            validEvidenceIds=validEvidenceIds,
            warnings=warnings,
        )
        missingFactReasons = self._CleanMissingFactReasons(
            result.missingFactReasons,
            validEvidenceIds=validEvidenceIds,
            warnings=warnings,
        )
        for factRecord in result.productFacts:
            cleanedFactRecord = self._CleanFactRecord(
                factRecord,
                warnings=warnings,
            )
            if cleanedFactRecord is None:
                continue
            invalidRefs = [
                sourceRef
                for sourceRef in cleanedFactRecord.sourceRefs
                if sourceRef not in validEvidenceIds
            ]
            if invalidRefs:
                warnings.append(
                    "rejected_fact_invalid_source_refs field={0} refs={1}".format(
                        cleanedFactRecord.fieldName,
                        ",".join(invalidRefs),
                    )
                )
                productFacts.append(
                    cleanedFactRecord.model_copy(
                        update={"validationStatus": "rejected"}
                    )
                )
                continue
            validationIssue = self._ValidateQuantityEvidence(
                cleanedFactRecord.normalizedValue,
                cleanedFactRecord.sourceRefs,
                evidenceById,
            )
            validationIssue = validationIssue or self._ValidateTextEvidence(
                cleanedFactRecord.fieldName,
                cleanedFactRecord.normalizedValue,
                cleanedFactRecord.sourceRefs,
                evidenceById,
            )
            if validationIssue is not None:
                warnings.append(
                    "unresolved_fact_evidence field={0} reason={1}".format(
                        cleanedFactRecord.fieldName,
                        validationIssue,
                    )
                )
                unresolvedFacts.append(
                    cleanedFactRecord.model_copy(
                        update={"validationStatus": "unresolved"}
                    )
                )
                continue
            productFacts.append(cleanedFactRecord)
        for factRecord in result.unresolvedFacts:
            cleanedFactRecord = self._CleanFactRecord(
                factRecord,
                warnings=warnings,
            )
            if cleanedFactRecord is not None:
                unresolvedFacts.append(cleanedFactRecord)
        productFacts, promotedUnresolvedFacts = self._PromoteTableIngredientFacts(
            productFacts,
            reconstructedTables,
            evidenceById,
            warnings,
        )
        unresolvedFacts.extend(promotedUnresolvedFacts)
        normalizedFactTexts = self._BuildNormalizedFactTexts(productFacts)
        return result.model_copy(
            update={
                "reconstructedTables": reconstructedTables,
                "productFacts": productFacts,
                "unresolvedFacts": unresolvedFacts,
                "evidenceTraces": evidenceTraces,
                "missingFactReasons": missingFactReasons,
                "normalizedFactTexts": normalizedFactTexts,
                "warnings": warnings,
            }
        )

    def _CleanEvidenceTraces(
        self,
        traces: Sequence[ReconstructionEvidenceTrace],
        *,
        validEvidenceIds: set[str],
        warnings: List[str],
    ) -> List[ReconstructionEvidenceTrace]:
        cleanedTraces: List[ReconstructionEvidenceTrace] = []
        for trace in traces:
            fieldName = NormalizeWhiteSpace(trace.fieldName)
            normalizedValue = NormalizeWhitespaceLines(trace.normalizedValue)
            selectedSpan = NormalizeWhitespaceLines(trace.selectedSpan)
            decisionReason = NormalizeWhiteSpace(trace.decisionReason)
            status = NormalizeWhiteSpace(trace.status)
            unresolvedReason = NormalizeWhiteSpace(trace.unresolvedReason)
            if not any(
                (fieldName, normalizedValue, selectedSpan, decisionReason, unresolvedReason)
            ):
                continue
            cleanedTraces.append(
                trace.model_copy(
                    update={
                        "fieldName": fieldName,
                        "normalizedValue": normalizedValue,
                        "selectedSpan": selectedSpan,
                        "decisionReason": decisionReason,
                        "status": status,
                        "unresolvedReason": unresolvedReason,
                        "sourceRefs": self._CleanSourceRefs(
                            trace.sourceRefs,
                            validEvidenceIds=validEvidenceIds,
                            warnings=warnings,
                            context="evidence_trace field={0}".format(fieldName),
                        ),
                    }
                )
            )
        return cleanedTraces

    def _CleanMissingFactReasons(
        self,
        reasons: Sequence[ReconstructionMissingFactReason],
        *,
        validEvidenceIds: set[str],
        warnings: List[str],
    ) -> List[ReconstructionMissingFactReason]:
        cleanedReasons: List[ReconstructionMissingFactReason] = []
        for reasonRecord in reasons:
            factName = NormalizeWhiteSpace(reasonRecord.factName)
            reason = NormalizeWhitespaceLines(reasonRecord.reason)
            if not factName and not reason:
                continue
            cleanedReasons.append(
                reasonRecord.model_copy(
                    update={
                        "factName": factName,
                        "reason": reason,
                        "sourceRefs": self._CleanSourceRefs(
                            reasonRecord.sourceRefs,
                            validEvidenceIds=validEvidenceIds,
                            warnings=warnings,
                            context="missing_fact_reason fact={0}".format(factName),
                        ),
                    }
                )
            )
        return cleanedReasons

    def _PromoteTableIngredientFacts(
        self,
        productFacts: Sequence[ClassificationFact],
        reconstructedTables: Sequence[ReconstructionTable],
        evidenceById: Mapping[str, InputEvidenceRecord],
        warnings: List[str],
    ) -> tuple[List[ClassificationFact], List[ClassificationFact]]:
        promotedProductFacts = list(productFacts)
        unresolvedFacts: List[ClassificationFact] = []
        seenIngredientValues = {
            _CompactEvidenceText(fact.normalizedValue)
            for fact in promotedProductFacts
            if self._IsIngredientClassificationField(fact.fieldName)
        }
        for table in reconstructedTables:
            for row in table.rows:
                if not self._ShouldPromoteTableRow(row):
                    continue
                valueKey = _CompactEvidenceText(row.normalizedValue)
                if valueKey == "" or valueKey in seenIngredientValues:
                    continue
                factRecord = ClassificationFact(
                    fieldName=self._BuildPromotedFactFieldName(table, row),
                    normalizedValue=row.normalizedValue,
                    sourceRefs=list(row.sourceRefs or table.sourceRefs),
                    correctionType="llm_reconstructed",
                    validationStatus="accepted",
                )
                validationIssue = None
                if row.validationStatus == "review_required":
                    validationIssue = ",".join(row.validationIssues) or "table_row_review_required"
                validationIssue = validationIssue or self._ValidateQuantityEvidence(
                    factRecord.normalizedValue,
                    factRecord.sourceRefs,
                    evidenceById,
                )
                validationIssue = validationIssue or self._ValidateTextEvidence(
                    factRecord.fieldName,
                    factRecord.normalizedValue,
                    factRecord.sourceRefs,
                    evidenceById,
                )
                if validationIssue is not None:
                    warnings.append(
                        "unresolved_promoted_ingredient_fact field={0} reason={1}".format(
                            factRecord.fieldName,
                            validationIssue,
                        )
                    )
                    unresolvedFacts.append(
                        factRecord.model_copy(update={"validationStatus": "unresolved"})
                    )
                    continue
                promotedProductFacts.append(factRecord)
                seenIngredientValues.add(valueKey)
        return promotedProductFacts, unresolvedFacts

    def _ShouldPromoteTableRow(self, row: ReconstructionTableRow) -> bool:
        return self._IsIngredientClassificationField(
            row.fieldName,
        ) and not self._IsNutritionField(row.fieldName)

    def _IsIngredientClassificationField(self, fieldName: str) -> bool:
        compactFieldName = _CompactEvidenceText(fieldName)
        return any(
            marker in compactFieldName
            for marker in CLASSIFICATION_INGREDIENT_FIELD_MARKERS
        )

    def _IsNutritionField(self, fieldName: str) -> bool:
        compactFieldName = _CompactEvidenceText(fieldName)
        return any(marker in compactFieldName for marker in NUTRITION_FIELD_MARKERS)

    def _BuildPromotedFactFieldName(
        self,
        table: ReconstructionTable,
        row: ReconstructionTableRow,
    ) -> str:
        fieldName = NormalizeWhiteSpace(row.fieldName)
        tableName = NormalizeWhiteSpace(table.tableName)
        if (
            tableName == ""
            or tableName == "Reconstructed table"
            or tableName in fieldName
        ):
            return fieldName
        return "{0} ({1})".format(fieldName, tableName)

    def _CleanReconstructedTables(
        self,
        tables: Sequence[ReconstructionTable],
        *,
        validEvidenceIds: set[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
        warnings: List[str],
    ) -> List[ReconstructionTable]:
        cleanedTables: List[ReconstructionTable] = []
        for table in tables:
            tableName = NormalizeWhiteSpace(table.tableName) or "Reconstructed table"
            tableSourceRefs = self._CleanSourceRefs(
                table.sourceRefs,
                validEvidenceIds=validEvidenceIds,
                warnings=warnings,
                context="table={0}".format(tableName),
            )
            cleanedRows: List[ReconstructionTableRow] = []
            for row in table.rows:
                fieldName = NormalizeWhiteSpace(
                    _StripOcrCollectionMarkers(row.fieldName)
                )
                normalizedValue = NormalizeWhitespaceLines(
                    _StripOcrCollectionMarkers(row.normalizedValue)
                )
                if fieldName == "" or normalizedValue == "":
                    warnings.append(
                        "rejected_table_row_empty_field_or_value table={0} field={1}".format(
                            tableName,
                            row.fieldName,
                        )
                    )
                    continue
                rowSourceRefs = self._CleanSourceRefs(
                    row.sourceRefs,
                    validEvidenceIds=validEvidenceIds,
                    warnings=warnings,
                    context="table={0} field={1}".format(tableName, fieldName),
                )
                rawValidationRefs = self._BuildTableRawValidationRefs(
                    rowSourceRefs or tableSourceRefs,
                    tableSourceRefs,
                    evidenceById,
                )
                validationIssue = self._ValidateQuantityEvidence(
                    normalizedValue,
                    rawValidationRefs or rowSourceRefs or tableSourceRefs,
                    evidenceById,
                )
                validationIssue = validationIssue or self._ValidateTextEvidence(
                    fieldName,
                    normalizedValue,
                    rawValidationRefs or rowSourceRefs or tableSourceRefs,
                    evidenceById,
                )
                validationStatus = self._ResolveRowValidationStatus(
                    normalizedValue,
                    rawValidationRefs or rowSourceRefs or tableSourceRefs,
                    evidenceById,
                    validationIssue,
                )
                cleanedRows.append(
                    row.model_copy(
                        update={
                            "fieldName": fieldName,
                            "normalizedValue": normalizedValue,
                            "unit": NormalizeWhiteSpace(row.unit),
                            "dailyValuePercent": NormalizeWhiteSpace(
                                row.dailyValuePercent,
                            ),
                            "sourceRefs": rowSourceRefs or tableSourceRefs,
                            "validationStatus": validationStatus,
                            "validationIssues": (
                                [] if validationIssue is None else [validationIssue]
                            ),
                        }
                    )
                )
            if cleanedRows:
                cleanedTables.append(
                    table.model_copy(
                        update={
                            "tableName": tableName,
                            "sourceRefs": tableSourceRefs,
                            "rows": cleanedRows,
                        }
                    )
                )
        return cleanedTables

    def _BuildTableRawValidationRefs(
        self,
        rowSourceRefs: Sequence[str],
        tableSourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
    ) -> List[str]:
        tableRefs = [
            sourceRef
            for sourceRef in rowSourceRefs
            if (
                sourceRef in evidenceById
                and _IsVlmTableSourceType(evidenceById[sourceRef].sourceType)
            )
        ]
        if tableRefs:
            return tableRefs
        return [
            sourceRef
            for sourceRef in tableSourceRefs
            if (
                sourceRef in evidenceById
                and _IsVlmTableSourceType(evidenceById[sourceRef].sourceType)
            )
        ]

    def _FindTableRawValue(
        self,
        fieldName: str,
        sourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
    ) -> Optional[str]:
        fieldKey = _CompactEvidenceText(fieldName)
        if not fieldKey:
            return None
        for sourceRef in sourceRefs:
            record = evidenceById.get(sourceRef)
            if record is None or not _IsVlmTableSourceType(record.sourceType):
                continue
            for line in record.text.splitlines():
                cells = [
                    NormalizeWhiteSpace(cell)
                    for cell in line.split("|")
                    if NormalizeWhiteSpace(cell)
                ]
                for cellIndex, cell in enumerate(cells[:-1]):
                    if _CompactEvidenceText(cell) == fieldKey:
                        return cells[cellIndex + 1]
        return None

    def _BuildRelatedRawOcrRefs(
        self,
        sourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
    ) -> List[str]:
        imageIndexes = {
            match.group(1)
            for sourceRef in sourceRefs
            if (
                sourceRef in evidenceById
                and evidenceById[sourceRef].sourceRef is not None
                and (
                    match := re.match(
                        r"^image-(\d+)-table-",
                        evidenceById[sourceRef].sourceRef or "",
                    )
                )
            )
        }
        if not imageIndexes:
            return []
        return [
            record.evidenceId
            for record in evidenceById.values()
            if (
                record.sourceType == "raw_ocr_tile"
                and record.sourceRef is not None
                and any(
                    record.sourceRef.startswith("image-{0}-tile-".format(imageIndex))
                    for imageIndex in imageIndexes
                )
            )
        ]

    def _FindRawOcrHint(
        self,
        fieldName: str,
        rawValue: str,
        sourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
    ) -> str:
        fieldKey = _CompactEvidenceText(fieldName)
        valueTokens = _ExtractQuantityTokens(rawValue)
        for sourceRef in sourceRefs:
            record = evidenceById.get(sourceRef)
            if record is None or record.sourceType != "raw_ocr_tile":
                continue
            lines = [
                NormalizeWhiteSpace(line)
                for line in record.text.splitlines()
                if NormalizeWhiteSpace(line)
            ]
            for lineIndex, line in enumerate(lines):
                if fieldKey and fieldKey in _CompactEvidenceText(line):
                    return " / ".join(lines[lineIndex : lineIndex + 3])
                if valueTokens and valueTokens <= _ExtractQuantityTokens(line):
                    return line
        return ""

    def _MergeSourceRefs(
        self,
        preferredRefs: Sequence[str],
        sourceRefs: Sequence[str],
    ) -> List[str]:
        return list(dict.fromkeys([*preferredRefs, *sourceRefs]))

    def _CleanSourceRefs(
        self,
        sourceRefs: Sequence[str],
        *,
        validEvidenceIds: set[str],
        warnings: List[str],
        context: str,
    ) -> List[str]:
        cleanedRefs: List[str] = []
        invalidRefs: List[str] = []
        for sourceRef in sourceRefs:
            sourceRefText = NormalizeWhiteSpace(str(sourceRef))
            if sourceRefText == "":
                continue
            if sourceRefText not in validEvidenceIds:
                invalidRefs.append(sourceRefText)
                continue
            if sourceRefText not in cleanedRefs:
                cleanedRefs.append(sourceRefText)
        if invalidRefs:
            warnings.append(
                "rejected_invalid_source_refs {0} refs={1}".format(
                    context,
                    ",".join(invalidRefs),
                )
            )
        return cleanedRefs

    def _CleanFactRecord(
        self,
        factRecord: ClassificationFact,
        *,
        warnings: List[str],
    ) -> Optional[ClassificationFact]:
        fieldName = _StripOcrCollectionMarkers(factRecord.fieldName)
        normalizedValue = _StripOcrCollectionMarkers(factRecord.normalizedValue)
        displayValue = normalizedValue
        if _IsGenericOcrFieldName(fieldName):
            splitText = _SplitKnownFieldText(displayValue)
            if splitText is None:
                warnings.append(
                    "rejected_fact_generic_ocr_field field={0}".format(
                        factRecord.fieldName,
                    )
                )
                return None
            fieldName, splitValue = splitText
            normalizedValue = splitValue
        if fieldName == "" or normalizedValue == "":
            warnings.append(
                "rejected_fact_empty_field_or_value field={0}".format(
                    factRecord.fieldName,
                )
            )
            return None
        return factRecord.model_copy(
            update={
                "fieldName": NormalizeWhiteSpace(fieldName),
                "normalizedValue": NormalizeWhitespaceLines(
                    normalizedValue,
                ),
            }
        )

    def _ValidateQuantityEvidence(
        self,
        value: str,
        sourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
    ) -> Optional[str]:
        valueTokens = _ExtractQuantityTokens(value)
        if not valueTokens:
            return None
        evidenceText = "\n".join(
            evidenceById[sourceRef].text
            for sourceRef in sourceRefs
            if sourceRef in evidenceById
        )
        evidenceTokens = _ExtractQuantityTokens(evidenceText)
        missingTokens = valueTokens - evidenceTokens
        if missingTokens:
            return "missing_quantity_tokens:{0}".format(
                ",".join(
                    "{0}{1}".format(number, unit)
                    for number, unit in sorted(missingTokens)
                )
            )
        matchingRecords = [
            evidenceById[sourceRef]
            for sourceRef in sourceRefs
            if (
                sourceRef in evidenceById
                and valueTokens <= _ExtractQuantityTokens(
                    evidenceById[sourceRef].text
                )
            )
        ]
        if any(
            not _IsVlmTableSourceType(record.sourceType)
            or record.validationStatus == "verified"
            for record in matchingRecords
        ):
            return None
        validationIssues = [
            issue
            for record in matchingRecords
            for issue in record.validationIssues
            if issue
        ]
        if validationIssues:
            return "structured_source_validation:{0}".format(
                ",".join(dict.fromkeys(validationIssues))
            )
        return None

    def _ResolveRowValidationStatus(
        self,
        value: str,
        sourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
        validationIssue: Optional[str],
    ) -> str:
        if validationIssue is not None:
            return "review_required"
        if not _ExtractQuantityTokens(value):
            return "evidence_matched"
        if any(
            evidenceById[sourceRef].validationStatus == "verified"
            for sourceRef in sourceRefs
            if sourceRef in evidenceById
        ):
            return "verified"
        return "evidence_matched"

    def _ValidateTextEvidence(
        self,
        fieldName: str,
        value: str,
        sourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
    ) -> Optional[str]:
        if _ExtractQuantityTokens(value) and not _HasMeaningfulNonQuantityText(value):
            return None
        compactValue = _CompactEvidenceText(value)
        if len(compactValue) < 3:
            return None
        if self._IsIngredientClassificationField(fieldName):
            summaryOnlyIssue = self._ValidateIngredientSourceTypes(
                sourceRefs,
                evidenceById,
            )
            if summaryOnlyIssue is not None:
                return summaryOnlyIssue
            sectionIssue = self._ValidateIngredientSectionEvidence(
                value,
                sourceRefs,
                evidenceById,
            )
            if sectionIssue is not None:
                return sectionIssue
        if any(
            compactValue
            in _CompactEvidenceText(evidenceById[sourceRef].text)
            for sourceRef in sourceRefs
            if sourceRef in evidenceById
        ):
            return None
        valueTokens = _ExtractMeaningfulEvidenceTokens(value)
        evidenceTokens = {
            token
            for sourceRef in sourceRefs
            if sourceRef in evidenceById
            for token in _ExtractMeaningfulEvidenceTokens(
                evidenceById[sourceRef].text,
                minLength=1,
            )
        }
        if valueTokens and all(
            _IsTokenCoveredByEvidenceParts(token, evidenceTokens)
            for token in valueTokens
        ):
            return None
        if self._HasSufficientCorrectedTextCoverage(valueTokens, evidenceTokens):
            return None
        return "normalized_value_not_found_in_source"

    def _ValidateIngredientSourceTypes(
        self,
        sourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
    ) -> Optional[str]:
        matchedSourceTypes = [
            evidenceById[sourceRef].sourceType
            for sourceRef in sourceRefs
            if sourceRef in evidenceById
        ]
        if matchedSourceTypes and all(
            sourceType == "product_summary"
            for sourceType in matchedSourceTypes
        ):
            return "ingredient_fact_requires_label_or_ocr_evidence"
        return None

    def _ValidateIngredientSectionEvidence(
        self,
        value: str,
        sourceRefs: Sequence[str],
        evidenceById: Mapping[str, InputEvidenceRecord],
    ) -> Optional[str]:
        sectionTexts = [
            sectionText
            for sourceRef in sourceRefs
            if sourceRef in evidenceById
            and evidenceById[sourceRef].sourceType == "raw_ocr_tile"
            and _HasIngredientMarketingSectionBoundary(evidenceById[sourceRef].text)
            for sectionText in [_ExtractIngredientSectionText(evidenceById[sourceRef].text)]
            if sectionText
        ]
        if not sectionTexts:
            return None
        valueTokens = _ExtractMeaningfulEvidenceTokens(value)
        sectionTokens = _ExtractMeaningfulEvidenceTokens(
            "\n".join(sectionTexts),
            minLength=1,
        )
        fullTokens = {
            token
            for sourceRef in sourceRefs
            if sourceRef in evidenceById
            for token in _ExtractMeaningfulEvidenceTokens(
                evidenceById[sourceRef].text,
                minLength=1,
            )
        }
        outsideTokens = sorted(
            token
            for token in valueTokens
            if not _IsTokenTextuallyCovered(token, sectionTokens)
            and _IsTokenTextuallyCovered(token, fullTokens)
        )
        if outsideTokens:
            return "ingredient_value_outside_ingredient_section:{0}".format(
                ",".join(outsideTokens)
            )
        return None

    def _HasSufficientCorrectedTextCoverage(
        self,
        valueTokens: set[str],
        evidenceTokens: set[str],
    ) -> bool:
        if len(valueTokens) < 4:
            return False
        coveredTokenCount = sum(
            1
            for token in valueTokens
            if _IsTokenCoveredByEvidenceParts(token, evidenceTokens)
        )
        requiredTokenCount = max(2, (len(valueTokens) + 1) // 2)
        return coveredTokenCount >= requiredTokenCount

    def _BuildNormalizedFactTexts(
        self,
        productFacts: Sequence[ClassificationFact],
    ) -> List[str]:
        factTexts: List[str] = []
        seenFactTexts: set[str] = set()
        for factRecord in productFacts:
            if factRecord.validationStatus != "accepted":
                continue
            if _IsGenericOcrFieldName(factRecord.fieldName):
                continue
            factText = factRecord.ToFactText()
            factText = _StripOcrCollectionMarkers(factText)
            if factText and factText not in seenFactTexts:
                seenFactTexts.add(factText)
                factTexts.append(factText)
        return factTexts


class DeterministicProductFactReconstructor:
    """Dictionary correction과 기존 OCR normalizer로 기본 product facts를 만든다."""

    def __init__(
        self,
        dictionaryRetriever: ProductDictionaryRetriever,
        ocrFactNormalizer: Optional[ProductOcrFactNormalizer] = None,
        validator: Optional[ProductFactReconstructionValidator] = None,
    ) -> None:
        self._dictionaryRetriever = dictionaryRetriever
        self._ocrFactNormalizer = ocrFactNormalizer or ProductOcrFactNormalizer()
        self._validator = validator or ProductFactReconstructionValidator()

    def Reconstruct(
        self,
        evidencePackage: InputEvidencePackage,
    ) -> InputReconstructionResult:
        dictionaryMatches = self._dictionaryRetriever.FindMatches(
            [record.text for record in evidencePackage.records]
        )
        factRecords = self._BuildFactRecords(evidencePackage, dictionaryMatches)
        result = InputReconstructionResult(
            productFacts=factRecords,
            usedLlmReconstruction=False,
            fallbackReason="llm_reconstruction_not_used",
            dictionaryMatches=dictionaryMatches,
        )
        return self._validator.Validate(result, evidencePackage)

    def _BuildFactRecords(
        self,
        evidencePackage: InputEvidencePackage,
        dictionaryMatches: Sequence[ProductDictionaryMatch],
    ) -> List[ClassificationFact]:
        factRecords: List[ClassificationFact] = []
        for record in evidencePackage.records:
            if record.sourceType in {"notice_field", "product_summary"}:
                factRecord = self._BuildFactRecordFromText(record, dictionaryMatches)
                if factRecord is not None:
                    factRecords.append(factRecord)
        combinedText = "\n".join(record.text for record in evidencePackage.records)
        normalizedResult = self._ocrFactNormalizer.Normalize(combinedText)
        for factText in normalizedResult.factTexts:
            factRecord = self._BuildFactRecordFromNormalizedText(
                factText,
                evidencePackage,
                dictionaryMatches,
            )
            if factRecord is not None:
                factRecords.append(factRecord)
        return self._DeduplicateFactRecords(factRecords)

    def _BuildFactRecordFromText(
        self,
        record: InputEvidenceRecord,
        dictionaryMatches: Sequence[ProductDictionaryMatch],
    ) -> Optional[ClassificationFact]:
        splitText = _SplitFieldText(record.text)
        if splitText is None:
            return None
        fieldName, fieldValue = splitText
        if _IsGenericOcrFieldName(fieldName):
            return None
        normalizedValue = self.ApplyDictionaryCorrections(
            fieldValue,
            dictionaryMatches,
        )
        correctionType = (
            "dictionary_fuzzy"
            if normalizedValue != fieldValue
            and any(match.matchType == "fuzzy" for match in dictionaryMatches)
            else "dictionary_exact"
            if normalizedValue != fieldValue
            else "none"
        )
        return ClassificationFact(
            fieldName=fieldName,
            normalizedValue=normalizedValue,
            sourceRefs=[record.evidenceId],
            correctionType=correctionType,
            validationStatus="accepted",
        )

    def _BuildFactRecordFromNormalizedText(
        self,
        factText: str,
        evidencePackage: InputEvidencePackage,
        dictionaryMatches: Sequence[ProductDictionaryMatch],
    ) -> Optional[ClassificationFact]:
        sourceRefs = self._FindSourceRefs(factText, evidencePackage)
        if not sourceRefs:
            sourceRefs = [evidencePackage.records[0].evidenceId] if evidencePackage.records else []
        splitText = _SplitFieldText(factText)
        if splitText is None:
            return None
        else:
            fieldName, fieldValue = splitText
        if _IsGenericOcrFieldName(fieldName):
            splitText = _SplitKnownFieldText(fieldValue)
            if splitText is None:
                return None
            fieldName, fieldValue = splitText
        normalizedValue = self.ApplyDictionaryCorrections(
            fieldValue,
            dictionaryMatches,
        )
        return ClassificationFact(
            fieldName=fieldName,
            normalizedValue=normalizedValue,
            sourceRefs=sourceRefs,
            correctionType="dictionary_exact" if normalizedValue != fieldValue else "none",
            validationStatus="accepted",
        )

    def ApplyDictionaryCorrections(
        self,
        text: str,
        dictionaryMatches: Sequence[ProductDictionaryMatch],
    ) -> str:
        normalizedText = text
        for dictionaryMatch in dictionaryMatches:
            if dictionaryMatch.correctionAction != "auto_corrected":
                continue
            if dictionaryMatch.matchedText == dictionaryMatch.canonicalName:
                continue
            normalizedText = normalizedText.replace(
                dictionaryMatch.matchedText,
                dictionaryMatch.canonicalName,
            )
        return normalizedText

    def _FindSourceRefs(
        self,
        factText: str,
        evidencePackage: InputEvidencePackage,
    ) -> List[str]:
        normalizedFactText = NormalizeWhiteSpace(factText)
        splitFact = _SplitFieldText(factText)
        factValue = splitFact[1] if splitFact is not None else factText
        normalizedFactValue = NormalizeWhiteSpace(factValue)
        factQuantityTokens = _ExtractQuantityTokens(factValue)
        sourceRefs: List[str] = []
        for record in evidencePackage.records:
            normalizedEvidenceText = NormalizeWhiteSpace(record.text)
            evidenceQuantityTokens = _ExtractQuantityTokens(record.text)
            if (
                normalizedFactText in normalizedEvidenceText
                or normalizedFactValue in normalizedEvidenceText
                or (
                    factQuantityTokens
                    and factQuantityTokens <= evidenceQuantityTokens
                )
            ):
                sourceRefs.append(record.evidenceId)
        return sourceRefs

    def _DeduplicateFactRecords(
        self,
        factRecords: Sequence[ClassificationFact],
    ) -> List[ClassificationFact]:
        deduplicatedRecords: List[ClassificationFact] = []
        seenFactTexts: set[str] = set()
        for factRecord in factRecords:
            factText = factRecord.ToFactText()
            if factText in seenFactTexts:
                continue
            seenFactTexts.add(factText)
            deduplicatedRecords.append(factRecord)
        return deduplicatedRecords


class ProductFactReconstructionAgent:
    """Few-shot LLM을 이용해 table/raw OCR evidence를 상품 input fact로 복원한다."""

    def __init__(
        self,
        runtimeAdapter: Optional[RuntimeAdapter[object]],
        validator: Optional[ProductFactReconstructionValidator] = None,
        maxTokens: int = DEFAULT_LLM_INPUT_RECONSTRUCTION_MAX_TOKENS,
        artifactRootPath: Optional[Path] = None,
    ) -> None:
        self._runtimeAdapter = runtimeAdapter
        self._validator = validator or ProductFactReconstructionValidator()
        self._maxTokens = max(1, maxTokens)
        self._artifactRootPath = artifactRootPath

    def Reconstruct(
        self,
        evidencePackage: InputEvidencePackage,
    ) -> InputReconstructionResult:
        if self._runtimeAdapter is None:
            return InputReconstructionResult(
                warnings=["llm_reconstruction_failed: runtime adapter is not configured"],
                fallbackReason="llm_runtime_not_configured",
            )

        request = self._BuildRequest(evidencePackage)
        self._TryWriteArtifact(
            evidencePackage,
            "llm-input-reconstruction-request.json",
            {
                "product_page_url": evidencePackage.productPageUrl,
                "evidence_record_count": len(evidencePackage.records),
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )
        try:
            response = self._runtimeAdapter.Generate(request)
        except RuntimeError as error:
            self._TryWriteArtifact(
                evidencePackage,
                "llm-input-reconstruction-error.json",
                {
                    "product_page_url": evidencePackage.productPageUrl,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
            return InputReconstructionResult(
                warnings=["llm_reconstruction_failed: {0}".format(error)],
                fallbackReason="llm_reconstruction_failed",
            )

        self._TryWriteArtifact(
            evidencePackage,
            "llm-input-reconstruction-response.json",
            response.model_dump(mode="json", by_alias=True),
        )
        try:
            return self._BuildValidatedResult(
                response.generatedText,
                evidencePackage,
            )
        except (ValueError, ValidationError) as error:
            repairedResult = self._TryRepairResponse(
                evidencePackage,
                response.generatedText,
                error,
            )
            if repairedResult is not None:
                return repairedResult
            self._TryWriteArtifact(
                evidencePackage,
                "llm-input-reconstruction-error.json",
                {
                    "product_page_url": evidencePackage.productPageUrl,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
            return InputReconstructionResult(
                warnings=["llm_reconstruction_failed: {0}".format(error)],
                fallbackReason="llm_reconstruction_failed",
            )
        except RuntimeError as error:
            self._TryWriteArtifact(
                evidencePackage,
                "llm-input-reconstruction-error.json",
                {
                    "product_page_url": evidencePackage.productPageUrl,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
            return InputReconstructionResult(
                warnings=["llm_reconstruction_failed: {0}".format(error)],
                fallbackReason="llm_reconstruction_failed",
            )

    def _BuildValidatedResult(
        self,
        generatedText: str,
        evidencePackage: InputEvidencePackage,
    ) -> InputReconstructionResult:
        payload = self._ParseJsonPayload(generatedText)
        result = InputReconstructionResult.model_validate(payload)
        result = result.model_copy(
            update={
                "normalizedFactTexts": [],
                "usedLlmReconstruction": True,
                "fallbackReason": None,
                "dictionaryMatches": [],
            }
        )
        validatedResult = self._validator.Validate(result, evidencePackage)
        if not validatedResult.productFacts:
            return validatedResult.model_copy(
                update={
                    "usedLlmReconstruction": False,
                    "fallbackReason": "llm_reconstruction_no_product_facts",
                    "warnings": [
                        *validatedResult.warnings,
                        "llm_reconstruction_no_product_facts",
                    ],
                }
            )
        return validatedResult

    def _TryRepairResponse(
        self,
        evidencePackage: InputEvidencePackage,
        generatedText: str,
        originalError: Exception,
    ) -> InputReconstructionResult | None:
        if self._runtimeAdapter is None:
            return None

        repairRequest = self._BuildRepairRequest(generatedText, originalError)
        self._TryWriteArtifact(
            evidencePackage,
            "llm-input-reconstruction-repair-request.json",
            {
                "product_page_url": evidencePackage.productPageUrl,
                "request": repairRequest.model_dump(mode="json", by_alias=True),
            },
        )
        try:
            response = self._runtimeAdapter.Generate(repairRequest)
            self._TryWriteArtifact(
                evidencePackage,
                "llm-input-reconstruction-repair-response.json",
                response.model_dump(mode="json", by_alias=True),
            )
            return self._BuildValidatedResult(response.generatedText, evidencePackage)
        except (ValueError, ValidationError, RuntimeError) as error:
            self._TryWriteArtifact(
                evidencePackage,
                "llm-input-reconstruction-repair-error.json",
                {
                    "product_page_url": evidencePackage.productPageUrl,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                    "original_error_type": type(originalError).__name__,
                    "original_error_message": str(originalError),
                },
            )
            return None

    def _BuildRequest(
        self,
        evidencePackage: InputEvidencePackage,
    ) -> LlmRequest:
        evidencePayload = {
            "evidence": [
                evidenceRecord.model_dump(mode="json", by_alias=True)
                for evidenceRecord in evidencePackage.records
            ],
        }
        fewShotPayload = {
            "examples": PRODUCT_FACT_RECONSTRUCTION_FEW_SHOT_EXAMPLES,
        }
        return LlmRequest(
            systemPrompt=PRODUCT_FACT_RECONSTRUCTION_SYSTEM_PROMPT,
            userPrompt="\n".join(
                [
                    "아래 evidence만 사용해 상품 입력 fact JSON을 작성하라.",
                    "출력 key는 product_facts, reconstructed_tables, unresolved_facts, evidence_traces, missing_fact_reasons, conflicts, warnings만 사용하라.",
                    "product_facts가 분류 입력의 기준값이다. 핵심 상품 fact는 반드시 product_facts에 먼저 넣어라.",
                    "evidence_traces에는 product_facts/unresolved_facts 각 항목의 source_refs, selected_span, decision_reason, status, unresolved_reason을 짧게 기록하라.",
                    "missing_fact_reasons에는 evidence로 확정할 수 없는 분류 중요 필드만 fact_name, reason, source_refs로 기록하라.",
                    "내부 사고 과정이나 단계별 chain-of-thought는 출력하지 말고 고정 trace 필드만 사용하라.",
                    "vlm_table/pp_table에 표 형태 정보가 있으면 reconstructed_tables에 compact row로 보존하라.",
                    "reconstructed_tables는 product_facts로 선택되지 않은 표 행도 보존하되, 긴 원재료 전문만 중복하지 마라.",
                    "product_summary는 제품 정체성/형태/설명 힌트로만 사용하고 원재료/함량은 OCR 또는 표 증거에서만 만들라.",
                    "raw OCR에서 원재료 섹션과 생산/활용/브랜드 섹션이 나뉘면 원재료 fact는 원재료 섹션 안의 텍스트만 사용하라.",
                    "JSON null을 절대 출력하지 말고, 모르는 값은 빈 문자열/빈 배열 또는 항목 생략으로 표현하라.",
                    "오탈자 교정, 단위 정규화, 표준 필드명/값은 normalized_value에만 넣어라.",
                    "교정 전 OCR 원문값을 별도 필드로 출력하지 마라.",
                    "정규화된 값을 뒷받침할 evidence가 부족하면 해당 항목은 unresolved_facts로 보내라.",
                    "normalized_fact_texts, dictionary_matches, used_llm_reconstruction, fallback_reason은 출력하지 마라.",
                    "source_refs에는 evidence_id만 사용하라.",
                    "다음 examples는 형식 참고용이며 evidence가 아니다.",
                    json.dumps(fewShotPayload, ensure_ascii=False, separators=(",", ":")),
                    "다음 JSON만 실제 evidence다.",
                    json.dumps(evidencePayload, ensure_ascii=False, separators=(",", ":")),
                ]
            ),
            responseFormat=LlmResponseFormat.JSON_SCHEMA,
            responseSchemaName="ProductFactReconstructionOutput",
            responseSchema=ProductFactReconstructionOutput.model_json_schema(
                by_alias=True
            ),
            responseModel=ProductFactReconstructionOutput,
            generationOptions=LlmGenerationOptions(
                temperature=0.0,
                maxTokens=self._maxTokens,
            ),
        )

    def _BuildRepairRequest(
        self,
        generatedText: str,
        originalError: Exception,
    ) -> LlmRequest:
        return LlmRequest(
            systemPrompt=PRODUCT_FACT_RECONSTRUCTION_SYSTEM_PROMPT,
            userPrompt="\n".join(
                [
                    "이전 응답은 JSON parse 또는 schema validation에 실패했다.",
                    "같은 의미의 결과를 valid JSON object로만 다시 출력하라.",
                    "설명, markdown, code fence를 출력하지 마라.",
                    "오류: {0}: {1}".format(
                        type(originalError).__name__,
                        originalError,
                    ),
                    "깨진 이전 응답:",
                    generatedText,
                ]
            ),
            responseFormat=LlmResponseFormat.JSON_SCHEMA,
            responseSchemaName="ProductFactReconstructionOutput",
            responseSchema=ProductFactReconstructionOutput.model_json_schema(
                by_alias=True
            ),
            responseModel=ProductFactReconstructionOutput,
            generationOptions=LlmGenerationOptions(
                temperature=0.0,
                maxTokens=self._maxTokens,
            ),
        )

    def _ParseJsonPayload(self, generatedText: str) -> JsonObject:
        strippedText = generatedText.strip()
        if strippedText == "":
            raise ValueError("empty LLM response")
        payload = json.loads(strippedText)
        if not isinstance(payload, dict):
            raise ValueError("LLM reconstruction response must be a JSON object.")
        requiredKeys = {
            "reconstructed_tables",
            "product_facts",
            "unresolved_facts",
            "evidence_traces",
            "missing_fact_reasons",
            "conflicts",
            "warnings",
        }
        missingKeys = sorted(requiredKeys - set(payload))
        if missingKeys:
            raise ValueError(
                "LLM reconstruction response missing required key(s): {0}".format(
                    ", ".join(missingKeys),
                )
            )
        return payload

    def _TryWriteArtifact(
        self,
        evidencePackage: InputEvidencePackage,
        fileName: str,
        payload: Mapping[str, object],
    ) -> None:
        if self._artifactRootPath is None:
            return
        productId = ExtractProductIdFromUrl(evidencePackage.productPageUrl)
        artifactDirectory = self._artifactRootPath / productId
        try:
            WriteJsonAtomically(artifactDirectory / fileName, payload)
        except OSError:
            return

class ProductInputReconstructionService:
    """Evidence build와 선택된 input reconstruction strategy를 묶는다."""

    def __init__(
        self,
        runtimeAdapter: Optional[RuntimeAdapter[object]] = None,
        fuzzyMinRatio: float = 0.86,
        llmMaxTokens: int = DEFAULT_LLM_INPUT_RECONSTRUCTION_MAX_TOKENS,
        llmArtifactRootPath: Optional[Path] = None,
    ) -> None:
        self._evidenceBuilder = ProductInputEvidenceBuilder()
        dictionaryEntries = ProductDictionaryRepository().LoadEntries()
        dictionaryRetriever = ProductDictionaryRetriever(
            dictionaryEntries,
            fuzzyMinRatio=fuzzyMinRatio,
        )
        self._validator = ProductFactReconstructionValidator()
        self._deterministicReconstructor = DeterministicProductFactReconstructor(
            dictionaryRetriever,
            validator=self._validator,
        )
        self._llmReconstructor = (
            ProductFactReconstructionAgent(
                runtimeAdapter=runtimeAdapter,
                validator=self._validator,
                maxTokens=llmMaxTokens,
                artifactRootPath=llmArtifactRootPath,
            )
            if runtimeAdapter is not None
            else None
        )

    def ReconstructFromPipelineParts(
        self,
        collectionResult: object,
        ocrImageResults: Sequence[object],
        combinedOcrText: str,
    ) -> InputReconstructionResult:
        evidencePackage = self._evidenceBuilder.BuildFromPipelineParts(
            collectionResult=collectionResult,
            ocrImageResults=ocrImageResults,
            combinedOcrText=combinedOcrText,
        )
        return self.ReconstructFromEvidencePackage(evidencePackage)

    def ReconstructFromEvidencePackage(
        self,
        evidencePackage: InputEvidencePackage,
    ) -> InputReconstructionResult:
        if self._llmReconstructor is None:
            reconstructionResult = self._deterministicReconstructor.Reconstruct(
                evidencePackage,
            )
        else:
            llmResult = self._llmReconstructor.Reconstruct(evidencePackage)
            reconstructionResult = self._SelectReconstructionResult(
                llmResult,
                evidencePackage,
            )
        sourceRefLabels = self._BuildSourceRefLabels(
            evidencePackage,
            reconstructionResult,
        )
        return reconstructionResult.model_copy(
            update={
                "sourceRefLabels": sourceRefLabels,
                "sourceEvidencePreview": self._BuildSourceEvidencePreview(
                    evidencePackage,
                    reconstructionResult,
                    sourceRefLabels,
                )
            }
        )

    def _SelectReconstructionResult(
        self,
        llmResult: InputReconstructionResult,
        evidencePackage: InputEvidencePackage,
    ) -> InputReconstructionResult:
        if llmResult.usedLlmReconstruction:
            return self._validator.Validate(llmResult, evidencePackage)

        baselineResult = self._deterministicReconstructor.Reconstruct(
            evidencePackage,
        )
        return baselineResult.model_copy(
            update={
                "warnings": list(
                    dict.fromkeys(
                        [*baselineResult.warnings, *llmResult.warnings]
                    )
                ),
                "fallbackReason": llmResult.fallbackReason,
            }
        )

    def _BuildSourceRefLabels(
        self,
        evidencePackage: InputEvidencePackage,
        reconstructionResult: InputReconstructionResult,
    ) -> Dict[str, str]:
        referencedEvidenceIds = self._CollectReferencedEvidenceIds(
            reconstructionResult,
        )
        labels: Dict[str, str] = {}
        for record in evidencePackage.records:
            if referencedEvidenceIds and record.evidenceId not in referencedEvidenceIds:
                continue
            labels[record.evidenceId] = self._BuildRecordSourceLabel(record)
        return labels

    def _BuildSourceEvidencePreview(
        self,
        evidencePackage: InputEvidencePackage,
        reconstructionResult: InputReconstructionResult,
        sourceRefLabels: Mapping[str, str],
    ) -> List[Dict[str, str]]:
        referencedEvidenceIds = self._CollectReferencedEvidenceIds(
            reconstructionResult,
        )
        previewEvidenceIds = self._BuildPreviewEvidenceIds(
            evidencePackage,
            referencedEvidenceIds,
        )
        previewRecords: List[Dict[str, str]] = []
        for record in evidencePackage.records:
            if previewEvidenceIds and record.evidenceId not in previewEvidenceIds:
                continue
            previewRecords.append(
                {
                    "evidence_id": record.evidenceId,
                    "source_type": record.sourceType,
                    "option_key": record.optionKey or "",
                    "source_label": sourceRefLabels.get(
                        record.evidenceId,
                        self._BuildRecordSourceLabel(record),
                    ),
                    "text": record.text[:800],
                }
            )
            if len(previewRecords) >= 16:
                break
        return previewRecords

    def _BuildPreviewEvidenceIds(
        self,
        evidencePackage: InputEvidencePackage,
        referencedEvidenceIds: set[str],
    ) -> set[str]:
        previewEvidenceIds = set(referencedEvidenceIds)
        referencedImageIndexes = {
            match.group(1)
            for record in evidencePackage.records
            if (
                record.evidenceId in referencedEvidenceIds
                and record.sourceRef is not None
                and (
                    match := re.match(
                        r"^image-(\d+)-(?:tile|table)-",
                        record.sourceRef,
                    )
                )
            )
        }
        if not referencedImageIndexes:
            return previewEvidenceIds
        previewEvidenceIds.update(
            record.evidenceId
            for record in evidencePackage.records
            if (
                _IsVlmTableSourceType(record.sourceType)
                and record.sourceRef is not None
                and any(
                    record.sourceRef.startswith("image-{0}-table-".format(imageIndex))
                    for imageIndex in referencedImageIndexes
                )
            )
        )
        return previewEvidenceIds

    def _CollectReferencedEvidenceIds(
        self,
        reconstructionResult: InputReconstructionResult,
    ) -> set[str]:
        referencedEvidenceIds = {
            sourceRef
            for fact in [
                *reconstructionResult.productFacts,
                *reconstructionResult.unresolvedFacts,
            ]
            for sourceRef in fact.sourceRefs
        }
        for table in reconstructionResult.reconstructedTables:
            referencedEvidenceIds.update(table.sourceRefs)
            for row in table.rows:
                referencedEvidenceIds.update(row.sourceRefs)
        for trace in reconstructionResult.evidenceTraces:
            referencedEvidenceIds.update(trace.sourceRefs)
        for reasonRecord in reconstructionResult.missingFactReasons:
            referencedEvidenceIds.update(reasonRecord.sourceRefs)
        return referencedEvidenceIds

    def _BuildRecordSourceLabel(self, record: InputEvidenceRecord) -> str:
        sourceRef = record.sourceRef or record.evidenceId
        sourceParts = sourceRef.split("-")
        if sourceRef.startswith("notice-option-") and "-field-" in sourceRef:
            return "상품고시 옵션 {0} 항목 {1}".format(sourceParts[2], sourceParts[4])
        if sourceRef.startswith("notice-option-"):
            return "상품고시 옵션 {0}".format(sourceParts[2])
        if sourceRef.startswith("notice-field-"):
            return "상품고시 항목 {0}".format(sourceParts[2])
        if record.sourceType == "product_summary":
            return "상품 요약 {0}".format(sourceRef)
        if (
            len(sourceParts) >= 4
            and sourceParts[0] == "image"
            and sourceParts[2] == "table"
        ):
            return "VLM 표 이미지 {0} 표 {1}".format(
                sourceParts[1],
                sourceParts[3],
            )
        if (
            len(sourceParts) >= 4
            and sourceParts[0] == "image"
            and sourceParts[2] == "tile"
        ):
            return "Raw OCR 이미지 {0} 타일 {1}".format(sourceParts[1], sourceParts[3])
        if sourceRef == "combined_ocr_text":
            return "통합 OCR 텍스트"
        return sourceRef
