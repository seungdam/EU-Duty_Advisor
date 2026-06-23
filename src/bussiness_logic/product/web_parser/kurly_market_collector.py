"""KurlyMarket product page collection via Playwright."""

from typing import Any, List, Optional, Protocol
from urllib.parse import urljoin, urlparse

from bussiness_logic.product.web_parser.kurly_market_schema import (
    KurlyCollectionResult,
    KurlyProductPage,
    ProductSummaryEvidence,
    RenderedPageEvidence,
)


class KurlyProductPageParserProtocol(Protocol):
    """Collector가 요구하는 KurlyMarket parser 최소 interface."""

    def IsSupportedProductPageUrl(self, url: str) -> bool:
        raise NotImplementedError

    def NormalizeTextLines(self, textLines: List[str]) -> List[str]:
        raise NotImplementedError

    def NormalizeProductNoticeLines(self, textLines: List[str]) -> List[str]:
        raise NotImplementedError

    def ParseCollectedTextLines(
        self,
        textLines: List[str],
        productNoticeLines: List[str],
        productPageUrl: Optional[str] = None,
    ) -> KurlyProductPage:
        raise NotImplementedError


class KurlyCollectionError(RuntimeError):
    """KurlyMarket 상품 페이지 수집이 실패했을 때 사용한다."""


class KurlyPageCollector:
    """Playwright로 KurlyMarket 상품 페이지를 제한 스크롤해 수집한다."""

    DEFAULT_TIMEOUT_MILLISECONDS = 30000
    DEFAULT_SCROLL_COUNT = 8
    DEFAULT_SCROLL_WAIT_MILLISECONDS = 500
    DEFAULT_VIEWPORT_WIDTH = 1440
    DEFAULT_VIEWPORT_HEIGHT = 1600
    DEFAULT_SCROLL_STEP_RATIO = 0.75
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        parser: Optional[KurlyProductPageParserProtocol] = None,
        headless: bool = True,
        timeoutMilliseconds: Optional[int] = None,
        scrollCount: Optional[int] = None,
        scrollWaitMilliseconds: Optional[int] = None,
    ) -> None:
        timeoutMilliseconds = (
            self.DEFAULT_TIMEOUT_MILLISECONDS
            if timeoutMilliseconds is None
            else timeoutMilliseconds
        )
        scrollCount = (
            self.DEFAULT_SCROLL_COUNT
            if scrollCount is None
            else scrollCount
        )
        scrollWaitMilliseconds = (
            self.DEFAULT_SCROLL_WAIT_MILLISECONDS
            if scrollWaitMilliseconds is None
            else scrollWaitMilliseconds
        )
        if timeoutMilliseconds <= 0:
            raise ValueError("timeoutMilliseconds must be greater than 0.")
        if scrollCount < 0:
            raise ValueError("scrollCount must be greater than or equal to 0.")
        if scrollWaitMilliseconds < 0:
            raise ValueError(
                "scrollWaitMilliseconds must be greater than or equal to 0."
            )

        self._parser = parser or self._BuildDefaultParser()
        self._headless = headless
        self._timeoutMilliseconds = timeoutMilliseconds
        self._scrollCount = scrollCount
        self._scrollWaitMilliseconds = scrollWaitMilliseconds

    def Collect(self, productPageUrl: str) -> KurlyCollectionResult:
        self.ValidateProductPageUrl(productPageUrl)
        renderedPageEvidence = self.CollectRenderedPageEvidence(productPageUrl)
        return self.BuildCollectionResult(renderedPageEvidence)

    def ValidateProductPageUrl(self, productPageUrl: str) -> None:
        if not self._parser.IsSupportedProductPageUrl(productPageUrl):
            raise KurlyCollectionError(
                "unsupported KurlyMarket product page URL: {0}".format(
                    productPageUrl,
                )
            )

    def CollectRenderedPageEvidence(
        self,
        productPageUrl: str,
    ) -> RenderedPageEvidence:
        self.ValidateProductPageUrl(productPageUrl)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise KurlyCollectionError(
                "playwright is required for KurlyPageCollector."
            ) from error

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self._headless)
                try:
                    context = browser.new_context(
                        user_agent=self.DEFAULT_USER_AGENT,
                        viewport={
                            "width": self.DEFAULT_VIEWPORT_WIDTH,
                            "height": self.DEFAULT_VIEWPORT_HEIGHT,
                        },
                    )
                    try:
                        page = context.new_page()
                        page.route("**/*", self._BlockUnnecessaryResource)
                        page.goto(
                            productPageUrl,
                            wait_until="domcontentloaded",
                            timeout=self._timeoutMilliseconds,
                        )
                        self._ScrollUntilProductNoticeLoaded(page)
                        visibleText = self._ReadVisibleText(page)
                        productSummaryEvidence = self._ReadProductSummaryEvidence(
                            page,
                        )
                        productNoticeText = self._ReadProductNoticeText(page)
                        productDetailImageUrls = self._ReadProductDetailImageUrls(
                            page,
                        )
                    finally:
                        context.close()
                finally:
                    browser.close()
        except Exception as error:
            raise KurlyCollectionError(
                "failed to collect KurlyMarket product page: {0}".format(error)
            ) from error

        return RenderedPageEvidence(
            productPageUrl=productPageUrl,
            visibleText=visibleText,
            productSummaryEvidence=productSummaryEvidence,
            productNoticeText=productNoticeText,
            productDetailImageUrls=productDetailImageUrls,
        )

    def BuildCollectionResult(
        self,
        renderedPageEvidence: RenderedPageEvidence,
    ) -> KurlyCollectionResult:
        textLines = self._parser.NormalizeTextLines(
            renderedPageEvidence.visibleText.splitlines()
        )
        productNoticeLines = self._parser.NormalizeProductNoticeLines(
            renderedPageEvidence.productNoticeText.splitlines(),
        )
        parsedProductPage = self._parser.ParseCollectedTextLines(
            textLines=textLines,
            productNoticeLines=productNoticeLines,
            productPageUrl=renderedPageEvidence.productPageUrl,
        )
        parsedProductPage = self._ApplyProductSummaryEvidence(
            parsedProductPage,
            renderedPageEvidence.productSummaryEvidence,
        )
        ocrCandidateImageUrls = self._BuildOcrCandidateImageUrls(
            parsedProductPage,
            renderedPageEvidence.productDetailImageUrls,
        )
        warnings = self._BuildCollectionWarnings(
            parsedProductPage,
            renderedPageEvidence.productDetailImageUrls,
            ocrCandidateImageUrls,
        )

        return KurlyCollectionResult(
            productPageUrl=renderedPageEvidence.productPageUrl,
            parsedProductPage=parsedProductPage,
            visibleTextLineCount=len(textLines),
            productNoticeTextLineCount=len(productNoticeLines),
            productDetailImageUrls=renderedPageEvidence.productDetailImageUrls,
            ocrCandidateImageUrls=ocrCandidateImageUrls,
            warnings=warnings,
        )

    @staticmethod
    def _BuildDefaultParser() -> KurlyProductPageParserProtocol:
        from bussiness_logic.product.web_parser.kurly_page_adapter import KurlyPageAdapter

        return KurlyPageAdapter()


    def _BlockUnnecessaryResource(self, route: Any) -> None:
        if route.request.resource_type in ("media", "font"):
            route.abort()
            return
        route.continue_()

    def _ScrollUntilProductNoticeLoaded(self, page: Any) -> None:
        prepareRenderedPage = getattr(self._parser, "PrepareRenderedPage", None)
        if callable(prepareRenderedPage):
            if prepareRenderedPage(
                page,
                self._scrollCount,
                self._scrollWaitMilliseconds,
            ):
                return

        scrollStep = max(
            1,
            int(
                self.DEFAULT_VIEWPORT_HEIGHT
                * self.DEFAULT_SCROLL_STEP_RATIO
            ),
        )
        noticeFound = False
        for _ in range(self._scrollCount):
            bodyText = self._ReadVisibleText(page)
            if "상품고시정보" in bodyText:
                noticeFound = True
            if noticeFound and "WHY KURLY" in bodyText:
                return

            page.evaluate("(step) => window.scrollBy(0, step)", scrollStep)
            if self._scrollWaitMilliseconds > 0:
                page.wait_for_timeout(self._scrollWaitMilliseconds)

    def _ReadVisibleText(self, page: Any) -> str:
        try:
            value = page.locator("body").inner_text(
                timeout=self._timeoutMilliseconds,
            )
        except Exception:
            return ""

        if not isinstance(value, str):
            return ""
        return value

    def _ReadProductSummaryEvidence(self, page: Any) -> ProductSummaryEvidence:
        readProductSummaryEvidence = getattr(
            self._parser,
            "ReadProductSummaryEvidence",
            None,
        )
        if callable(readProductSummaryEvidence):
            value = readProductSummaryEvidence(page)
            if isinstance(value, ProductSummaryEvidence):
                return value
            if isinstance(value, dict):
                return ProductSummaryEvidence.model_validate(value)

        try:
            value = page.evaluate(
                """
                () => {
                    const normalize = (text) => (
                        text || ""
                    ).replace(/\\s+/g, " ").trim();
                    const lines = (
                        document.body.innerText || ""
                    ).split("\\n").map(normalize).filter(Boolean);
                    const isPriceLike = (text) => (
                        /^\\d[\\d,]*\\s*원~?$/.test(text)
                        || /^\\d+%$/.test(text)
                    );
                    const isSectionHeading = (text) => (
                        text === "상품고시정보"
                        || text === "WHY KURLY"
                        || text.startsWith("상품 후기")
                        || text.startsWith("상품 문의")
                    );
                    const headings = Array.from(
                        document.querySelectorAll("h1,h2,h3,[role='heading']")
                    ).map((element) => normalize(
                        element.innerText || element.textContent || ""
                    )).filter(Boolean);
                    const productName = headings.find((text) => (
                        !isPriceLike(text) && !isSectionHeading(text)
                    )) || "";
                    const productLineIndex = lines.findIndex(
                        (line) => line === productName
                    );
                    const summaryFieldLabels = new Set([
                        "원산지",
                        "배송",
                        "판매자",
                        "포장타입",
                        "판매단위",
                        "중량/용량",
                        "알레르기정보",
                        "상품선택"
                    ]);
                    const readShortDescription = () => {
                        if (productLineIndex < 0) {
                            return "";
                        }
                        const candidate = lines[productLineIndex + 1] || "";
                        if (!candidate || isPriceLike(candidate)) {
                            return "";
                        }
                        if (candidate.includes(":")) {
                            return "";
                        }
                        if (summaryFieldLabels.has(candidate)) {
                            return "";
                        }
                        return candidate;
                    };
                    const readBrandName = () => {
                        const brandMatch = productName.match(/^\\[([^\\]]+)\\]/);
                        if (brandMatch) {
                            return brandMatch[1].trim();
                        }
                        if (productLineIndex < 0) {
                            return "";
                        }
                        for (let index = productLineIndex - 1; index >= 0; index -= 1) {
                            const line = lines[index];
                            if (!line || isPriceLike(line)) {
                                continue;
                            }
                            if (
                                /^\\d+$/.test(line)
                                || line.includes("후기")
                                || line.includes("재구매")
                                || line.includes("샛별배송")
                                || line.includes("Kurly")
                                || line === "단독"
                                || line === "카테고리"
                            ) {
                                continue;
                            }
                            if (line.length > 30) {
                                continue;
                            }
                            return line;
                        }
                        return "";
                    };
                    return {
                        product_name: productName,
                        short_description: readShortDescription(),
                        brand_name: readBrandName()
                    };
                }
                """
            )
        except Exception:
            return ProductSummaryEvidence()
        if not isinstance(value, dict):
            return ProductSummaryEvidence()
        return ProductSummaryEvidence.model_validate(value)

    @staticmethod
    def _ApplyProductSummaryEvidence(
            parsedProductPage: KurlyProductPage,
        productSummaryEvidence: ProductSummaryEvidence,
    ) -> KurlyProductPage:
        updates = {}
        if productSummaryEvidence.productName:
            updates["productName"] = productSummaryEvidence.productName
        if productSummaryEvidence.shortDescription:
            updates["shortDescription"] = productSummaryEvidence.shortDescription
        if productSummaryEvidence.brandName:
            updates["brandName"] = productSummaryEvidence.brandName
        if not updates:
            return parsedProductPage
        return parsedProductPage.model_copy(update=updates)

    def _ReadProductNoticeText(self, page: Any) -> str:
        readProductNoticeText = getattr(self._parser, "ReadProductNoticeText", None)
        if callable(readProductNoticeText):
            value = readProductNoticeText(page)
            if isinstance(value, str):
                return value

        try:
            value = page.evaluate(
                """
                () => {
                    const normalize = (text) => (
                        text || ""
                    ).replace(/\\s+/g, " ").trim();
                    const mainRoot = document.body;
                    const lines = [];
                    const pushLine = (text) => {
                        const line = normalize(text);
                        if (!line) {
                            return;
                        }
                        if (lines.length > 0 && lines[lines.length - 1] === line) {
                            return;
                        }
                        lines.push(line);
                    };
                    const readText = (element) => normalize(
                        element.innerText || element.textContent || ""
                    );
                    const isStopText = (text) => (
                        text === "WHY KURLY"
                        || text.startsWith("상품 후기")
                        || text.startsWith("고객 후기")
                        || text.startsWith("상품 리뷰")
                        || text.startsWith("고객 리뷰")
                        || text.startsWith("상품 문의")
                        || text.startsWith("고객행복센터")
                    );
                    const headingCandidates = Array.from(
                        mainRoot.querySelectorAll("h2,h3,h4,h5,[role='heading']")
                    );
                    const noticeHeadingFromSemanticNodes = (
                        headingCandidates.find((element) => {
                            const text = readText(element);
                            return text === "상품고시정보"
                                || text.startsWith("상품고시정보");
                        })
                    );
                    const noticeHeading = noticeHeadingFromSemanticNodes || (
                        Array.from(
                            mainRoot.querySelectorAll(
                                "button,a,span,strong,p,dt,th,div"
                            )
                        )
                            .filter((element) => {
                                const text = readText(element);
                                return text === "상품고시정보"
                                    || text.startsWith("상품고시정보");
                            })
                            .sort((left, right) => (
                                readText(left).length - readText(right).length
                            ))[0]
                    );
                    if (!noticeHeading) {
                        return "";
                    }
                    pushLine(readText(noticeHeading));

                    const blockSelector = [
                        "h2",
                        "h3",
                        "h4",
                        "h5",
                        "li",
                        "p",
                        "dt",
                        "dd",
                        "th",
                        "td",
                        "div"
                    ].join(",");
                    const hasChildBlock = (node) => !!node.querySelector(
                        blockSelector
                    );
                    const allNodes = Array.from(
                        mainRoot.querySelectorAll(blockSelector)
                    );
                    let collecting = false;
                    for (const node of allNodes) {
                        if (node === noticeHeading) {
                            collecting = true;
                            continue;
                        }
                        if (!collecting) {
                            continue;
                        }
                        const text = readText(node);
                        if (!text) {
                            continue;
                        }
                        if (isStopText(text)) {
                            break;
                        }
                        const tagName = node.tagName.toLowerCase();
                        if (tagName === "div" && hasChildBlock(node)) {
                            continue;
                        }
                        pushLine(text);
                    }
                    return lines.join("\\n");
                }
                """
            )
        except Exception:
            return ""
        if not isinstance(value, str):
            return ""
        return value

    def _ReadProductDetailImageUrls(self, page: Any) -> List[str]:
        values = page.evaluate(
            """
            () => {
                const readUrlValues = (element) => [
                    element.currentSrc || "",
                    element.src || "",
                    element.getAttribute("src") || "",
                    element.getAttribute("data-src") || "",
                    element.getAttribute("data-original") || "",
                    element.getAttribute("data-lazy") || "",
                    element.getAttribute("srcset") || ""
                ];
                return Array.from(document.querySelectorAll("img"))
                    .flatMap((element) => readUrlValues(element))
                    .filter((value) => value && value.trim() !== "");
            }
            """
        )
        if not isinstance(values, list):
            return []

        imageUrls: List[str] = []
        seenUrls: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            for imageUrl in self._ExpandImageUrlValue(page.url, value):
                if imageUrl in seenUrls:
                    continue
                if not self._LooksProductDetailImageUrl(imageUrl):
                    continue
                seenUrls.add(imageUrl)
                imageUrls.append(imageUrl)
        return imageUrls

    @staticmethod
    def _ExpandImageUrlValue(baseUrl: str, value: str) -> List[str]:
        imageUrls: List[str] = []
        for token in value.split(","):
            candidate = token.strip().split(" ")[0].strip()
            if candidate == "":
                continue
            imageUrls.append(urljoin(baseUrl, candidate))
        return imageUrls

    def _LooksProductDetailImageUrl(self, imageUrl: str) -> bool:
        looksProductDetailImageUrl = getattr(
            self._parser,
            "LooksProductDetailImageUrl",
            None,
        )
        if callable(looksProductDetailImageUrl):
            value = looksProductDetailImageUrl(imageUrl)
            if isinstance(value, bool):
                return value

        parsedUrl = urlparse(imageUrl)
        hostName = parsedUrl.netloc.lower()
        path = parsedUrl.path.lower()

        if hostName == "img-cf.kurly.com" and "/goodsview/" in path:
            return True

        if hostName == "product-image.kurly.com":
            if "/product/description/" in path:
                return True
            return "/src/product/image/" in path and "%3e1010x" in path

        return False

    @staticmethod
    def _BuildOcrCandidateImageUrls(
            parsedProductPage: KurlyProductPage,
        productDetailImageUrls: List[str],
    ) -> List[str]:
        if not parsedProductPage.requiresOcrFallback:
            return []
        return list(productDetailImageUrls)

    @staticmethod
    def _BuildCollectionWarnings(
            parsedProductPage: KurlyProductPage,
        productDetailImageUrls: List[str],
        ocrCandidateImageUrls: List[str],
    ) -> List[str]:
        warnings = list(parsedProductPage.warnings)
        if not productDetailImageUrls:
            warnings.append("product detail image URLs not found")
        if parsedProductPage.requiresOcrFallback and not ocrCandidateImageUrls:
            warnings.append(
                "OCR fallback is required but no OCR candidate image URLs were found"
            )
        return warnings
