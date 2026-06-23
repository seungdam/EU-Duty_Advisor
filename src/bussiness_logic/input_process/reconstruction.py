"""Product input reconstruction from notice, table OCR, and raw OCR evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from bussiness_logic.bridge import (
    LlmGenerationOptions,
    LlmRequest,
    LlmResponseFormat,
    RuntimeAdapter,
)
from bussiness_logic.input_process.dictionary import (
    DEFAULT_PRODUCT_INPUT_DICTIONARY_PATH,
    ProductDictionaryMatch,
    ProductDictionaryRepository,
    ProductDictionaryRetriever,
)
from bussiness_logic.product.ocr.ocr_normalization import (
    OCR_FACT_LABEL_MATCHERS,
    ProductOcrFactNormalizer,
)
from bussiness_logic.utils import NormalizeWhiteSpace, NormalizeWhitespaceLines


DEFAULT_LLM_INPUT_RECONSTRUCTION_MAX_TOKENS = 4096

PRODUCT_FACT_RECONSTRUCTION_SYSTEM_PROMPT = """
You reconstruct structured product input facts from Korean product notice, PP-Structure table OCR, and raw OCR text.
Return strict JSON only.
Return exactly one JSON object. Do not append markdown, commentary, or extra braces after the root object.
Return only these top-level keys: reconstructed_tables, product_facts, unresolved_facts, conflicts, warnings.
reconstructed_tables preserves PP-Structure table OCR contents for UI review. Do not summarize PP tables away.
reconstructed_tables must be an array of objects with exactly these keys: table_name, source_refs, rows.
Each reconstructed_tables row must have exactly these keys:
field_name, raw_value, normalized_value, unit, daily_value_percent, source_refs.
For nutrition tables, return each nutrient as its own row. For label/specification tables, return each label field as its own row.
product_facts is only the compact classification input facts derived from the same evidence.
product_facts and unresolved_facts must be arrays of objects with exactly these keys:
field_name, raw_value, normalized_value, source_refs, correction_type, validation_status.
conflicts and warnings must be arrays of strings.
Do not infer HS, CN, TARIC, customs, legal, or regulatory conclusions.
Do not create product facts that are absent from the provided evidence.
Correct OCR typos only when the surrounding evidence strongly supports the correction.
If evidence is insufficient or conflicting, use unresolved_facts or conflicts.
The application will generate normalized_fact_texts after validation.
Preserve table rows in reconstructed_tables even when they are not selected as product_facts.
Prefer concise product_facts for classification: product name, food/cosmetic type, physical form, processing state, preparation/use, storage state, ingredients, composition ratios, net content, and origin/manufacture country when explicit.
Do not include allergen warnings, same-facility/cross-contamination warnings, seller/vendor/manufacturer business-party names, expiry, package material, or marketing copy as product_facts unless they directly change customs classification.
Return atomic field_name/raw_value pairs. Do not put a whole OCR block under a generic field.
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

PRODUCT_FACT_RECONSTRUCTION_FEW_SHOT_EXAMPLES = [
    {
        "input": {
            "evidence": [
                {
                    "evidence_id": "evidence-1",
                    "source_type": "pp_table",
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
                            "raw_value": "나트류 320mg",
                            "normalized_value": "나트륨 320mg",
                            "unit": "mg",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                        {
                            "field_name": "탄수화물",
                            "raw_value": "탄수하물 40g",
                            "normalized_value": "탄수화물 40g",
                            "unit": "g",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                        {
                            "field_name": "단백질",
                            "raw_value": "단백질 8g",
                            "normalized_value": "단백질 8g",
                            "unit": "g",
                            "daily_value_percent": "",
                            "source_refs": ["evidence-1"],
                        },
                    ],
                }
            ],
            "product_facts": [
                {
                    "field_name": "영양성분",
                    "raw_value": "나트류 320mg 탄수하물 40g 단백질 8g",
                    "normalized_value": "나트륨 320mg 탄수화물 40g 단백질 8g",
                    "source_refs": ["evidence-1"],
                    "correction_type": "llm_reconstructed",
                    "validation_status": "accepted",
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
                    "source_type": "raw_ocr_tile",
                    "text": "제품명 오봉집낙지볶음 내용량 300g(274kcal) 식품의 유형 기타수산물가공품",
                },
                {
                    "evidence_id": "evidence-2",
                    "source_type": "raw_ocr_tile",
                    "text": "제품명 오봉집낙지볶음 내용량 500g(457kcal) 식품의 유형 기타수산물가공품",
                },
            ],
        },
        "output": {
            "reconstructed_tables": [],
            "product_facts": [
                {
                    "field_name": "제품명",
                    "raw_value": "오봉집낙지볶음",
                    "normalized_value": "오봉집낙지볶음",
                    "source_refs": ["evidence-1", "evidence-2"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                },
                {
                    "field_name": "식품의 유형",
                    "raw_value": "기타수산물가공품",
                    "normalized_value": "기타수산물가공품",
                    "source_refs": ["evidence-1", "evidence-2"],
                    "correction_type": "none",
                    "validation_status": "accepted",
                },
            ],
            "unresolved_facts": [
                {
                    "field_name": "포장단위별 내용물의 용량(중량), 수량",
                    "raw_value": "300g(274kcal) / 500g(457kcal)",
                    "normalized_value": "",
                    "source_refs": ["evidence-1", "evidence-2"],
                    "correction_type": "none",
                    "validation_status": "unresolved",
                }
            ],
            "conflicts": [
                "포장단위별 내용물의 용량(중량), 수량이 evidence-1에서는 300g, evidence-2에서는 500g으로 충돌한다."
            ],
            "warnings": [],
        },
    },
]


class ProductInputEvidenceRecord(BaseModel):
    """상품 input 복원을 위한 원천 evidence record."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    evidenceId: str = Field(alias="evidence_id")
    sourceType: str = Field(alias="source_type")
    text: str
    sourceRef: Optional[str] = Field(default=None, alias="source_ref")


class ProductInputEvidencePackage(BaseModel):
    """상품 input 복원 단계에 전달되는 evidence 묶음."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    productPageUrl: Optional[str] = Field(default=None, alias="product_page_url")
    records: List[ProductInputEvidenceRecord] = Field(default_factory=list)


class ProductFactRecord(BaseModel):
    """복원된 상품 input fact."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    fieldName: str = Field(alias="field_name")
    rawValue: str = Field(default="", alias="raw_value")
    normalizedValue: str = Field(default="", alias="normalized_value")
    sourceRefs: List[str] = Field(default_factory=list, alias="source_refs")
    correctionType: str = Field(default="none", alias="correction_type")
    validationStatus: str = Field(default="accepted", alias="validation_status")

    def ToFactText(self) -> str:
        normalizedFieldName = NormalizeWhiteSpace(self.fieldName)
        displayValue = NormalizeWhitespaceLines(
            self.normalizedValue or self.rawValue
        )
        if normalizedFieldName == "":
            return displayValue
        if displayValue == "":
            return normalizedFieldName
        if displayValue.startswith("{0}:".format(normalizedFieldName)):
            return displayValue
        return "{0}: {1}".format(normalizedFieldName, displayValue)


class ProductReconstructedTableRow(BaseModel):
    """PP table reconstruction row preserved for UI review."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    fieldName: str = Field(alias="field_name")
    rawValue: str = Field(default="", alias="raw_value")
    normalizedValue: str = Field(default="", alias="normalized_value")
    unit: str = ""
    dailyValuePercent: str = Field(default="", alias="daily_value_percent")
    sourceRefs: List[str] = Field(default_factory=list, alias="source_refs")


class ProductReconstructedTable(BaseModel):
    """PP table reconstruction preserved separately from classification facts."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    tableName: str = Field(default="", alias="table_name")
    sourceRefs: List[str] = Field(default_factory=list, alias="source_refs")
    rows: List[ProductReconstructedTableRow] = Field(default_factory=list)


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


class ProductFactReconstructionResult(BaseModel):
    """상품 input 복원 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    reconstructedTables: List[ProductReconstructedTable] = Field(
        default_factory=list,
        alias="reconstructed_tables",
    )
    productFacts: List[ProductFactRecord] = Field(
        default_factory=list,
        alias="product_facts",
    )
    unresolvedFacts: List[ProductFactRecord] = Field(
        default_factory=list,
        alias="unresolved_facts",
    )
    conflicts: List[str] = Field(default_factory=list)
    normalizedFactTexts: List[str] = Field(
        default_factory=list,
        alias="normalized_fact_texts",
    )
    warnings: List[str] = Field(default_factory=list)
    usedLlmReconstruction: bool = Field(
        default=False,
        alias="used_llm_reconstruction",
    )
    fallbackReason: Optional[str] = Field(default=None, alias="fallback_reason")
    dictionaryMatches: List[ProductDictionaryMatch] = Field(
        default_factory=list,
        alias="dictionary_matches",
    )
    debugArtifacts: Dict[str, str] = Field(
        default_factory=dict,
        alias="debug_artifacts",
    )
    sourceRefLabels: Dict[str, str] = Field(
        default_factory=dict,
        alias="source_ref_labels",
    )
    sourceEvidencePreview: List[Dict[str, str]] = Field(
        default_factory=list,
        alias="source_evidence_preview",
    )

    @field_validator("conflicts", "warnings", mode="before")
    @classmethod
    def NormalizeIssueTexts(cls, value: Any) -> List[str]:
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
        collectionResult: Any,
        ocrImageResults: Sequence[Any],
        combinedOcrText: str,
    ) -> ProductInputEvidencePackage:
        records: List[ProductInputEvidenceRecord] = []
        boundCollectionResult = _BoundCollectionResult.model_validate(collectionResult)
        boundOcrImageResults = [
            _BoundOcrImageResult.model_validate(ocrImageResult)
            for ocrImageResult in ocrImageResults
        ]
        self._AppendNoticeEvidence(records, boundCollectionResult.parsedProductPage)
        self._AppendOcrEvidence(records, boundOcrImageResults)
        if not any(record.sourceType.startswith("raw_ocr") for record in records):
            self._AppendRecord(
                records,
                sourceType="raw_ocr",
                text=combinedOcrText,
                sourceRef="combined_ocr_text",
            )
        return ProductInputEvidencePackage(
            productPageUrl=boundCollectionResult.productPageUrl,
            records=records,
        )

    def _AppendNoticeEvidence(
        self,
        records: List[ProductInputEvidenceRecord],
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
                )
            for fieldIndex, field in enumerate(noticeOption.fields, start=1):
                self._AppendNoticeFieldRecord(
                    records,
                    field,
                    "notice-option-{0}-field-{1}".format(optionIndex, fieldIndex),
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
        records: List[ProductInputEvidenceRecord],
        field: _BoundNoticeField,
        sourceRef: str,
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
        )

    def _AppendOcrEvidence(
        self,
        records: List[ProductInputEvidenceRecord],
        ocrImageResults: Sequence[_BoundOcrImageResult],
    ) -> None:
        for imageIndex, imageResult in enumerate(ocrImageResults, start=1):
            for tableIndex, table in enumerate(imageResult.structuredOcr.tables, start=1):
                self._AppendRecord(
                    records,
                    sourceType="pp_table",
                    text=table.plainText,
                    sourceRef="image-{0}-table-{1}".format(imageIndex, tableIndex),
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
                )

    def _AppendRecord(
        self,
        records: List[ProductInputEvidenceRecord],
        sourceType: str,
        text: str,
        sourceRef: Optional[str],
    ) -> None:
        normalizedText = _StripOcrCollectionMarkers(text)
        if normalizedText == "":
            return
        records.append(
            ProductInputEvidenceRecord(
                evidenceId="evidence-{0}".format(len(records) + 1),
                sourceType=sourceType,
                text=normalizedText,
                sourceRef=sourceRef,
            )
        )


class ProductFactReconstructionValidator:
    """LLM 또는 deterministic 복원 결과의 source reference를 검증한다."""

    def Validate(
        self,
        result: ProductFactReconstructionResult,
        evidencePackage: ProductInputEvidencePackage,
    ) -> ProductFactReconstructionResult:
        validEvidenceIds = {record.evidenceId for record in evidencePackage.records}
        productFacts: List[ProductFactRecord] = []
        unresolvedFacts: List[ProductFactRecord] = []
        warnings = list(result.warnings)
        reconstructedTables = self._CleanReconstructedTables(
            result.reconstructedTables,
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
            productFacts.append(cleanedFactRecord)
        for factRecord in result.unresolvedFacts:
            cleanedFactRecord = self._CleanFactRecord(
                factRecord,
                warnings=warnings,
            )
            if cleanedFactRecord is not None:
                unresolvedFacts.append(cleanedFactRecord)
        normalizedFactTexts = self._BuildNormalizedFactTexts(productFacts)
        return result.model_copy(
            update={
                "reconstructedTables": reconstructedTables,
                "productFacts": productFacts,
                "unresolvedFacts": unresolvedFacts,
                "normalizedFactTexts": normalizedFactTexts,
                "warnings": warnings,
            }
        )

    def _CleanReconstructedTables(
        self,
        tables: Sequence[ProductReconstructedTable],
        *,
        validEvidenceIds: set[str],
        warnings: List[str],
    ) -> List[ProductReconstructedTable]:
        cleanedTables: List[ProductReconstructedTable] = []
        for table in tables:
            tableName = NormalizeWhiteSpace(table.tableName) or "Reconstructed table"
            tableSourceRefs = self._CleanSourceRefs(
                table.sourceRefs,
                validEvidenceIds=validEvidenceIds,
                warnings=warnings,
                context="table={0}".format(tableName),
            )
            cleanedRows: List[ProductReconstructedTableRow] = []
            for row in table.rows:
                fieldName = NormalizeWhiteSpace(
                    _StripOcrCollectionMarkers(row.fieldName)
                )
                rawValue = NormalizeWhitespaceLines(
                    _StripOcrCollectionMarkers(row.rawValue)
                )
                normalizedValue = NormalizeWhitespaceLines(
                    _StripOcrCollectionMarkers(row.normalizedValue)
                )
                if fieldName == "" or (rawValue == "" and normalizedValue == ""):
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
                cleanedRows.append(
                    row.model_copy(
                        update={
                            "fieldName": fieldName,
                            "rawValue": rawValue,
                            "normalizedValue": normalizedValue,
                            "unit": NormalizeWhiteSpace(row.unit),
                            "dailyValuePercent": NormalizeWhiteSpace(
                                row.dailyValuePercent,
                            ),
                            "sourceRefs": rowSourceRefs or tableSourceRefs,
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
        factRecord: ProductFactRecord,
        *,
        warnings: List[str],
    ) -> Optional[ProductFactRecord]:
        fieldName = _StripOcrCollectionMarkers(factRecord.fieldName)
        rawValue = _StripOcrCollectionMarkers(factRecord.rawValue)
        normalizedValue = _StripOcrCollectionMarkers(factRecord.normalizedValue)
        displayValue = normalizedValue or rawValue
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
            rawValue = splitValue
            normalizedValue = splitValue
        if fieldName == "" or (rawValue == "" and normalizedValue == ""):
            warnings.append(
                "rejected_fact_empty_field_or_value field={0}".format(
                    factRecord.fieldName,
                )
            )
            return None
        return factRecord.model_copy(
            update={
                "fieldName": NormalizeWhiteSpace(fieldName),
                "rawValue": NormalizeWhitespaceLines(rawValue),
                "normalizedValue": NormalizeWhitespaceLines(
                    normalizedValue,
                ),
            }
        )

    def _BuildNormalizedFactTexts(
        self,
        productFacts: Sequence[ProductFactRecord],
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
        evidencePackage: ProductInputEvidencePackage,
    ) -> ProductFactReconstructionResult:
        dictionaryMatches = self._dictionaryRetriever.FindMatches(
            [record.text for record in evidencePackage.records]
        )
        factRecords = self._BuildFactRecords(evidencePackage, dictionaryMatches)
        result = ProductFactReconstructionResult(
            productFacts=factRecords,
            usedLlmReconstruction=False,
            fallbackReason="llm_reconstruction_not_used",
            dictionaryMatches=dictionaryMatches,
        )
        return self._validator.Validate(result, evidencePackage)

    def _BuildFactRecords(
        self,
        evidencePackage: ProductInputEvidencePackage,
        dictionaryMatches: Sequence[ProductDictionaryMatch],
    ) -> List[ProductFactRecord]:
        factRecords: List[ProductFactRecord] = []
        for record in evidencePackage.records:
            if record.sourceType == "notice_field":
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
        record: ProductInputEvidenceRecord,
        dictionaryMatches: Sequence[ProductDictionaryMatch],
    ) -> Optional[ProductFactRecord]:
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
        return ProductFactRecord(
            fieldName=fieldName,
            rawValue=fieldValue,
            normalizedValue=normalizedValue,
            sourceRefs=[record.evidenceId],
            correctionType=correctionType,
            validationStatus="accepted",
        )

    def _BuildFactRecordFromNormalizedText(
        self,
        factText: str,
        evidencePackage: ProductInputEvidencePackage,
        dictionaryMatches: Sequence[ProductDictionaryMatch],
    ) -> Optional[ProductFactRecord]:
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
        return ProductFactRecord(
            fieldName=fieldName,
            rawValue=fieldValue,
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
        evidencePackage: ProductInputEvidencePackage,
    ) -> List[str]:
        normalizedFactText = NormalizeWhiteSpace(factText)
        sourceRefs: List[str] = []
        for record in evidencePackage.records:
            if normalizedFactText in NormalizeWhiteSpace(record.text):
                sourceRefs.append(record.evidenceId)
        return sourceRefs

    def _DeduplicateFactRecords(
        self,
        factRecords: Sequence[ProductFactRecord],
    ) -> List[ProductFactRecord]:
        deduplicatedRecords: List[ProductFactRecord] = []
        seenFactTexts: set[str] = set()
        for factRecord in factRecords:
            factText = factRecord.ToFactText()
            if factText in seenFactTexts:
                continue
            seenFactTexts.add(factText)
            deduplicatedRecords.append(factRecord)
        return deduplicatedRecords


class LlmProductFactReconstructor:
    """Few-shot LLM을 이용해 table/raw OCR evidence를 상품 input fact로 복원한다."""

    def __init__(
        self,
        runtimeAdapter: Optional[RuntimeAdapter[Any]],
        validator: Optional[ProductFactReconstructionValidator] = None,
        maxTokens: int = DEFAULT_LLM_INPUT_RECONSTRUCTION_MAX_TOKENS,
        debugArtifactRootPath: Optional[Path] = None,
    ) -> None:
        self._runtimeAdapter = runtimeAdapter
        self._validator = validator or ProductFactReconstructionValidator()
        self._maxTokens = max(1, maxTokens)
        self._debugStore = (
            ProductInputReconstructionDebugStore(debugArtifactRootPath)
            if debugArtifactRootPath is not None
            else None
        )

    def Reconstruct(
        self,
        evidencePackage: ProductInputEvidencePackage,
    ) -> ProductFactReconstructionResult:
        if self._runtimeAdapter is None:
            return ProductFactReconstructionResult(
                warnings=["llm_reconstruction_failed: runtime adapter is not configured"],
                fallbackReason="llm_runtime_not_configured",
            )

        request = self._BuildRequest(evidencePackage)
        debugArtifacts = self._TryWriteDebugArtifact(
            lambda: self._debugStore.WriteRequest(evidencePackage, request)
            if self._debugStore is not None
            else None,
            "request",
        )
        try:
            response = self._runtimeAdapter.Generate(request)
            debugArtifacts.update(
                self._TryWriteDebugArtifact(
                    lambda: self._debugStore.WriteResponse(evidencePackage, response)
                    if self._debugStore is not None
                    else None,
                    "response",
                )
            )
            payload = self._ParseJsonPayload(response.generatedText)
            result = ProductFactReconstructionResult.model_validate(payload)
            result = result.model_copy(
                update={
                    "normalizedFactTexts": [],
                    "usedLlmReconstruction": True,
                    "fallbackReason": None,
                    "dictionaryMatches": [],
                    "debugArtifacts": debugArtifacts,
                }
            )
            return self._validator.Validate(result, evidencePackage)
        except (ValueError, ValidationError, RuntimeError) as error:
            debugArtifacts.update(
                self._TryWriteDebugArtifact(
                    lambda: self._debugStore.WriteError(evidencePackage, error)
                    if self._debugStore is not None
                    else None,
                    "error",
                )
            )
            return ProductFactReconstructionResult(
                warnings=["llm_reconstruction_failed: {0}".format(error)],
                fallbackReason="llm_reconstruction_failed",
                debugArtifacts=debugArtifacts,
            )

    def _BuildRequest(
        self,
        evidencePackage: ProductInputEvidencePackage,
    ) -> LlmRequest:
        contextPayload = {
            "evidence": [
                evidenceRecord.model_dump(mode="json", by_alias=True)
                for evidenceRecord in evidencePackage.records
            ],
            "few_shot_examples": PRODUCT_FACT_RECONSTRUCTION_FEW_SHOT_EXAMPLES,
        }
        return LlmRequest(
            systemPrompt=PRODUCT_FACT_RECONSTRUCTION_SYSTEM_PROMPT,
            userPrompt="\n".join(
                [
                    "아래 evidence만 사용해 상품 입력 fact JSON을 작성하라.",
                    "출력 key는 reconstructed_tables, product_facts, unresolved_facts, conflicts, warnings만 사용하라.",
                    "reconstructed_tables에는 PP table/raw OCR에서 복원 가능한 표 행을 가능한 한 보존하라.",
                    "product_facts에는 분류 후보 생성에 필요한 핵심 상품 fact만 넣어라.",
                    "normalized_fact_texts, dictionary_matches, used_llm_reconstruction, fallback_reason은 출력하지 마라.",
                    "source_refs에는 evidence_id만 사용하라.",
                    json.dumps(contextPayload, ensure_ascii=False, separators=(",", ":")),
                ]
            ),
            responseFormat=LlmResponseFormat.JSON_OBJECT,
            generationOptions=LlmGenerationOptions(
                temperature=0.0,
                maxTokens=self._maxTokens,
            ),
        )

    def _ParseJsonPayload(self, generatedText: str) -> Dict[str, Any]:
        strippedText = generatedText.strip()
        if strippedText == "":
            raise ValueError("empty LLM response")
        try:
            payload = json.loads(strippedText)
        except json.JSONDecodeError:
            startIndex = strippedText.find("{")
            endIndex = strippedText.rfind("}")
            if startIndex < 0 or endIndex <= startIndex:
                raise
            payload = json.loads(strippedText[startIndex : endIndex + 1])
        if not isinstance(payload, dict):
            raise ValueError("LLM reconstruction response must be a JSON object.")
        return payload

    def _TryWriteDebugArtifact(
        self,
        writeCallable: Any,
        artifactName: str,
    ) -> Dict[str, str]:
        if self._debugStore is None:
            return {}
        try:
            artifactPath = writeCallable()
        except OSError:
            return {}
        if artifactPath is None:
            return {}
        return {artifactName: str(artifactPath)}


class ProductInputReconstructionDebugStore:
    """LLM input reconstruction 요청/응답 artifact를 상품별 디렉터리에 저장한다."""

    def __init__(self, artifactRootPath: Path) -> None:
        self._artifactRootPath = artifactRootPath

    def WriteRequest(
        self,
        evidencePackage: ProductInputEvidencePackage,
        request: LlmRequest,
    ) -> Path:
        return self._WriteJson(
            evidencePackage,
            "llm-input-reconstruction-request.json",
            {
                "product_page_url": evidencePackage.productPageUrl,
                "evidence_record_count": len(evidencePackage.records),
                "request": request.model_dump(mode="json", by_alias=True),
            },
        )

    def WriteResponse(
        self,
        evidencePackage: ProductInputEvidencePackage,
        response: Any,
    ) -> Path:
        return self._WriteJson(
            evidencePackage,
            "llm-input-reconstruction-response.json",
            response.model_dump(mode="json", by_alias=True),
        )

    def WriteError(
        self,
        evidencePackage: ProductInputEvidencePackage,
        error: Exception,
    ) -> Path:
        return self._WriteJson(
            evidencePackage,
            "llm-input-reconstruction-error.json",
            {
                "product_page_url": evidencePackage.productPageUrl,
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )

    def _WriteJson(
        self,
        evidencePackage: ProductInputEvidencePackage,
        fileName: str,
        payload: Mapping[str, Any],
    ) -> Path:
        artifactDirectory = self._BuildArtifactDirectory(evidencePackage)
        artifactDirectory.mkdir(parents=True, exist_ok=True)
        artifactPath = artifactDirectory / fileName
        artifactPath.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return artifactPath

    def _BuildArtifactDirectory(
        self,
        evidencePackage: ProductInputEvidencePackage,
    ) -> Path:
        return self._artifactRootPath / self._ExtractProductId(
            evidencePackage.productPageUrl,
        )

    def _ExtractProductId(self, productPageUrl: Optional[str]) -> str:
        if productPageUrl is None:
            return "unknown"
        parsedUrl = urlparse(productPageUrl)
        pathParts = [pathPart for pathPart in parsedUrl.path.split("/") if pathPart]
        if len(pathParts) >= 2 and pathParts[0] == "goods":
            return self._BuildSafePathName(pathParts[1])
        if len(pathParts) >= 2 and pathParts[0] == "products":
            return "global-{0}".format(self._BuildSafePathName(pathParts[1]))
        if (
            len(pathParts) >= 3
            and pathParts[0] == "en"
            and pathParts[1] == "products"
        ):
            return "global-{0}".format(self._BuildSafePathName(pathParts[2]))
        return self._BuildSafePathName(parsedUrl.path.strip("/") or "unknown")

    @staticmethod
    def _BuildSafePathName(value: str) -> str:
        safeName = "".join(
            character
            if character.isalnum() or character in {"-", "_"}
            else "-"
            for character in value
        ).strip("-")
        return safeName or "unknown"


class ProductInputReconstructionService:
    """Evidence build와 선택된 input reconstruction strategy를 묶는다."""

    def __init__(
        self,
        dictionaryPath: Optional[str] = None,
        runtimeAdapter: Optional[RuntimeAdapter[Any]] = None,
        fuzzyMinRatio: float = 0.86,
        llmMaxTokens: int = DEFAULT_LLM_INPUT_RECONSTRUCTION_MAX_TOKENS,
        llmDebugArtifactRootPath: Optional[Path] = None,
    ) -> None:
        self._evidenceBuilder = ProductInputEvidenceBuilder()
        if runtimeAdapter is not None:
            self._reconstructor = LlmProductFactReconstructor(
                runtimeAdapter=runtimeAdapter,
                maxTokens=llmMaxTokens,
                debugArtifactRootPath=llmDebugArtifactRootPath,
            )
            return

        resolvedDictionaryPath = (
            DEFAULT_PRODUCT_INPUT_DICTIONARY_PATH
            if dictionaryPath is None
            else Path(dictionaryPath)
        )
        dictionaryEntries = ProductDictionaryRepository(
            resolvedDictionaryPath,
        ).LoadEntries()
        dictionaryRetriever = ProductDictionaryRetriever(
            dictionaryEntries,
            fuzzyMinRatio=fuzzyMinRatio,
        )
        self._reconstructor = DeterministicProductFactReconstructor(
            dictionaryRetriever,
        )

    def ReconstructFromPipelineParts(
        self,
        collectionResult: Any,
        ocrImageResults: Sequence[Any],
        combinedOcrText: str,
    ) -> ProductFactReconstructionResult:
        evidencePackage = self._evidenceBuilder.BuildFromPipelineParts(
            collectionResult=collectionResult,
            ocrImageResults=ocrImageResults,
            combinedOcrText=combinedOcrText,
        )
        reconstructionResult = self._reconstructor.Reconstruct(evidencePackage)
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

    def _BuildSourceRefLabels(
        self,
        evidencePackage: ProductInputEvidencePackage,
        reconstructionResult: ProductFactReconstructionResult,
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
        evidencePackage: ProductInputEvidencePackage,
        reconstructionResult: ProductFactReconstructionResult,
        sourceRefLabels: Mapping[str, str],
    ) -> List[Dict[str, str]]:
        referencedEvidenceIds = self._CollectReferencedEvidenceIds(
            reconstructionResult,
        )
        previewRecords: List[Dict[str, str]] = []
        for record in evidencePackage.records:
            if referencedEvidenceIds and record.evidenceId not in referencedEvidenceIds:
                continue
            previewRecords.append(
                {
                    "evidence_id": record.evidenceId,
                    "source_type": record.sourceType,
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

    def _CollectReferencedEvidenceIds(
        self,
        reconstructionResult: ProductFactReconstructionResult,
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
        return referencedEvidenceIds

    def _BuildRecordSourceLabel(self, record: ProductInputEvidenceRecord) -> str:
        sourceRef = record.sourceRef or record.evidenceId
        sourceParts = sourceRef.split("-")
        if sourceRef.startswith("notice-option-") and "-field-" in sourceRef:
            return "상품고시 옵션 {0} 항목 {1}".format(sourceParts[2], sourceParts[4])
        if sourceRef.startswith("notice-option-"):
            return "상품고시 옵션 {0}".format(sourceParts[2])
        if sourceRef.startswith("notice-field-"):
            return "상품고시 항목 {0}".format(sourceParts[2])
        if (
            len(sourceParts) >= 4
            and sourceParts[0] == "image"
            and sourceParts[2] == "table"
        ):
            return "PP table 이미지 {0} 표 {1}".format(sourceParts[1], sourceParts[3])
        if (
            len(sourceParts) >= 4
            and sourceParts[0] == "image"
            and sourceParts[2] == "tile"
        ):
            return "Raw OCR 이미지 {0} 타일 {1}".format(sourceParts[1], sourceParts[3])
        if sourceRef == "combined_ocr_text":
            return "통합 OCR 텍스트"
        return sourceRef
