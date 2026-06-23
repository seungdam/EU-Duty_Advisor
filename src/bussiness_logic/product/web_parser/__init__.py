"""Kurly product web parsing and collection components."""

from bussiness_logic.product.web_parser.kurly_parser import (
    KurlyBasePageParser,
    KurlyCosmeticsPageParser,
    KurlyDomainDetector,
    KurlyFoodPageParser,
)
from bussiness_logic.product.web_parser.kurly_domestic import KurlyDomesticPageParser
from bussiness_logic.product.web_parser.kurly_global import KurlyGlobalPageParser
from bussiness_logic.product.web_parser.kurly_market_collector import (
    KurlyCollectionError,
    KurlyPageCollector,
)
from bussiness_logic.product.web_parser.kurly_market_schema import (
    KurlyCollectionResult,
    KurlyProductDomain,
    KurlyProductPage,
    ProductNoticeField,
    ProductNoticeOption,
    RenderedPageEvidence,
)
from bussiness_logic.product.web_parser.kurly_page_adapter import KurlyPageAdapter

__all__ = [
    "KurlyBasePageParser",
    "KurlyCollectionError",
    "KurlyCollectionResult",
    "KurlyCosmeticsPageParser",
    "KurlyDomainDetector",
    "KurlyFoodPageParser",
    "KurlyGlobalPageParser",
    "KurlyPageAdapter",
    "KurlyPageCollector",
    "KurlyDomesticPageParser",
    "KurlyProductDomain",
    "KurlyProductPage",
    "ProductNoticeField",
    "ProductNoticeOption",
    "RenderedPageEvidence",
]
