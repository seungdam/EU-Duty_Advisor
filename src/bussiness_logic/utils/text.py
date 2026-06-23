"""문자열 정규화, URL 판별, 단순 term 검색 helper."""

import re
from typing import List


URL_PATTERN = re.compile(
    r"^(https?://|www\.)[^\s]+$|^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/[^\s]*)?$"
)


def NormalizeWhiteSpace(text: str) -> str:
    return " ".join((text or "").strip().split())

def NormalizeWhitespaceLines(text: str) -> str:
    return "\n".join(
        NormalizeWhiteSpace(line)
        for line in (text or "").splitlines()
        if NormalizeWhiteSpace(line)
    )

def IsUrlLike(text: str) -> bool:
    return URL_PATTERN.match(text.strip()) is not None


def FindContainedTerms(text: str, terms: set[str]) -> List[str]:
    loweredText = text.lower()
    return sorted(term for term in terms if term in loweredText)
