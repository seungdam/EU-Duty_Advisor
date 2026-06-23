"""KurlyMarket HTML text extraction helpers."""

from html.parser import HTMLParser
from typing import List, Optional

from bussiness_logic.utils import NormalizeWhiteSpace


class KurlyHtmlTextExtractor(HTMLParser):
    """HTML을 block 단위 text line으로 변환한다."""

    _BLOCK_TAGS = {
        "article",
        "br",
        "dd",
        "div",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "li",
        "main",
        "p",
        "section",
        "td",
        "th",
        "title",
        "tr",
        "ul",
    }
    _SKIP_TAGS = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skipDepth = 0

    def ExtractTextLines(self, htmlText: str) -> List[str]:
        self._parts = []
        self._skipDepth = 0
        self.feed(htmlText)
        self.close()
        return self._BuildTextLines()

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        loweredTag = tag.lower()
        if loweredTag in self._SKIP_TAGS:
            self._skipDepth += 1
            return
        if self._skipDepth > 0:
            return
        if loweredTag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        loweredTag = tag.lower()
        if loweredTag in self._SKIP_TAGS:
            self._skipDepth = max(0, self._skipDepth - 1)
            return
        if self._skipDepth > 0:
            return
        if loweredTag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skipDepth > 0:
            return
        if data.strip() == "":
            return
        self._parts.append(data)

    def _BuildTextLines(self) -> List[str]:
        text = "".join(self._parts)
        return [
            normalizedLine
            for normalizedLine in (
                NormalizeWhiteSpace(line) for line in text.splitlines()
            )
            if normalizedLine != ""
        ]
