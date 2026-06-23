"""Product input adapters for core classification."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Dict, List, Mapping, Optional, Sequence, Set, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)

from bussiness_logic.core.classification import (
    PRODUCT_DOMAIN_SCOPE_MAP,
    ProductClassificationInput,
)
from bussiness_logic.product.ocr.ocr_normalization import (
    PRODUCT_REFERENCE_PLACEHOLDER_PATTERN,
    ProductOcrFactNormalizer,
)
from bussiness_logic.utils import NormalizeWhiteSpace, NormalizeWhitespaceLines


def _NormalizeOptionalText(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalizedValue = NormalizeWhitespaceLines(value)
    return normalizedValue or None


def _NormalizeTextList(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    normalizedValues: List[str] = []
    for item in value:
        normalizedItem = _NormalizeOptionalText(item)
        if normalizedItem is not None:
            normalizedValues.append(normalizedItem)
    return normalizedValues


def _NormalizeMapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", by_alias=True)
    return {}


def _NormalizeMappingList(value: Any) -> List[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    normalizedItems: List[Mapping[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            normalizedItems.append(item)
            continue
        if isinstance(item, BaseModel):
            normalizedItems.append(item.model_dump(mode="python", by_alias=True))
    return normalizedItems


OptionalText: TypeAlias = Annotated[
    Optional[str],
    BeforeValidator(_NormalizeOptionalText),
]
TextList: TypeAlias = Annotated[
    List[str],
    BeforeValidator(_NormalizeTextList),
]


class ProductInputDataShape(str, Enum):
    TopLevelParsedPage = "top_level_parsed_page"
    CollectionResult = "collection_result"
    SmokeSummary = "smoke_summary"
    FlatSmokeArtifact = "flat_smoke_artifact"


class _AdapterDataModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class NoticeFieldData(_AdapterDataModel):
    fieldName: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("field_name", "fieldName"),
    )
    fieldValue: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("field_value", "fieldValue"),
    )
    rawText: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("raw_text", "rawText"),
    )


class NoticeOptionData(_AdapterDataModel):
    optionName: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("option_name", "optionName"),
    )
    rawText: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("raw_text", "rawText"),
    )
    fields: List[NoticeFieldData] = Field(default_factory=list)

    @field_validator("fields", mode="before")
    @classmethod
    def _NormalizeFields(cls, value: Any) -> List[Mapping[str, Any]]:
        return _NormalizeMappingList(value)


class OcrNormalizationData(_AdapterDataModel):
    factTexts: TextList = Field(
        default_factory=list,
        validation_alias=AliasChoices("fact_texts", "factTexts"),
    )
    excludedTextPreview: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("excluded_text_preview", "excludedTextPreview"),
    )


class InputReconstructionData(_AdapterDataModel):
    normalizedFactTexts: TextList = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "classification_input_fact_texts",
            "classification_fact_texts",
            "normalized_fact_texts",
            "normalizedFactTexts",
        ),
    )
    structuredProductFacts: List[Mapping[str, Any]] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "classification_input_product_facts",
            "structured_product_facts",
            "product_facts",
            "structuredProductFacts",
        ),
    )
    unresolvedProductFacts: List[Mapping[str, Any]] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "unresolved_product_facts",
            "unresolved_facts",
            "unresolvedProductFacts",
        ),
    )
    productFactConflicts: List[Any] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "product_fact_conflicts",
            "conflicts",
            "productFactConflicts",
        ),
    )

    @field_validator("structuredProductFacts", "unresolvedProductFacts", mode="before")
    @classmethod
    def _NormalizeFactRecords(cls, value: Any) -> List[Mapping[str, Any]]:
        return _NormalizeMappingList(value)

    @field_validator("productFactConflicts", mode="before")
    @classmethod
    def _NormalizeConflicts(cls, value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]


class ParsedProductPageData(_AdapterDataModel):
    productName: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("product_name", "productName"),
    )
    productDomain: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("product_domain", "productDomain"),
    )
    shortDescription: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("short_description", "shortDescription"),
    )
    brandName: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("brand_name", "brandName"),
    )
    packageType: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("package_type", "packageType"),
    )
    saleUnit: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("sale_unit", "saleUnit"),
    )
    rawProductNoticeText: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices(
            "raw_product_notice_text",
            "rawProductNoticeText",
        ),
    )
    productNoticeOptionNames: TextList = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "product_notice_option_names",
            "productNoticeOptionNames",
        ),
    )
    productNoticeFields: List[NoticeFieldData] = Field(
        default_factory=list,
        validation_alias=AliasChoices("product_notice_fields", "productNoticeFields"),
    )
    productNoticeOptions: List[NoticeOptionData] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "product_notice_options",
            "productNoticeOptions",
        ),
    )

    @field_validator("productNoticeFields", mode="before")
    @classmethod
    def _NormalizeNoticeFields(cls, value: Any) -> List[Mapping[str, Any]]:
        return _NormalizeMappingList(value)

    @field_validator("productNoticeOptions", mode="before")
    @classmethod
    def _NormalizeNoticeOptions(cls, value: Any) -> List[Mapping[str, Any]]:
        return _NormalizeMappingList(value)


class TopLevelParsedPageData(_AdapterDataModel):
    productPageUrl: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("product_page_url", "productPageUrl"),
    )
    parsedProductPage: ParsedProductPageData = Field(
        default_factory=ParsedProductPageData,
        validation_alias=AliasChoices(
            "parsed_product_page",
            "source_product_page",
            "parsedProductPage",
        ),
    )
    combinedOcrText: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("combined_ocr_text", "combinedOcrText"),
    )
    ocrNormalization: OcrNormalizationData = Field(
        default_factory=OcrNormalizationData,
        validation_alias=AliasChoices("ocr_normalization", "ocrNormalizationResult"),
    )
    inputReconstruction: InputReconstructionData = Field(
        default_factory=InputReconstructionData,
        validation_alias=AliasChoices(
            "input_reconstruction",
            "inputReconstructionResult",
        ),
    )

    @field_validator(
        "parsedProductPage",
        "ocrNormalization",
        "inputReconstruction",
        mode="before",
    )
    @classmethod
    def _NormalizeNestedPayload(cls, value: Any) -> Mapping[str, Any]:
        return _NormalizeMapping(value)


class CollectionResultPayload(_AdapterDataModel):
    productPageUrl: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("product_page_url", "productPageUrl"),
    )
    parsedProductPage: ParsedProductPageData = Field(
        default_factory=ParsedProductPageData,
        validation_alias=AliasChoices("parsed_product_page", "parsedProductPage"),
    )

    @field_validator("parsedProductPage", mode="before")
    @classmethod
    def _NormalizeParsedProductPage(cls, value: Any) -> Mapping[str, Any]:
        return _NormalizeMapping(value)


class CollectionResultData(_AdapterDataModel):
    collectionResult: CollectionResultPayload = Field(
        default_factory=CollectionResultPayload,
        validation_alias=AliasChoices("collection_result", "collectionResult"),
    )
    combinedOcrText: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("combined_ocr_text", "combinedOcrText"),
    )
    ocrNormalization: OcrNormalizationData = Field(
        default_factory=OcrNormalizationData,
        validation_alias=AliasChoices("ocr_normalization", "ocrNormalizationResult"),
    )
    inputReconstruction: InputReconstructionData = Field(
        default_factory=InputReconstructionData,
        validation_alias=AliasChoices(
            "input_reconstruction",
            "inputReconstructionResult",
        ),
    )

    @field_validator(
        "collectionResult",
        "ocrNormalization",
        "inputReconstruction",
        mode="before",
    )
    @classmethod
    def _NormalizeNestedPayload(cls, value: Any) -> Mapping[str, Any]:
        return _NormalizeMapping(value)

class OcrImageResultData(_AdapterDataModel):
    ocrText: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("ocr_text", "ocrText"),
    )

class FlatSmokeArtifactData(_AdapterDataModel):
    productPageUrl: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("product_page_url", "productPageUrl"),
    )
    combinedOcrText: OptionalText = Field(
        default=None,
        validation_alias=AliasChoices("combined_ocr_text", "combinedOcrText"),
    )
    ocrImageResults: List[OcrImageResultData] = Field(
        default_factory=list,
        validation_alias=AliasChoices("ocr_image_results", "ocrImageResults"),
    )

    @field_validator("ocrImageResults", mode="before")
    @classmethod
    def _NormalizeOcrImageResults(cls, value: Any) -> List[Mapping[str, Any]]:
        return _NormalizeMappingList(value)

class ProductInputAdapter:
    """수집 산출물을 Stage 1 후보 조회용 입력 스키마로 변환한다."""

    def __init__(
        self,
        ocrFactNormalizer: Optional[ProductOcrFactNormalizer] = None,
    ) -> None:
        self.ocrFactNormalizer = ocrFactNormalizer or ProductOcrFactNormalizer()

    def BuildFromData(
        self,
        productData: Mapping[str, Any],
    ) -> ProductClassificationInput:
        dataShape = self._DetectDataShape(productData)
        if dataShape == ProductInputDataShape.TopLevelParsedPage:
            return self._BuildFromTopLevelParsedPageData(productData)
        if dataShape == ProductInputDataShape.CollectionResult:
            return self._BuildFromCollectionResultData(productData)
        if dataShape == ProductInputDataShape.SmokeSummary:
            return self._BuildFromSmokeSummaryData(productData)
        if dataShape == ProductInputDataShape.FlatSmokeArtifact:
            return self._BuildFromFlatSmokeArtifactData(productData)
        raise ValueError("Unsupported product input data shape.")

    def BuildFromObject(
        self,
        productObject: Any,
    ) -> ProductClassificationInput:
        if isinstance(productObject, Mapping):
            return self.BuildFromData(productObject)

        modelDump = getattr(productObject, "model_dump", None)
        if callable(modelDump):
            return self.BuildFromData(modelDump(mode="json", by_alias=True))

        toDict = getattr(productObject, "ToDict", None)
        if callable(toDict):
            return self.BuildFromData(toDict())

        raise TypeError(
            "productObject must be a mapping or provide model_dump()/ToDict().",
        )

    def _DetectDataShape(
        self,
        productData: Mapping[str, Any],
    ) -> ProductInputDataShape:
        if self._HasAnyKey(
            productData,
            "parsed_product_page",
            "source_product_page",
            "parsedProductPage",
        ):
            return ProductInputDataShape.TopLevelParsedPage
        if self._HasAnyKey(productData, "collection_result", "collectionResult"):
            return ProductInputDataShape.CollectionResult
        if "product" in productData and "notice" in productData:
            return ProductInputDataShape.SmokeSummary
        if self._LooksLikeFlatSmokeArtifact(productData):
            return ProductInputDataShape.FlatSmokeArtifact
        raise ValueError("Unsupported product input data shape.")

    def _BuildFromTopLevelParsedPageData(
        self,
        productData: Mapping[str, Any],
    ) -> ProductClassificationInput:
        sourceData = TopLevelParsedPageData.model_validate(productData)
        return self._BuildInput(
            productPageUrl=sourceData.productPageUrl,
            parsedProductPage=sourceData.parsedProductPage,
            combinedOcrText=sourceData.combinedOcrText or "",
            ocrNormalizationData=sourceData.ocrNormalization,
            inputReconstructionData=sourceData.inputReconstruction,
        )

    def _BuildFromCollectionResultData(
        self,
        productData: Mapping[str, Any],
    ) -> ProductClassificationInput:
        sourceData = CollectionResultData.model_validate(productData)
        return self._BuildInput(
            productPageUrl=sourceData.collectionResult.productPageUrl,
            parsedProductPage=sourceData.collectionResult.parsedProductPage,
            combinedOcrText=sourceData.combinedOcrText or "",
            ocrNormalizationData=sourceData.ocrNormalization,
            inputReconstructionData=sourceData.inputReconstruction,
        )


    def _BuildFromFlatSmokeArtifactData(
        self,
        productData: Mapping[str, Any],
    ) -> ProductClassificationInput:
        sourceData = FlatSmokeArtifactData.model_validate(productData)
        return self._BuildInput(
            productPageUrl=sourceData.productPageUrl,
            parsedProductPage=ParsedProductPageData.model_validate(productData),
            combinedOcrText=self._BuildCombinedOcrTextFromFlatData(sourceData),
        )

    def _BuildInput(
        self,
        productPageUrl: Optional[str],
        parsedProductPage: ParsedProductPageData,
        combinedOcrText: str,
        ocrNormalizationData: Optional[OcrNormalizationData] = None,
        inputReconstructionData: Optional[InputReconstructionData] = None,
    ) -> ProductClassificationInput:
        productDomain = parsedProductPage.productDomain or "unknown"
        noticeOptions = parsedProductPage.productNoticeOptions
        fallbackNoticeFields = (
            []
            if noticeOptions
            else parsedProductPage.productNoticeFields
        )
        rawProductNoticeText = parsedProductPage.rawProductNoticeText
        productNoticeText = (
            rawProductNoticeText
            if rawProductNoticeText is not None
            and not self._ContainsPlaceholderReference(rawProductNoticeText)
            else self._BuildRawNoticeText(fallbackNoticeFields, noticeOptions)
        )
        noticeOptionNames = (
            parsedProductPage.productNoticeOptionNames
            or self._ExtractNoticeOptionNames(noticeOptions)
        )
        normalizedOcrFactTexts: List[str] = []
        excludedOcrTextPreview = ""
        structuredProductFacts: List[Dict[str, Any]] = []
        unresolvedProductFacts: List[Dict[str, Any]] = []
        productFactConflicts: List[Any] = []
        if inputReconstructionData is not None:
            normalizedOcrFactTexts = inputReconstructionData.normalizedFactTexts
            structuredProductFacts = [
                dict(item)
                for item in inputReconstructionData.structuredProductFacts
            ]
            unresolvedProductFacts = [
                dict(item)
                for item in inputReconstructionData.unresolvedProductFacts
            ]
            productFactConflicts = list(inputReconstructionData.productFactConflicts)
        elif ocrNormalizationData is not None:
            normalizedOcrFactTexts = ocrNormalizationData.factTexts
            excludedOcrTextPreview = ocrNormalizationData.excludedTextPreview or ""
        if (
            inputReconstructionData is None
            and not normalizedOcrFactTexts
            and combinedOcrText.strip() != ""
        ):
            ocrNormalizationResult = self.ocrFactNormalizer.Normalize(
                combinedOcrText,
                productDomain=productDomain,
            )
            normalizedOcrFactTexts = ocrNormalizationResult.factTexts
            excludedOcrTextPreview = ocrNormalizationResult.excludedTextPreview

        return ProductClassificationInput(
            productPageUrl=productPageUrl,
            productName=parsedProductPage.productName,
            productDomain=productDomain,
            domainScopes=self._BuildDomainScopes(productDomain),
            shortDescription=parsedProductPage.shortDescription,
            brandName=parsedProductPage.brandName,
            packageType=parsedProductPage.packageType,
            saleUnit=parsedProductPage.saleUnit,
            noticeFieldTexts=self._BuildNoticeFieldTexts(fallbackNoticeFields),
            noticeOptionNames=noticeOptionNames,
            productNoticeText=productNoticeText,
            normalizedOcrFactTexts=normalizedOcrFactTexts,
            structuredProductFacts=structuredProductFacts,
            unresolvedProductFacts=unresolvedProductFacts,
            productFactConflicts=productFactConflicts,
            excludedOcrTextPreview=excludedOcrTextPreview,
            ocrText=combinedOcrText,
        )

    def _BuildNoticeFieldTexts(
        self,
        noticeFields: Sequence[NoticeFieldData],
    ) -> List[str]:
        fieldTexts: List[str] = []
        for noticeField in noticeFields:
            fieldName = noticeField.fieldName
            fieldValue = noticeField.fieldValue
            rawText = noticeField.rawText
            if rawText is not None and self._ContainsPlaceholderReference(rawText):
                continue
            if fieldValue is not None and self._ContainsPlaceholderReference(fieldValue):
                continue
            if fieldName is None and fieldValue is None:
                continue
            if fieldName is None:
                fieldTexts.append(fieldValue or "")
                continue
            if fieldValue is None:
                fieldTexts.append(fieldName)
                continue
            fieldTexts.append("{0}: {1}".format(fieldName, fieldValue))
        return fieldTexts

    def _BuildRawNoticeText(
        self,
        noticeFields: Sequence[NoticeFieldData],
        noticeOptions: Sequence[NoticeOptionData],
    ) -> str:
        rawTexts: List[str] = []
        seenRawTexts: Set[str] = set()
        for noticeOption in noticeOptions:
            rawText = noticeOption.rawText
            if rawText is not None and not self._ContainsPlaceholderReference(rawText):
                if rawText not in seenRawTexts:
                    seenRawTexts.add(rawText)
                    rawTexts.append(rawText)
                continue
            if noticeOption.optionName is not None:
                rawTexts.append(noticeOption.optionName)
            for fieldText in self._BuildNoticeFieldTexts(noticeOption.fields):
                if fieldText in seenRawTexts:
                    continue
                seenRawTexts.add(fieldText)
                rawTexts.append(fieldText)
        if not rawTexts:
            rawTexts = [
                rawText
                for rawText in (noticeField.rawText for noticeField in noticeFields)
                if rawText is not None
                and not self._ContainsPlaceholderReference(rawText)
            ]
        if not rawTexts:
            rawTexts = self._BuildNoticeFieldTexts(noticeFields)
        return NormalizeWhitespaceLines("\n".join(rawTexts))

    def _ContainsPlaceholderReference(self, text: str) -> bool:
        normalizedText = NormalizeWhiteSpace(text).lower()
        return PRODUCT_REFERENCE_PLACEHOLDER_PATTERN.search(normalizedText) is not None

    def _ExtractNoticeOptionNames(
        self,
        noticeOptions: Sequence[NoticeOptionData],
    ) -> List[str]:
        optionNames: List[str] = []
        seenOptionNames: Set[str] = set()
        for noticeOption in noticeOptions:
            optionName = noticeOption.optionName
            if optionName is None or optionName in seenOptionNames:
                continue
            seenOptionNames.add(optionName)
            optionNames.append(optionName)
        return optionNames

    def _BuildDomainScopes(self, productDomain: str) -> List[str]:
        normalizedProductDomain = NormalizeWhiteSpace(productDomain).lower()
        return list(
            PRODUCT_DOMAIN_SCOPE_MAP.get(
                normalizedProductDomain,
                PRODUCT_DOMAIN_SCOPE_MAP["unknown"],
            )
        )

    def _BuildCombinedOcrTextFromFlatData(
        self,
        sourceData: FlatSmokeArtifactData,
    ) -> str:
        if sourceData.combinedOcrText is not None:
            return sourceData.combinedOcrText

        return NormalizeWhitespaceLines(
            "\n".join(
                imageResult.ocrText
                for imageResult in sourceData.ocrImageResults
                if imageResult.ocrText is not None
            )
        )

    def _HasAnyKey(self, productData: Mapping[str, Any], *keys: str) -> bool:
        return any(key in productData for key in keys)

    def _LooksLikeFlatSmokeArtifact(self, productData: Mapping[str, Any]) -> bool:
        return self._HasAnyKey(
            productData,
            "product_name",
            "productName",
            "combined_ocr_text",
            "combinedOcrText",
            "ocr_image_results",
            "ocrImageResults",
            "raw_product_notice_text",
            "rawProductNoticeText",
            "product_notice_fields",
            "productNoticeFields",
        )
