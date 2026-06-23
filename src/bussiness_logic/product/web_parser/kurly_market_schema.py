"""KurlyMarket product page collection schema."""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


class KurlyProductDomain(str, Enum):
    """Kurly 상품고시정보 기준의 상품 domain."""

    FOOD = "food"
    COSMETICS = "cosmetics"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class ProductNoticeField(BaseModel):
    """상품고시정보 label-value record."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    fieldName: str = Field(alias="field_name")
    fieldValue: Optional[str] = Field(default=None, alias="field_value")
    requiresOcrFallback: bool = Field(default=False, alias="requires_ocr_fallback")
    rawText: str = Field(default="", alias="raw_text", exclude=True)


class ProductNoticeOption(BaseModel):
    """상품 옵션 하나에 정규화된 상품고시정보 field set."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    optionName: Optional[str] = Field(default=None, alias="option_name")
    fields: List[ProductNoticeField] = Field(default_factory=list)
    rawText: str = Field(default="", alias="raw_text", exclude=True)


class KurlyProductPage(BaseModel):
    """KurlyMarket 상품 상세 parser 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    productPageUrl: Optional[str] = Field(default=None, alias="product_page_url")
    productDomain: KurlyProductDomain = Field(
        default=KurlyProductDomain.UNKNOWN,
        alias="product_domain",
    )
    productName: Optional[str] = Field(default=None, alias="product_name")
    shortDescription: Optional[str] = Field(default=None, alias="short_description")
    brandName: Optional[str] = Field(default=None, alias="brand_name")
    packageType: Optional[str] = Field(default=None, alias="package_type")
    saleUnit: Optional[str] = Field(default=None, alias="sale_unit")
    productNoticeOptionNames: List[str] = Field(
        default_factory=list,
        alias="product_notice_option_names",
    )
    productNoticeFields: List[ProductNoticeField] = Field(
        default_factory=list,
        alias="product_notice_fields",
        exclude=True,
    )
    productNoticeOptions: List[ProductNoticeOption] = Field(
        default_factory=list,
        alias="product_notice_options",
    )
    rawProductNoticeText: str = Field(
        default="",
        alias="raw_product_notice_text",
        exclude=True,
    )
    imageReferenceDetected: bool = Field(
        default=False,
        alias="image_reference_detected",
    )
    requiresOcrFallback: bool = Field(
        default=False,
        alias="requires_ocr_fallback",
    )
    warnings: List[str] = Field(default_factory=list)

    @computed_field(alias="raw_product_notice_text_length")
    @property
    def rawProductNoticeTextLength(self) -> int:
        return len(self.rawProductNoticeText)

    @computed_field(alias="product_notice_field_count")
    @property
    def productNoticeFieldCount(self) -> int:
        return len(self.productNoticeFields)

    @computed_field(alias="product_notice_option_count")
    @property
    def productNoticeOptionCount(self) -> int:
        return len(self.productNoticeOptions)


class KurlyCollectionResult(BaseModel):
    """렌더링된 KurlyMarket 상품 페이지 수집 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    productPageUrl: str = Field(alias="product_page_url")
    parsedProductPage: KurlyProductPage = Field(alias="parsed_product_page")
    visibleTextLineCount: int = Field(default=0, alias="visible_text_line_count")
    productNoticeTextLineCount: int = Field(
        default=0,
        alias="product_notice_text_line_count",
    )
    productDetailImageUrls: List[str] = Field(
        default_factory=list,
        alias="product_detail_image_urls",
        exclude=True,
    )
    ocrCandidateImageUrls: List[str] = Field(
        default_factory=list,
        alias="ocr_candidate_image_urls",
        exclude=True,
    )
    warnings: List[str] = Field(default_factory=list)

    @computed_field(alias="product_detail_image_url_count")
    @property
    def productDetailImageUrlCount(self) -> int:
        return len(self.productDetailImageUrls)

    @computed_field(alias="ocr_candidate_image_url_count")
    @property
    def ocrCandidateImageUrlCount(self) -> int:
        return len(self.ocrCandidateImageUrls)


class ProductSummaryEvidence(BaseModel):
    """상품 상단 영역에서 직접 읽은 요약 정보."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    productName: Optional[str] = Field(default=None, alias="product_name")
    shortDescription: Optional[str] = Field(default=None, alias="short_description")
    brandName: Optional[str] = Field(default=None, alias="brand_name")


class RenderedPageEvidence(BaseModel):
    """Playwright 렌더링 이후 parser에 넘길 원천 증거."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    productPageUrl: str = Field(alias="product_page_url")
    visibleText: str = Field(default="", alias="visible_text", exclude=True)
    productNoticeText: str = Field(
        default="",
        alias="product_notice_text",
        exclude=True,
    )
    productSummaryEvidence: ProductSummaryEvidence = Field(
        default_factory=ProductSummaryEvidence,
        alias="product_summary_evidence",
        exclude=True,
    )
    productDetailImageUrls: List[str] = Field(
        default_factory=list,
        alias="product_detail_image_urls",
        exclude=True,
    )

    @computed_field(alias="visible_text_length")
    @property
    def visibleTextLength(self) -> int:
        return len(self.visibleText)

    @computed_field(alias="product_notice_text_length")
    @property
    def productNoticeTextLength(self) -> int:
        return len(self.productNoticeText)

    @computed_field(alias="product_detail_image_url_count")
    @property
    def productDetailImageUrlCount(self) -> int:
        return len(self.productDetailImageUrls)
