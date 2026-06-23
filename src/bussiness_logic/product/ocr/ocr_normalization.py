"""OCR text normalization for product-source collection results."""

import re
from typing import List, Optional, Sequence, Set

from pydantic import BaseModel, ConfigDict, Field

from bussiness_logic.utils import NormalizeWhiteSpace, NormalizeWhitespaceLines


OCR_FACT_LABEL_KEYWORDS = [
    "원료명",
    "원재료명",
    "원재료",
    "성분",
    "전성분",
    "ingredients",
    "함량",
    "영양성분",
    "영양정보",
    "식품의 유형",
    "식품유형",
    "제품명",
    "내용량",
    "내용물",
    "중량",
    "용량",
    "수량",
    "보관",
    "제조국",
    "제조원",
    "제조사",
    "원산지",
    "소비기한",
    "유통기한",
    "품질유지기한",
    "포장재질",
    "품목보고번호",
    "유통전문판매원",
    "주의사항",
    "사용방법",
    "기능성",
    "피부",
    "inci",
]
OCR_FIELD_CAPTURE_FOLLOWING_LINE_LIMIT = 4
OCR_INGREDIENT_FIELD_LABELS = {
    "원료명",
    "원재료명",
    "원재료",
    "성분",
    "전성분",
    "ingredients",
    "inci",
}
OCR_NUTRITION_FIELD_LABELS = {
    "영양성분",
    "영양정보",
}
OCR_INGREDIENT_CAPTURE_WEAK_INTERRUPT_LABELS = {
    "보관",
    "주의사항",
}
OCR_LONG_FIELD_CAPTURE_FOLLOWING_LINE_LIMIT = 36
OCR_NUTRITION_FIELD_CAPTURE_FOLLOWING_LINE_LIMIT = 12
OCR_MARKETING_KEYWORDS = [
    "멤버십",
    "적립",
    "보러 가기",
    "컬리의 사적인",
    "사용 후기",
    "상품전략가",
    "추천하고 싶나요",
    "향은 어땠",
    "마음에 든",
    "구매 전",
    "후기",
    "리뷰",
    "이벤트",
    "샛별배송",
    "배송",
    "브랜드와 생산자",
    "brand&artisan",
    "recommendation",
]
OCR_FIELD_VALUE_NOISE_KEYWORDS = [
    "냉장에서 해동",
    "조리해주세요",
    "조리시간",
    "구워",
    "오븐",
    "오본",
    "에어프라이어",
    "그릴",
    "플러스친구",
    "인스타그램",
    "qr",
    "바코드",
    "피해 보상",
    "불량식품 신고",
    "국번없이",
    "고객상담실",
    "혼입가능",
    "혼입 가능",
    "조리시 안전사고",
    "안전관리인증",
    "나트륨항량",
    "평균함량",
    "1일섭취",
    "수신자요금부담",
]
OCR_OBSERVED_QUANTITY_EXCLUDED_KEYWORDS = [
    "영양정보",
    "영양성분",
    "나트륨",
    "탄수화물",
    "당류",
    "지방",
    "트랜스지방",
    "포화지방",
    "콜레스테롤",
    "단백질",
    "kcal",
    "기준치",
]
OCR_FIELD_TEXT_CUT_KEYWORDS = [
    *OCR_OBSERVED_QUANTITY_EXCLUDED_KEYWORDS,
    "품목보고번호",
    "포장재질",
    "축산물",
    "유통전문판매원",
    "반품",
    "교환",
    "고객센터",
    "플러스친구",
    "인스타그램",
    "16.5cm",
    "6.5cm",
    "500ml",
]
PRODUCT_REFERENCE_PLACEHOLDER_KEYWORDS = [
    "상품설명 및 상품이미지 참조",
    "상품설명 및 상품 이미지 참조",
    "상품설명 참조",
    "상품이미지 참조",
    "상품 이미지 참조",
    "상품 이미지를 참고",
    "상품이미지를 참고",
    "상세이미지 참조",
    "상세 이미지 참조",
]
OCR_FACT_LABEL_MATCHERS = tuple(
    (
        fieldLabel,
        NormalizeWhiteSpace(fieldLabel).lower(),
        NormalizeWhiteSpace(fieldLabel).lower().replace(" ", ""),
    )
    for fieldLabel in sorted(
        OCR_FACT_LABEL_KEYWORDS,
        key=lambda label: len(NormalizeWhiteSpace(label)),
        reverse=True,
    )
)
OCR_MARKETING_KEYWORD_PATTERN = re.compile(
    "|".join(re.escape(keyword.lower()) for keyword in OCR_MARKETING_KEYWORDS),
)
OCR_FIELD_VALUE_NOISE_PATTERN = re.compile(
    "|".join(re.escape(keyword.lower()) for keyword in OCR_FIELD_VALUE_NOISE_KEYWORDS),
)
OCR_OBSERVED_QUANTITY_NOISE_PATTERN = re.compile(
    "|".join(
        re.escape(keyword.lower())
        for keyword in [
            *OCR_MARKETING_KEYWORDS,
            *OCR_FIELD_VALUE_NOISE_KEYWORDS,
            *OCR_OBSERVED_QUANTITY_EXCLUDED_KEYWORDS,
        ]
    ),
)
OCR_FIELD_TEXT_CUT_PATTERN = re.compile(
    "|".join(re.escape(keyword.lower()) for keyword in OCR_FIELD_TEXT_CUT_KEYWORDS),
)
PRODUCT_REFERENCE_PLACEHOLDER_PATTERN = re.compile(
    "|".join(
        re.escape(keyword.lower())
        for keyword in PRODUCT_REFERENCE_PLACEHOLDER_KEYWORDS
    ),
)
OCR_PERCENT_VALUE_PATTERN = re.compile(r"\d+(?:[.,]\d+)?\s*%")
OCR_KOREAN_TEXT_PATTERN = re.compile(r"[가-힣]")
OCR_MEANINGFUL_LATIN_TEXT_PATTERN = re.compile(r"[A-Za-z]{4,}")
OCR_NUMBER_SYMBOL_ONLY_PATTERN = re.compile(r"[0-9\s.,:/|()%-]+")
OCR_NUMBER_UNIT_ONLY_PATTERN = re.compile(
    r"[0-9\s.,:/|()%-]+(?:mg|g|kg|ml|l|kcal|mc)+[0-9\s.,:/|()%-]*",
)


class ProductOcrFactNormalizationResult(BaseModel):
    """OCR 원문에서 분류 판단에 필요한 상품 사실 라인만 추린 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    factTexts: List[str] = Field(default_factory=list, alias="fact_texts")
    excludedTextPreview: str = Field(default="", alias="excluded_text_preview")
    rawLineCount: int = Field(default=0, alias="raw_line_count")
    factLineCount: int = Field(default=0, alias="fact_line_count")


class ProductOcrFactNormalizer:
    """상품 OCR 원문에서 마케팅 문구를 줄이고 분류 관련 라인을 보존한다."""

    def Normalize(
        self,
        ocrText: str,
        productDomain: str = "unknown",
    ) -> ProductOcrFactNormalizationResult:
        del productDomain

        rawLines = [
            NormalizeWhiteSpace(line)
            for line in NormalizeWhitespaceLines(ocrText).splitlines()
        ]
        lines = [line for line in rawLines if line]
        factTexts: List[str] = []
        excludedTexts: List[str] = []
        seenFactTexts: Set[str] = set()
        index = 0

        while index < len(lines):
            line = lines[index]
            normalizedLine = line.lower()
            fieldLabel = self._FindFieldLabel(normalizedLine)

            if self._ContainsPlaceholderReference(normalizedLine):
                excludedTexts.append(line)
                index += 1
                continue

            if fieldLabel is None:
                observedFactText = self._BuildObservedQuantityFact(line)
                if observedFactText is not None and observedFactText not in seenFactTexts:
                    seenFactTexts.add(observedFactText)
                    factTexts.append(observedFactText)
                else:
                    excludedTexts.append(line)
                index += 1
                continue

            fieldLines = [line]
            index += 1
            capturedFollowingLineCount = 0
            captureFollowingLineLimit = self._ReadFieldCaptureFollowingLineLimit(
                fieldLabel,
            )
            while (
                index < len(lines)
                and capturedFollowingLineCount < captureFollowingLineLimit
            ):
                nextLine = lines[index]
                normalizedNextLine = nextLine.lower()
                if self._ContainsPlaceholderReference(normalizedNextLine):
                    excludedTexts.append(nextLine)
                    index += 1
                    continue
                nextFieldLabel = self._FindFieldLabel(normalizedNextLine)
                if nextFieldLabel is not None:
                    if (
                        fieldLabel in OCR_INGREDIENT_FIELD_LABELS
                        and nextFieldLabel in OCR_INGREDIENT_CAPTURE_WEAK_INTERRUPT_LABELS
                    ):
                        normalizedNextFieldLabel = NormalizeWhiteSpace(
                            nextFieldLabel,
                        ).lower()
                        hasMergedValueText = (
                            len(normalizedNextLine.strip(" :：·-*[]()"))
                            > len(normalizedNextFieldLabel) + 4
                        )
                        if (
                            hasMergedValueText
                            and OCR_FIELD_VALUE_NOISE_PATTERN.search(
                                normalizedNextLine,
                            )
                            is None
                        ):
                            fieldLines.append(nextLine)
                            capturedFollowingLineCount += 1
                        else:
                            excludedTexts.append(nextLine)
                        index += 1
                        continue
                    break
                if self._ShouldCaptureFieldValueLine(normalizedNextLine):
                    fieldLines.append(nextLine)
                    capturedFollowingLineCount += 1
                else:
                    excludedTexts.append(nextLine)
                index += 1

            factText = self._BuildFieldFactText(fieldLabel, fieldLines)
            if factText in seenFactTexts:
                continue
            seenFactTexts.add(factText)
            factTexts.append(factText)

        return ProductOcrFactNormalizationResult(
            factTexts=factTexts,
            excludedTextPreview=NormalizeWhitespaceLines(
                "\n".join(excludedTexts),
            )[:1000],
            rawLineCount=len(lines),
            factLineCount=len(factTexts),
        )

    def _FindFieldLabel(self, normalizedLine: str) -> Optional[str]:
        normalizedLine = normalizedLine.strip(" :：·-*[]()")
        compactLine = normalizedLine.replace(" ", "")
        for (
            fieldLabel,
            normalizedFieldLabel,
            compactFieldLabel,
        ) in OCR_FACT_LABEL_MATCHERS:
            if normalizedLine.startswith(normalizedFieldLabel):
                return fieldLabel
            if compactLine.startswith(compactFieldLabel):
                return fieldLabel
        return None

    def _ShouldCaptureFieldValueLine(self, normalizedLine: str) -> bool:
        if normalizedLine == "":
            return False
        if OCR_MARKETING_KEYWORD_PATTERN.search(normalizedLine) is not None:
            return False
        if OCR_FIELD_VALUE_NOISE_PATTERN.search(normalizedLine) is not None:
            return False
        if self._FindFieldLabel(normalizedLine) is not None:
            return False
        if normalizedLine in {"other", "haccp"}:
            return False
        if OCR_NUMBER_SYMBOL_ONLY_PATTERN.fullmatch(normalizedLine) is not None:
            return False
        if OCR_NUMBER_UNIT_ONLY_PATTERN.fullmatch(normalizedLine) is not None:
            return False
        if len(normalizedLine) <= 2:
            return False
        return True

    def _ReadFieldCaptureFollowingLineLimit(self, fieldLabel: str) -> int:
        if fieldLabel in OCR_INGREDIENT_FIELD_LABELS:
            return OCR_LONG_FIELD_CAPTURE_FOLLOWING_LINE_LIMIT
        if fieldLabel in OCR_NUTRITION_FIELD_LABELS:
            return OCR_NUTRITION_FIELD_CAPTURE_FOLLOWING_LINE_LIMIT
        return OCR_FIELD_CAPTURE_FOLLOWING_LINE_LIMIT

    def _BuildFieldFactText(
        self,
        fieldLabel: str,
        fieldLines: Sequence[str],
    ) -> str:
        normalizedLines: List[str] = []
        for line in fieldLines:
            normalizedLine = self._TrimFieldNoiseFromLine(NormalizeWhiteSpace(line))
            if normalizedLine != "":
                normalizedLines.append(normalizedLine)
        if not normalizedLines:
            return fieldLabel
        firstLine = normalizedLines[0]
        if len(normalizedLines) == 1:
            return firstLine
        if NormalizeWhiteSpace(fieldLabel).lower() in firstLine.lower():
            return NormalizeWhitespaceLines(" ".join(normalizedLines))
        return "{0}: {1}".format(
            fieldLabel,
            NormalizeWhitespaceLines(" ".join(normalizedLines)),
        )

    def _BuildObservedQuantityFact(self, line: str) -> Optional[str]:
        normalizedLine = NormalizeWhiteSpace(line)
        normalizedLowerLine = normalizedLine.lower()
        if (
            OCR_OBSERVED_QUANTITY_NOISE_PATTERN.search(normalizedLowerLine)
            is not None
        ):
            return None
        if "%" not in normalizedLine:
            return None
        if len(normalizedLine) > 140:
            return None
        if OCR_PERCENT_VALUE_PATTERN.search(normalizedLine) is None:
            return None
        hasKoreanText = OCR_KOREAN_TEXT_PATTERN.search(normalizedLine) is not None
        hasMeaningfulLatinText = (
            OCR_MEANINGFUL_LATIN_TEXT_PATTERN.search(normalizedLine) is not None
        )
        if not hasKoreanText and not hasMeaningfulLatinText:
            return None
        return "함량/용량 정보: {0}".format(normalizedLine)

    def _TrimFieldNoiseFromLine(self, line: str) -> str:
        trimmedLine = NormalizeWhiteSpace(line)
        normalizedLine = trimmedLine.lower()
        positiveCutPositions = [
            match.start()
            for match in OCR_FIELD_TEXT_CUT_PATTERN.finditer(normalizedLine)
            if match.start() > 0
        ]
        if not positiveCutPositions:
            return trimmedLine
        return trimmedLine[: min(positiveCutPositions)].rstrip(" ,.;:/|·-")

    def _ContainsPlaceholderReference(self, normalizedText: str) -> bool:
        return PRODUCT_REFERENCE_PLACEHOLDER_PATTERN.search(normalizedText) is not None
