"""Kurly domestic 상품 상세 parser."""

from typing import List, Optional

from bussiness_logic.product.web_parser.kurly_market_html import KurlyHtmlTextExtractor
from bussiness_logic.product.web_parser.kurly_market_schema import (
    KurlyProductDomain,
    KurlyProductPage,
)
from bussiness_logic.product.web_parser.kurly_parser import (
    KurlyBasePageParser,
    KurlyDomainDetector,
    KurlyCosmeticsPageParser,
    KurlyFoodPageParser,
)


__all__ = [
    "KurlyDomesticPageParser",
]


class KurlyDomesticPageParser:
    """상품고시정보 domain을 감지해 식품/화장품 parser로 분기한다."""

    def __init__(
        self,
        domainDetector: Optional[KurlyDomainDetector] = None,
        foodParser: Optional[KurlyFoodPageParser] = None,
        cosmeticsParser: Optional[KurlyCosmeticsPageParser] = None,
        fallbackParser: Optional[KurlyBasePageParser] = None,
    ) -> None:
        self._domainDetector = domainDetector or KurlyDomainDetector()
        self._foodParser = foodParser or KurlyFoodPageParser()
        self._cosmeticsParser = (
            cosmeticsParser or KurlyCosmeticsPageParser()
        )
        self._fallbackParser = fallbackParser or KurlyBasePageParser()

    def IsSupportedProductPageUrl(self, url: str) -> bool:
        return self._fallbackParser.IsSupportedProductPageUrl(url)

    def ParseHtml(
        self,
        htmlText: str,
        productPageUrl: Optional[str] = None,
    ) -> KurlyProductPage:
        textLines = KurlyHtmlTextExtractor().ExtractTextLines(htmlText)
        return self.ParseTextLines(textLines, productPageUrl=productPageUrl)

    def ParseText(
        self,
        pageText: str,
        productPageUrl: Optional[str] = None,
    ) -> KurlyProductPage:
        textLines = self.NormalizeTextLines(pageText.splitlines())
        return self.ParseTextLines(textLines, productPageUrl=productPageUrl)

    def ParseTextLines(
        self,
        textLines: List[str],
        productPageUrl: Optional[str] = None,
    ) -> KurlyProductPage:
        return self.ParseCollectedTextLines(
            textLines=textLines,
            productNoticeLines=[],
            productPageUrl=productPageUrl,
        )

    def ParseCollectedTextLines(
        self,
        textLines: List[str],
        productNoticeLines: List[str],
        productPageUrl: Optional[str] = None,
    ) -> KurlyProductPage:
        normalizedTextLines = self.NormalizeTextLines(textLines)
        normalizedNoticeLines = productNoticeLines or (
            self._fallbackParser._ExtractProductNoticeLines(normalizedTextLines)
        )
        parser = self._SelectParser(normalizedNoticeLines)
        return parser.ParseCollectedTextLines(
            textLines=normalizedTextLines,
            productNoticeLines=normalizedNoticeLines,
            productPageUrl=productPageUrl,
        )

    def NormalizeProductNoticeLines(self, textLines: List[str]) -> List[str]:
        return self._fallbackParser.NormalizeProductNoticeLines(textLines)

    def NormalizeTextLines(self, textLines: List[str]) -> List[str]:
        return self._fallbackParser.NormalizeTextLines(textLines)

    def DetectProductDomain(
        self,
        productNoticeLines: List[str],
    ) -> KurlyProductDomain:
        return self._domainDetector.Detect(productNoticeLines)

    def _SelectParser(
        self,
        productNoticeLines: List[str],
    ) -> KurlyBasePageParser:
        productDomain = self.DetectProductDomain(productNoticeLines)
        if productDomain == KurlyProductDomain.FOOD:
            return self._foodParser
        if productDomain == KurlyProductDomain.COSMETICS:
            return self._cosmeticsParser
        return self._fallbackParser
