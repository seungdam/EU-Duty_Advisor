"""Ontology 기반 Stage 1 CN 후보 조회 helper."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    computed_field,
)

from bussiness_logic.bridge import (
    LlmGenerationOptions,
    LlmRequest,
    LlmResponse,
    LlmResponseFormat,
)
from bussiness_logic.core.context_retrieval.loader import OntologyDocumentLoader
from bussiness_logic.core.context_retrieval.schema import PackagedOntologyContext
from bussiness_logic.core.context_retrieval.semantic_retrieval import (
    CnSemanticCandidateIndex,
    CnSemanticSearchHit,
)
from bussiness_logic.utils import NormalizeWhiteSpace, NormalizeWhitespaceLines


EVIDENCE_TIER_PRIMARY = "primary"
EVIDENCE_TIER_SECONDARY = "secondary"
EVIDENCE_TIER_WEAK = "weak"
CN_LEAF_CODE_CARDS_DOCUMENT_ID = "table.cn_leaf_code_cards"
BTI_CASE_CHUNKS_DOCUMENT_ID = "table.bti_case_chunks"
FOOD_DOMAIN_SCOPE = "food_16_21"
COSMETICS_DOMAIN_SCOPE = "cosmetics_33"
DEFAULT_CN_CANDIDATE_TOP_K = 8
DEFAULT_SEMANTIC_CANDIDATE_TOP_K = 8
DEFAULT_STAGE1_BTI_EVIDENCE_PER_CANDIDATE = 3
DEFAULT_STAGE1_EVIDENCE_TEXT_MAX_CHARACTERS = 1400
DEFAULT_STAGE1_PROMPT_EVIDENCE_TEXT_MAX_CHARACTERS = 600
DEFAULT_STAGE1_PROMPT_COMMON_EVIDENCE_LIMIT = 6
DEFAULT_STAGE1_PROMPT_CANDIDATE_EVIDENCE_LIMIT = 3
DEFAULT_STAGE1_CLASSIFICATION_MAX_TOKENS = 4096
CN_TABLE_RELATIVE_PATH = Path("data/processed/cn_table.csv")
FOOD_DOMAIN_SCOPE_CHAPTERS = {"16", "17", "18", "19", "20", "21"}
COSMETICS_DOMAIN_SCOPE_CHAPTERS = {"33"}
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣]+")
STAGE1_PROMPT_COMMON_EVIDENCE_TYPE_PRIORITY = [
    "product_fact",
    "product_notice_field",
    "product_notice_text",
    "ocr_fact",
]
STAGE1_PROMPT_CANDIDATE_EVIDENCE_TYPE_PRIORITY = [
    "bti_case_chunk",
    "cn_candidate_card",
]
STAGE1_PROMPT_EXCLUDED_FALLBACK_EVIDENCE_TYPES = {
    "ocr_raw_reference",
    "ocr_text",
    "ontology_chunk",
}
STAGE1_CLASSIFICATION_ALLOWED_STATUSES = {
    "strong_candidate",
    "possible_candidate",
    "unlikely_candidate",
    "insufficient_information",
}
STAGE1_CLASSIFICATION_PATH_LEVELS = ["hs2", "hs4", "hs6", "cn8"]
STAGE1_CLASSIFICATION_PATH_ALLOWED_CONSISTENCIES = {
    "consistent",
    "conflicting",
    "needs_review",
}
FINAL_DETERMINATION_WARNING_TERMS = [
    "final determination",
    "definitive classification",
    "legally determined",
    "must be classified",
    "확정 코드",
    "최종 코드",
    "최종 분류",
    "법적 판단",
]
CANDIDATE_COVERAGE_REFERENCE_PATTERN = re.compile(r"\d[\d.\s]{1,14}\d")
CANDIDATE_COVERAGE_TERMS = [
    "후보 카드",
    "후보 정보",
    "후보가",
    "candidate card",
    "candidate information",
]
CANDIDATE_COVERAGE_MISSING_TERMS = [
    "제공되지",
    "누락",
    "부재",
    "정보 없음",
    "정보가 없음",
    "카드 없음",
    "missing",
    "not provided",
    "unavailable",
]
LOW_VALUE_MATCH_TERMS = {
    "and",
    "animal",
    "any",
    "at",
    "blood",
    "by",
    "containing",
    "cosmetic",
    "cosmetics",
    "crustaceans",
    "fish",
    "food",
    "for",
    "frozen",
    "heat",
    "in",
    "insects",
    "meat",
    "molluscs",
    "offal",
    "of",
    "or",
    "other",
    "pre",
    "preparation",
    "preparations",
    "prepared",
    "preserved",
    "ready",
    "toilet",
    "the",
    "to",
    "weight",
    "with",
}
WEAK_SUPPLEMENTAL_FACT_MARKERS = {
    "풍미",
    "향분말",
    "향료",
    "함유",
    "주의사항",
}
EXCLUDED_CLASSIFICATION_FACT_MARKERS = {
    "혼입",
    "혼입가능",
    "혼입 가능",
    "같은 제조시설",
    "같은 제조 시설",
    "제조시설에서 제조",
    "사용한 제품과 같은",
    "알레르기",
    "allergen",
    "allergy",
    "may contain",
    "same facility",
    "same manufacturing",
}
PREFERRED_HEADING_HINTS = {
    "라면": ["1902"],
    "면류": ["1902"],
    "유탕면": ["1902"],
    "noodle": ["1902"],
    "noodles": ["1902"],
    "pasta": ["1902"],
    "주꾸미": ["1605"],
    "새우": ["1605"],
    "shrimp": ["1605"],
    "클렌저": ["3304"],
    "세럼": ["3304"],
    "화장품": ["3304"],
}
PRODUCT_DOMAIN_SCOPE_MAP = {
    "food": [FOOD_DOMAIN_SCOPE],
    "cosmetics": [COSMETICS_DOMAIN_SCOPE],
    "ambiguous": [FOOD_DOMAIN_SCOPE, COSMETICS_DOMAIN_SCOPE],
    "unknown": [FOOD_DOMAIN_SCOPE, COSMETICS_DOMAIN_SCOPE],
}

TERM_EXPANSION_MAP = {
    "갈비": ["ribs", "cuts", "swine", "pork"],
    "고기": ["meat"],
    "돼지": ["pork", "swine", "domestic swine", "meat"],
    "돼지고기": ["pork", "swine", "domestic swine", "meat"],
    "쇠고기": ["beef", "meat"],
    "소고기": ["beef", "meat"],
    "닭": ["chicken", "meat"],
    "닭고기": ["chicken", "meat"],
    "양념육": ["prepared meat", "preserved meat", "meat"],
    "생선": ["fish"],
    "어류": ["fish"],
    "수산물": ["fish", "crustaceans", "molluscs", "aquatic invertebrates"],
    "수산물가공품": [
        "preparations of fish",
        "prepared fish",
        "prepared molluscs",
        "prepared or preserved molluscs",
        "aquatic invertebrates",
    ],
    "기타수산물가공품": [
        "preparations of fish",
        "prepared fish",
        "prepared molluscs",
        "prepared or preserved molluscs",
        "aquatic invertebrates",
    ],
    "새우": ["shrimp", "crustaceans"],
    "주꾸미": ["octopus", "molluscs", "aquatic invertebrates", "prepared", "preserved"],
    "낙지": ["octopus", "molluscs", "aquatic invertebrates", "prepared", "preserved"],
    "문어": ["octopus", "molluscs", "aquatic invertebrates"],
    "오징어": ["squid", "cuttlefish", "molluscs", "aquatic invertebrates"],
    "게": ["crab", "crustaceans"],
    "게살": ["crab", "crustaceans"],
    "조개": ["molluscs"],
    "국수": ["noodle", "pasta"],
    "라면": ["noodle", "pasta"],
    "면류": ["noodle", "pasta"],
    "막국수": ["noodle", "buckwheat", "cereal"],
    "메밀": ["buckwheat", "cereal"],
    "국": ["soups"],
    "탕": ["soups"],
    "소스": ["sauce"],
    "초콜릿": ["chocolate"],
    "캔디": ["sugar", "confectionery"],
    "클렌저": ["cleanser", "skin", "toilet preparation"],
    "세안": ["cleanser", "skin", "toilet preparation"],
    "샴푸": ["shampoo", "hair"],
    "립스틱": ["lip", "make-up"],
    "향수": ["perfume", "fragrance"],
    "치약": ["oral", "dental"],
    "화장품": ["cosmetic", "toilet preparation"],
}
TIER_INCLUDE_RULE_WEIGHTS = {
    EVIDENCE_TIER_PRIMARY: 6.0,
    EVIDENCE_TIER_SECONDARY: 3.0,
    EVIDENCE_TIER_WEAK: 0.4,
}
TIER_SEARCH_KEYWORD_WEIGHTS = {
    EVIDENCE_TIER_PRIMARY: 4.0,
    EVIDENCE_TIER_SECONDARY: 2.0,
    EVIDENCE_TIER_WEAK: 0.25,
}
TIER_DESCRIPTION_WEIGHTS = {
    EVIDENCE_TIER_PRIMARY: 2.0,
    EVIDENCE_TIER_SECONDARY: 1.0,
    EVIDENCE_TIER_WEAK: 0.1,
}
DEFAULT_MAX_CANDIDATES_PER_HS4 = 3
DEFAULT_INITIAL_HS4_BRANCH_REPRESENTATIVE_LIMIT = 3
DEFAULT_INITIAL_HS6_BRANCH_REPRESENTATIVE_LIMIT = 5
RETRIEVAL_SOURCE_HEURISTIC = "heuristic"
RETRIEVAL_SOURCE_SEMANTIC = "semantic"

STAGE1_CLASSIFICATION_SYSTEM_PROMPT = """\
You are an EU HS/CN classification review assistant for Korean exporters.
Use the supplied core context, normalized product facts, and CN candidate cards.
Do not issue a final legal/customs determination.
Review each candidate against the product facts and explain whether it is plausible.
Separate evidence, assumptions, missing information, and reasons to reject candidates.
Always keep human review required.
Return only a JSON object.
"""

STAGE1_CLASSIFICATION_JSON_INSTRUCTIONS = {
    "classification_result": {
        "product_name": "string",
        "product_domain": "food|cosmetics|ambiguous|unknown",
        "domain_scopes": ["string"],
        "candidate_reviews": [
            {
                "hs8": "string",
                "hs6_code": "string|null",
                "status": (
                    "strong_candidate|possible_candidate|unlikely_candidate|"
                    "insufficient_information"
                ),
                "supporting_product_facts": ["string"],
                "conflicting_or_exclusion_facts": ["string"],
                "missing_information": ["string"],
                "evidence_refs": ["string"],
                "classification_path_review": {
                    "hs2": {
                        "code": "string|null",
                        "consistency": "consistent|conflicting|needs_review",
                        "comment": "string",
                    },
                    "hs4": {
                        "code": "string|null",
                        "consistency": "consistent|conflicting|needs_review",
                        "comment": "string",
                    },
                    "hs6": {
                        "code": "string|null",
                        "consistency": "consistent|conflicting|needs_review",
                        "comment": "string",
                    },
                    "cn8": {
                        "code": "string|null",
                        "consistency": "consistent|conflicting|needs_review",
                        "comment": "string",
                    },
                },
                "classification_rule_review": {
                    "include_rule_comment": "string",
                    "exclude_rule_comment": "string",
                    "hard_condition_comment": "string",
                },
                "similar_ebti_cases": [
                    {
                        "evidence_ref": "string",
                        "similarity_comment": "string",
                        "difference_comment": "string",
                    }
                ],
                "reason": "string",
                "human_review_required": True,
            }
        ],
        "not_enough_information": ["string"],
        "recommended_next_action": "string",
        "human_review_warning": "string",
    }
}


class ProductClassificationInput(BaseModel):
    """상품 수집 결과를 HS6/CN8 후보 조회에 맞게 정규화한 입력."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    productPageUrl: Optional[str] = Field(default=None, alias="product_page_url")
    productName: Optional[str] = Field(default=None, alias="product_name")
    productDomain: str = Field(default="unknown", alias="product_domain")
    domainScopes: List[str] = Field(default_factory=list, alias="domain_scopes")
    shortDescription: Optional[str] = Field(default=None, alias="short_description")
    brandName: Optional[str] = Field(default=None, alias="brand_name")
    packageType: Optional[str] = Field(default=None, alias="package_type")
    saleUnit: Optional[str] = Field(default=None, alias="sale_unit")
    noticeFieldTexts: List[str] = Field(
        default_factory=list,
        alias="notice_field_texts",
    )
    noticeOptionNames: List[str] = Field(
        default_factory=list,
        alias="notice_option_names",
    )
    productNoticeText: str = Field(
        default="",
        alias="product_notice_text",
        exclude=True,
    )
    normalizedOcrFactTexts: List[str] = Field(
        default_factory=list,
        alias="normalized_ocr_fact_texts",
    )
    structuredProductFacts: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias="structured_product_facts",
    )
    unresolvedProductFacts: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias="unresolved_product_facts",
    )
    productFactConflicts: List[Any] = Field(
        default_factory=list,
        alias="product_fact_conflicts",
    )
    excludedOcrTextPreview: str = Field(
        default="",
        alias="excluded_ocr_text_preview",
        exclude=True,
    )
    ocrText: str = Field(default="", alias="ocr_text", exclude=True)

    def BuildPrimarySearchText(self) -> str:
        return self._BuildSearchTextFromParts(
            [
                self.productName or "",
                self.shortDescription or "",
                self.brandName or "",
            ]
        )

    def BuildSecondarySearchText(self) -> str:
        secondaryOcrFactTexts = [
            factText
            for factText in self.normalizedOcrFactTexts
            if self._ShouldUseAsSecondaryClassificationFactText(factText)
        ]
        secondaryNoticeTexts = [
            noticeText
            for noticeText in self._SplitSupplementalFactTexts(
                self.productNoticeText,
            )
            if self._ShouldUseAsSecondaryClassificationFactText(noticeText)
        ]
        return self._BuildSearchTextFromParts(
            [
                self.packageType or "",
                self.saleUnit or "",
                *self.noticeOptionNames,
                *self.noticeFieldTexts,
                *secondaryNoticeTexts,
                *secondaryOcrFactTexts,
            ]
        )

    def BuildWeakSearchText(self) -> str:
        weakOcrFactTexts = [
            factText
            for factText in self.normalizedOcrFactTexts
            if self._ShouldUseAsWeakClassificationFactText(factText)
        ]
        weakNoticeTexts = [
            noticeText
            for noticeText in self._SplitSupplementalFactTexts(
                self.productNoticeText,
            )
            if self._ShouldUseAsWeakClassificationFactText(noticeText)
        ]
        return self._BuildSearchTextFromParts(
            [
                *weakNoticeTexts,
                *weakOcrFactTexts,
            ]
        )

    def BuildSearchText(self) -> str:
        rawParts = [
            self.BuildPrimarySearchText(),
            self.BuildSecondarySearchText(),
            self.BuildWeakSearchText()
            or (
                ""
                if self.normalizedOcrFactTexts
                else self._BuildRawOcrClassificationFallbackText()
            ),
        ]
        return self._BuildSearchTextFromParts(rawParts)

    def BuildSemanticSearchText(self) -> str:
        semanticText = self._BuildSearchTextFromParts(
            [
                self.BuildPrimarySearchText(),
                self.BuildSecondarySearchText(),
            ]
        )
        return semanticText or self.BuildSearchText()

    def _BuildSearchTextFromParts(self, rawParts: Sequence[str]) -> str:
        parts = [
            part
            for part in rawParts
            if isinstance(part, str) and part.strip() != ""
        ]
        return NormalizeWhitespaceLines("\n".join(parts))

    def _SplitSupplementalFactTexts(self, text: str) -> List[str]:
        normalizedText = NormalizeWhitespaceLines(text)
        return [
            line
            for line in normalizedText.splitlines()
            if line.strip() != ""
        ]

    def _BuildRawOcrClassificationFallbackText(self) -> str:
        return self._BuildSearchTextFromParts(
            [
                line
                for line in self._SplitSupplementalFactTexts(self.ocrText)
                if not self._IsExcludedClassificationFactText(line)
            ]
        )

    def _ShouldUseAsSecondaryClassificationFactText(self, factText: str) -> bool:
        return (
            not self._IsExcludedClassificationFactText(factText)
            and not self._IsWeakSupplementalFactText(factText)
        )

    def _ShouldUseAsWeakClassificationFactText(self, factText: str) -> bool:
        return (
            not self._IsExcludedClassificationFactText(factText)
            and self._IsWeakSupplementalFactText(factText)
        )

    def _IsExcludedClassificationFactText(self, factText: str) -> bool:
        normalizedText = NormalizeWhiteSpace(factText).lower()
        return any(
            marker in normalizedText
            for marker in EXCLUDED_CLASSIFICATION_FACT_MARKERS
        )

    def _IsWeakSupplementalFactText(self, factText: str) -> bool:
        normalizedText = NormalizeWhiteSpace(factText).lower()
        return any(
            marker in normalizedText
            for marker in WEAK_SUPPLEMENTAL_FACT_MARKERS
        )

    @computed_field(alias="product_notice_text_length")
    @property
    def productNoticeTextLength(self) -> int:
        return len(self.productNoticeText)

    @computed_field(alias="ocr_text_length")
    @property
    def ocrTextLength(self) -> int:
        return len(self.ocrText)

    @computed_field(alias="normalized_ocr_fact_count")
    @property
    def normalizedOcrFactCount(self) -> int:
        return len(self.normalizedOcrFactTexts)

    @computed_field(alias="search_text_length")
    @property
    def searchTextLength(self) -> int:
        return len(self.BuildSearchText())


class CnCandidatePromptPayload(BaseModel):
    """LLM 후보 검토 요청에 포함할 CN 후보 카드 payload."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    hs8: str
    hs6Code: Optional[str] = Field(default=None, alias="hs6_code")
    domainScope: str = Field(alias="domain_scope")
    score: float
    retrievalSources: List[str] = Field(
        default_factory=list,
        alias="retrieval_sources",
    )
    semanticScore: Optional[float] = Field(default=None, alias="semantic_score")
    semanticMatches: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias="semantic_matches",
    )
    codeHierarchy: Dict[str, Dict[str, Optional[str]]] = Field(
        alias="code_hierarchy",
    )
    hierarchyPathText: str = Field(alias="hierarchy_path_text")
    combinedDescription: str = Field(alias="combined_description")
    intermediateBranchContext: str = Field(
        default="",
        alias="intermediate_branch_context",
    )
    candidateContextText: str = Field(default="", alias="candidate_context_text")
    cn8LeafDescription: Optional[str] = Field(
        default=None,
        alias="cn8_leaf_description",
    )
    contextWarning: str = Field(alias="context_warning")
    classificationRuleTexts: Dict[str, str] = Field(
        alias="classification_rule_texts",
    )
    rankingEvidence: Dict[str, List[str]] = Field(alias="ranking_evidence")
    cnExplanatoryNote: str = Field(default="", alias="cn_explanatory_note")


class CnCandidate(BaseModel):
    """Stage 1에서 LLM/human review로 넘길 CN8 후보 카드."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    hs8: str
    domainScope: str = Field(alias="domain_scope")
    score: float
    matchedTerms: List[str] = Field(default_factory=list, alias="matched_terms")
    excludedTerms: List[str] = Field(default_factory=list, alias="excluded_terms")
    includeRuleMatches: List[str] = Field(
        default_factory=list,
        alias="include_rule_matches",
    )
    searchKeywordMatches: List[str] = Field(
        default_factory=list,
        alias="search_keyword_matches",
    )
    descriptionMatches: List[str] = Field(
        default_factory=list,
        alias="description_matches",
    )
    excludeRuleMatches: List[str] = Field(
        default_factory=list,
        alias="exclude_rule_matches",
    )
    primaryEvidenceMatches: List[str] = Field(
        default_factory=list,
        alias="primary_evidence_matches",
    )
    secondaryEvidenceMatches: List[str] = Field(
        default_factory=list,
        alias="secondary_evidence_matches",
    )
    weakEvidenceMatches: List[str] = Field(
        default_factory=list,
        alias="weak_evidence_matches",
    )
    includeRulePoints: float = Field(
        default=0.0,
        alias="include_rule_points",
        exclude=True,
    )
    searchKeywordPoints: float = Field(
        default=0.0,
        alias="search_keyword_points",
        exclude=True,
    )
    descriptionPoints: float = Field(
        default=0.0,
        alias="description_points",
        exclude=True,
    )
    hierarchyLevelPoints: Dict[str, float] = Field(
        default_factory=dict,
        alias="hierarchy_level_points",
        exclude=True,
    )
    hierarchyLevelMatches: Dict[str, List[str]] = Field(
        default_factory=dict,
        alias="hierarchy_level_matches",
        exclude=True,
    )
    hs2Code: Optional[str] = Field(default=None, alias="hs2_code", exclude=True)
    hs2Description: Optional[str] = Field(
        default=None,
        alias="hs2_description",
        exclude=True,
    )
    hs4Code: Optional[str] = Field(default=None, alias="hs4_code", exclude=True)
    hs4Description: Optional[str] = Field(
        default=None,
        alias="hs4_description",
        exclude=True,
    )
    hs6Code: Optional[str] = Field(default=None, alias="hs6_code", exclude=True)
    hs6Description: Optional[str] = Field(
        default=None,
        alias="hs6_description",
        exclude=True,
    )
    hs8Code: Optional[str] = Field(default=None, alias="hs8_code", exclude=True)
    hs8Description: Optional[str] = Field(
        default=None,
        alias="hs8_description",
        exclude=True,
    )
    branchContext: str = Field(
        default="",
        alias="intermediate_branch_context",
        exclude=True,
    )
    candidateContextText: str = Field(
        default="",
        alias="candidate_context_text",
    )
    combinedDescription: str = Field(default="", alias="combined_description")
    includeRuleKeywords: str = Field(
        default="",
        alias="include_rule_keywords",
        exclude=True,
    )
    excludeRuleKeywords: str = Field(
        default="",
        alias="exclude_rule_keywords",
        exclude=True,
    )
    hardConditions: str = Field(default="", alias="hard_conditions", exclude=True)
    cnExplanatoryNote: str = Field(default="", alias="cn_explanatory_note")
    needsHumanReview: bool = Field(default=True, alias="needs_human_review")
    retrievalSources: List[str] = Field(
        default_factory=lambda: [RETRIEVAL_SOURCE_HEURISTIC],
        alias="retrieval_sources",
    )
    semanticScore: Optional[float] = Field(default=None, alias="semantic_score")
    semanticMatches: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias="semantic_matches",
    )

    @computed_field(alias="code_hierarchy")
    @property
    def codeHierarchy(self) -> Dict[str, Dict[str, Optional[str]]]:
        return {
            "hs2": {
                "code": self.hs2Code,
                "description": self.hs2Description,
            },
            "hs4": {
                "code": self.hs4Code,
                "description": self.hs4Description,
            },
            "hs6": {
                "code": self.hs6Code,
                "description": self.hs6Description,
            },
            "cn8": {
                "code": self.hs8Code or self.hs8,
                "description": self.hs8Description,
            },
        }

    @computed_field(alias="score_breakdown")
    @property
    def scoreBreakdown(self) -> Dict[str, Any]:
        return {
            "include_rule_points": self.includeRulePoints,
            "search_keyword_points": self.searchKeywordPoints,
            "description_points": self.descriptionPoints,
            "hierarchy_level_points": dict(self.hierarchyLevelPoints),
            "hierarchy_level_matches": {
                level: list(matches[:8])
                for level, matches in self.hierarchyLevelMatches.items()
            },
            "primary_evidence_matches": list(self.primaryEvidenceMatches[:8]),
            "secondary_evidence_matches": list(self.secondaryEvidenceMatches[:8]),
            "weak_evidence_matches": list(self.weakEvidenceMatches[:8]),
            "exclude_rule_triggered": len(self.excludeRuleMatches) > 0,
            "retrieval_sources": list(self.retrievalSources),
            "semantic_score": self.semanticScore,
            "semantic_matches": list(self.semanticMatches[:3]),
            "formula": (
                "tiered include/search/description matches; weak OCR evidence "
                "has low weight; exclude_rule match forces score 0"
            ),
        }

    @computed_field(alias="classification_rule_texts")
    @property
    def classificationRuleTexts(self) -> Dict[str, str]:
        return {
            "include_rule_keywords": self.includeRuleKeywords,
            "exclude_rule_keywords": self.excludeRuleKeywords,
            "hard_conditions": self.hardConditions,
        }

    def ToPromptDict(self) -> Dict[str, Any]:
        return self.ToPromptPayload().model_dump(mode="json", by_alias=True)

    def ToPromptPayload(self) -> CnCandidatePromptPayload:
        codeHierarchy = self.codeHierarchy
        hierarchyPathParts: List[str] = []
        for level in ["hs2", "hs4", "hs6", "cn8"]:
            levelData = codeHierarchy.get(level)
            if not isinstance(levelData, Mapping):
                continue
            code = levelData.get("code")
            description = NormalizeWhiteSpace(str(levelData.get("description") or ""))
            if isinstance(code, str) and code.strip() and description:
                hierarchyPathParts.append("{0}: {1}".format(code, description))
            elif isinstance(code, str) and code.strip():
                hierarchyPathParts.append(code)
        return CnCandidatePromptPayload(
            hs8=self.hs8,
            hs6Code=self.hs6Code,
            domainScope=self.domainScope,
            score=self.score,
            retrievalSources=list(self.retrievalSources),
            semanticScore=self.semanticScore,
            semanticMatches=list(self.semanticMatches[:3]),
            codeHierarchy=codeHierarchy,
            hierarchyPathText=" > ".join(hierarchyPathParts),
            combinedDescription=self.combinedDescription,
            intermediateBranchContext=self.branchContext,
            candidateContextText=self.candidateContextText,
            cn8LeafDescription=self.hs8Description,
            contextWarning=(
                "intermediate_branch_context is a variable-depth branch context "
                "and is intentionally separated from hierarchy_path_text."
            ),
            classificationRuleTexts=self.classificationRuleTexts,
            rankingEvidence={
                "include_rule_matches": list(self.includeRuleMatches[:8]),
                "search_keyword_matches": list(self.searchKeywordMatches[:8]),
                "description_matches": list(self.descriptionMatches[:8]),
                "exclude_rule_matches": list(self.excludeRuleMatches[:8]),
                "primary_evidence_matches": list(self.primaryEvidenceMatches[:8]),
                "secondary_evidence_matches": list(self.secondaryEvidenceMatches[:8]),
                "weak_evidence_matches": list(self.weakEvidenceMatches[:8]),
            },
            cnExplanatoryNote=self.cnExplanatoryNote,
        )


class Stage1EvidenceRecord(BaseModel):
    """Stage 1 후보 검토에서 LLM이 인용할 수 있는 단일 근거."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    evidenceId: str = Field(alias="evidence_id")
    evidenceType: str = Field(alias="evidence_type")
    sourceName: str = Field(alias="source_name")
    sourceRef: str = Field(alias="source_ref")
    text: str
    candidateHs8: Optional[str] = Field(default=None, alias="candidate_hs8")
    candidateHs6: Optional[str] = Field(default=None, alias="candidate_hs6")
    legalStatus: str = Field(default="internal_reference", alias="legal_status")
    limitations: List[str] = Field(default_factory=list)


class Stage1EvidencePackage(BaseModel):
    """Stage 1 LLM request와 validator가 공유할 근거 묶음."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    evidenceRecords: List[Stage1EvidenceRecord] = Field(
        default_factory=list,
        alias="evidence_records",
    )
    commonEvidenceIds: List[str] = Field(
        default_factory=list,
        alias="common_evidence_ids",
    )
    candidateEvidenceIds: Dict[str, List[str]] = Field(
        default_factory=dict,
        alias="candidate_evidence_ids",
    )
    ontologyContextSummary: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias="ontology_context_summary",
    )

    @property
    def validEvidenceIds(self) -> Set[str]:
        return {
            evidenceRecord.evidenceId
            for evidenceRecord in self.evidenceRecords
        }

    def ToPromptDict(
        self,
        candidateCodes: Optional[Sequence[str]] = None,
        maxTextCharacters: int = DEFAULT_STAGE1_PROMPT_EVIDENCE_TEXT_MAX_CHARACTERS,
        commonEvidenceLimit: int = DEFAULT_STAGE1_PROMPT_COMMON_EVIDENCE_LIMIT,
        candidateEvidenceLimit: int = DEFAULT_STAGE1_PROMPT_CANDIDATE_EVIDENCE_LIMIT,
    ) -> Dict[str, Any]:
        def TrimText(text: str) -> str:
            if len(text) <= maxTextCharacters:
                return text
            return text[:maxTextCharacters].rstrip() + "..."

        def AppendSelectedId(selectedIds: List[str], evidenceId: str) -> None:
            if evidenceId not in selectedIds:
                selectedIds.append(evidenceId)

        def SelectByTypePriority(
            records: Sequence[Stage1EvidenceRecord],
            typePriority: Sequence[str],
            limit: int,
        ) -> List[str]:
            selectedIds: List[str] = []
            if limit <= 0:
                return selectedIds
            for evidenceType in typePriority:
                for record in records:
                    if len(selectedIds) >= limit:
                        return selectedIds
                    if record.evidenceType == evidenceType:
                        AppendSelectedId(selectedIds, record.evidenceId)
            for record in records:
                if len(selectedIds) >= limit:
                    return selectedIds
                if record.evidenceType in STAGE1_PROMPT_EXCLUDED_FALLBACK_EVIDENCE_TYPES:
                    continue
                AppendSelectedId(selectedIds, record.evidenceId)
            return selectedIds

        promptCandidateCodes = (
            list(candidateCodes)
            if candidateCodes is not None
            else list(self.candidateEvidenceIds.keys())
        )
        commonEvidenceIdSet = set(self.commonEvidenceIds)
        commonRecords = [
            evidenceRecord
            for evidenceRecord in self.evidenceRecords
            if evidenceRecord.evidenceId in commonEvidenceIdSet
        ]
        selectedCommonEvidenceIds = SelectByTypePriority(
            commonRecords,
            STAGE1_PROMPT_COMMON_EVIDENCE_TYPE_PRIORITY,
            commonEvidenceLimit,
        )
        selectedCandidateEvidenceIdsByCode: Dict[str, List[str]] = {}
        selectedEvidenceIds = list(selectedCommonEvidenceIds)

        for candidateCode in promptCandidateCodes:
            candidateEvidenceIdSet = set(
                self.candidateEvidenceIds.get(candidateCode, []),
            )
            candidateRecords = [
                evidenceRecord
                for evidenceRecord in self.evidenceRecords
                if evidenceRecord.candidateHs8 == candidateCode
                and evidenceRecord.evidenceId in candidateEvidenceIdSet
            ]
            selectedCandidateEvidenceIds = SelectByTypePriority(
                candidateRecords,
                STAGE1_PROMPT_CANDIDATE_EVIDENCE_TYPE_PRIORITY,
                candidateEvidenceLimit,
            )
            selectedCandidateEvidenceIdsByCode[candidateCode] = (
                selectedCandidateEvidenceIds
            )
            for evidenceId in selectedCandidateEvidenceIds:
                AppendSelectedId(selectedEvidenceIds, evidenceId)

        selectedEvidenceIdSet = set(selectedEvidenceIds)
        selectedEvidenceRecords = [
            evidenceRecord
            for evidenceRecord in self.evidenceRecords
            if evidenceRecord.evidenceId in selectedEvidenceIdSet
        ]

        def BuildPromptEvidenceRecord(
            evidenceRecord: Stage1EvidenceRecord,
        ) -> Dict[str, Any]:
            evidenceData = evidenceRecord.model_dump(mode="json", by_alias=True)
            evidenceText = evidenceRecord.text
            if evidenceRecord.evidenceType == "cn_candidate_card":
                evidenceText = (
                    "Candidate details are provided in "
                    "[stage1_cn_candidate_cards]. Use this evidence_id only "
                    "when citing the CN candidate card itself."
                )
            evidenceData["text"] = TrimText(evidenceText)
            return evidenceData

        return {
            "candidate_citation_requirements": [
                {
                    "hs8": candidateCode,
                    "must_include_one_of": list(selectedCandidateEvidenceIds),
                }
                for candidateCode, selectedCandidateEvidenceIds in (
                    selectedCandidateEvidenceIdsByCode.items()
                )
            ],
            "evidence_records": [
                BuildPromptEvidenceRecord(evidenceRecord)
                for evidenceRecord in selectedEvidenceRecords
            ],
            "common_evidence_ids": list(selectedCommonEvidenceIds),
            "candidate_evidence_ids": {
                candidateCode: [
                    *selectedCommonEvidenceIds,
                    *selectedCandidateEvidenceIds,
                ]
                for candidateCode, selectedCandidateEvidenceIds in (
                    selectedCandidateEvidenceIdsByCode.items()
                )
            },
            "valid_evidence_ids": sorted(selectedEvidenceIdSet),
            "ontology_context_summary": list(self.ontologyContextSummary),
            "omitted_evidence_record_count": (
                len(self.evidenceRecords) - len(selectedEvidenceRecords)
            ),
        }


class Stage1EvidencePackageBuilder:
    """Stage 1 후보 검토에 필요한 product/CN/core/BTI 근거를 묶는다."""

    def __init__(
        self,
        ontologyRootPath: str | Path,
        projectRootPath: Optional[str | Path] = None,
        maxBtiEvidencePerCandidate: int = DEFAULT_STAGE1_BTI_EVIDENCE_PER_CANDIDATE,
        maxEvidenceTextCharacters: int = DEFAULT_STAGE1_EVIDENCE_TEXT_MAX_CHARACTERS,
    ) -> None:
        self.ontologyRootPath = Path(ontologyRootPath)
        self.projectRootPath = (
            Path(projectRootPath)
            if projectRootPath is not None
            else self.ontologyRootPath.parent
        )
        self.maxBtiEvidencePerCandidate = max(0, maxBtiEvidencePerCandidate)
        self.maxEvidenceTextCharacters = max(200, maxEvidenceTextCharacters)

    def Build(
        self,
        productInput: ProductClassificationInput,
        candidates: Sequence[CnCandidate],
        packagedContext: Optional[PackagedOntologyContext] = None,
        includeOntologyEvidence: bool = False,
    ) -> Stage1EvidencePackage:
        evidenceRecords: List[Stage1EvidenceRecord] = []
        commonEvidenceIds: List[str] = []
        candidateEvidenceIds: Dict[str, List[str]] = {
            candidate.hs8: []
            for candidate in candidates
        }
        ontologyContextSummary = (
            self._BuildOntologyContextSummary(packagedContext)
            if packagedContext is not None
            else []
        )

        productEvidenceRecords = self._BuildProductEvidenceRecords(productInput)
        evidenceRecords.extend(productEvidenceRecords)
        commonEvidenceIds.extend(
            evidenceRecord.evidenceId
            for evidenceRecord in productEvidenceRecords
        )

        if packagedContext is not None and includeOntologyEvidence:
            ontologyEvidenceRecords = self._BuildOntologyEvidenceRecords(
                packagedContext,
            )
            evidenceRecords.extend(ontologyEvidenceRecords)
            commonEvidenceIds.extend(
                evidenceRecord.evidenceId
                for evidenceRecord in ontologyEvidenceRecords
            )

        btiRowsByCandidate = self._BuildBtiRowsByCandidate(candidates)
        for candidate in candidates:
            candidateRecords = self._BuildCandidateEvidenceRecords(
                candidate,
                btiRowsByCandidate.get(candidate.hs8, []),
            )
            evidenceRecords.extend(candidateRecords)
            candidateEvidenceIds[candidate.hs8] = [
                *commonEvidenceIds,
                *[
                    evidenceRecord.evidenceId
                    for evidenceRecord in candidateRecords
                ],
            ]

        return Stage1EvidencePackage(
            evidenceRecords=evidenceRecords,
            commonEvidenceIds=commonEvidenceIds,
            candidateEvidenceIds=candidateEvidenceIds,
            ontologyContextSummary=ontologyContextSummary,
        )

    def _BuildProductEvidenceRecords(
        self,
        productInput: ProductClassificationInput,
    ) -> List[Stage1EvidenceRecord]:
        records: List[Stage1EvidenceRecord] = []
        productSummaryParts = [
            "product_name: {0}".format(productInput.productName or "unknown"),
            "product_domain: {0}".format(productInput.productDomain),
            "domain_scopes: {0}".format(", ".join(productInput.domainScopes)),
            "short_description: {0}".format(productInput.shortDescription or ""),
            "brand_name: {0}".format(productInput.brandName or ""),
            "package_type: {0}".format(productInput.packageType or ""),
            "sale_unit: {0}".format(productInput.saleUnit or ""),
        ]
        records.append(
            Stage1EvidenceRecord(
                evidenceId="product_fact:summary",
                evidenceType="product_fact",
                sourceName="product_classification_input",
                sourceRef=productInput.productPageUrl or "product_input",
                text=self._TrimEvidenceText("\n".join(productSummaryParts)),
                legalStatus="discovery",
                limitations=[
                    "Product facts are extracted inputs for review, not official classification evidence.",
                ],
            )
        )

        for index, noticeFieldText in enumerate(productInput.noticeFieldTexts, start=1):
            records.append(
                Stage1EvidenceRecord(
                    evidenceId="product_fact:notice_field:{0}".format(index),
                    evidenceType="product_notice_field",
                    sourceName="product_notice_information",
                    sourceRef=productInput.productPageUrl or "product_input",
                    text=self._TrimEvidenceText(noticeFieldText),
                    legalStatus="discovery",
                    limitations=[
                        "Product notice text may require human verification against the source page.",
                    ],
                )
            )

        if productInput.productNoticeText.strip():
            records.append(
                Stage1EvidenceRecord(
                    evidenceId="product_fact:notice_text",
                    evidenceType="product_notice_text",
                    sourceName="product_notice_information",
                    sourceRef=productInput.productPageUrl or "product_input",
                    text=self._TrimEvidenceText(productInput.productNoticeText),
                    legalStatus="discovery",
                    limitations=[
                        "Product notice text is source evidence for review, not final classification proof.",
                    ],
                )
            )

        if productInput.normalizedOcrFactTexts:
            records.append(
                Stage1EvidenceRecord(
                    evidenceId="product_fact:ocr_normalized_facts",
                    evidenceType="ocr_fact",
                    sourceName="product_ocr_normalized_facts",
                    sourceRef=productInput.productPageUrl or "product_input",
                    text=self._TrimEvidenceText(
                        NormalizeWhitespaceLines(
                            "\n".join(productInput.normalizedOcrFactTexts),
                        ),
                    ),
                    legalStatus="discovery",
                    limitations=[
                        "OCR facts are normalized from image text and may contain recognition errors.",
                    ],
                )
            )

        if productInput.ocrText.strip():
            records.append(
                Stage1EvidenceRecord(
                    evidenceId="product_fact:ocr_raw_reference",
                    evidenceType="ocr_raw_reference",
                    sourceName="product_ocr_fallback",
                    sourceRef=productInput.productPageUrl or "product_input",
                    text=self._TrimEvidenceText(productInput.ocrText),
                    legalStatus="discovery",
                    limitations=[
                        "OCR text may contain recognition errors and must be reviewed.",
                    ],
                )
            )

        return records

    def _BuildOntologyContextSummary(
        self,
        packagedContext: PackagedOntologyContext,
    ) -> List[Dict[str, Any]]:
        summaries: List[Dict[str, Any]] = []
        for selectedResult in packagedContext.selectedResults:
            chunk = selectedResult.chunk
            summaries.append(
                {
                    "chunk_id": chunk.chunkId,
                    "document_id": chunk.documentId,
                    "relative_path": chunk.relativePath,
                    "heading_path": list(chunk.headingPath),
                    "score": selectedResult.score,
                    "matched_terms": list(selectedResult.matchedTerms),
                    "chunk_kind": chunk.metadata.get("chunk_kind"),
                }
            )
        return summaries

    def _BuildOntologyEvidenceRecords(
        self,
        packagedContext: PackagedOntologyContext,
    ) -> List[Stage1EvidenceRecord]:
        records: List[Stage1EvidenceRecord] = []
        seenEvidenceIds: Set[str] = set()
        for selectedResult in packagedContext.selectedResults:
            chunk = selectedResult.chunk
            evidenceId = "ontology_chunk:{0}".format(chunk.chunkId)
            if evidenceId in seenEvidenceIds:
                continue
            seenEvidenceIds.add(evidenceId)
            records.append(
                Stage1EvidenceRecord(
                    evidenceId=evidenceId,
                    evidenceType="ontology_chunk",
                    sourceName=chunk.documentId,
                    sourceRef=chunk.relativePath,
                    text=self._TrimEvidenceText(chunk.ToContextText()),
                    legalStatus="internal_reference",
                    limitations=[
                        "Ontology chunk guides reasoning but does not replace official classification review.",
                    ],
                )
            )
        return records

    def _BuildCandidateEvidenceRecords(
        self,
        candidate: CnCandidate,
        btiRows: Sequence[Mapping[str, str]],
    ) -> List[Stage1EvidenceRecord]:
        records = [
            Stage1EvidenceRecord(
                evidenceId="cn_candidate:{0}".format(candidate.hs8),
                evidenceType="cn_candidate_card",
                sourceName="cn_leaf_code_cards",
                sourceRef=candidate.hs8,
                candidateHs8=candidate.hs8,
                candidateHs6=candidate.hs6Code,
                text=self._TrimEvidenceText(
                    json.dumps(
                        candidate.ToPromptDict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
                legalStatus="internal_reference",
                limitations=[
                    "CN candidate card narrows review candidates and is not a final determination.",
                ],
            )
        ]

        for row in btiRows[: self.maxBtiEvidencePerCandidate]:
            chunkId = self._ReadString(row.get("chunk_id")) or "unknown"
            records.append(
                Stage1EvidenceRecord(
                    evidenceId="bti_case_chunk:{0}:{1}".format(
                        candidate.hs8,
                        chunkId,
                    ),
                    evidenceType="bti_case_chunk",
                    sourceName="bti_case_chunks",
                    sourceRef=self._ReadString(row.get("bti_reference")) or chunkId,
                    candidateHs8=candidate.hs8,
                    candidateHs6=candidate.hs6Code,
                    text=self._TrimEvidenceText(
                        self._ReadString(row.get("chunk_text")) or "",
                    ),
                    legalStatus="binding_to_holder",
                    limitations=[
                        "BTI is binding only for its holder and is used here as comparative evidence.",
                    ],
                )
            )

        return records

    def _BuildBtiRowsByCandidate(
        self,
        candidates: Sequence[CnCandidate],
    ) -> Dict[str, List[Mapping[str, str]]]:
        rowsByDomainScope = self._LoadBtiRowsByDomainScope()
        rowsByCandidate: Dict[str, List[Mapping[str, str]]] = {}
        for candidate in candidates:
            matchedRows: List[Mapping[str, str]] = []
            for row in rowsByDomainScope.get(candidate.domainScope, []):
                if not self._DoesBtiRowMatchCandidate(row, candidate):
                    continue
                matchedRows.append(row)
            rowsByCandidate[candidate.hs8] = sorted(
                matchedRows,
                key=self._BuildBtiRowSortKey,
            )[: self.maxBtiEvidencePerCandidate]
        return rowsByCandidate

    def _LoadBtiRowsByDomainScope(self) -> Dict[str, List[Dict[str, str]]]:
        rowsByDomainScope: Dict[str, List[Dict[str, str]]] = {}
        btiChunkDocument = self._FindDocument(BTI_CASE_CHUNKS_DOCUMENT_ID)
        if btiChunkDocument is None:
            return rowsByDomainScope

        for dataSource in self._ReadDataSources(btiChunkDocument):
            domainScope = self._ReadDomainScope(dataSource)
            resolvedPath = self._ResolvePath(str(dataSource.get("path", "")))
            if domainScope is None or resolvedPath is None:
                continue
            rowsByDomainScope[domainScope] = self._ReadCsvRows(resolvedPath)
        return rowsByDomainScope

    def _DoesBtiRowMatchCandidate(
        self,
        row: Mapping[str, str],
        candidate: CnCandidate,
    ) -> bool:
        candidateHs8 = self._NormalizeCode(candidate.hs8)
        candidateHs6 = self._NormalizeCode(candidate.hs6Code or "")
        rowCn8 = self._NormalizeCode(row.get("cn8", ""))
        rowAssignedCode = self._NormalizeCode(row.get("assigned_code", ""))
        rowHs6 = self._NormalizeCode(row.get("hs6", ""))
        chunkType = self._ReadString(row.get("chunk_type")) or ""
        needsReview = (self._ReadString(row.get("needs_review")) or "").lower()

        if chunkType != "case_summary":
            return False
        if needsReview in {"true", "1", "yes"}:
            return False
        if candidateHs8 and rowCn8 == candidateHs8:
            return True
        if candidateHs8 and rowAssignedCode.startswith(candidateHs8):
            return True
        return bool(candidateHs6 and rowHs6 == candidateHs6)

    def _BuildBtiRowSortKey(self, row: Mapping[str, str]) -> tuple[int, str]:
        priorityText = self._ReadString(row.get("chunk_priority")) or "9999"
        priority = int(priorityText) if priorityText.isdigit() else 9999
        return (priority, self._ReadString(row.get("chunk_id")) or "")

    def _FindDocument(self, documentId: str) -> Optional[Any]:
        documents = OntologyDocumentLoader(self.ontologyRootPath).LoadDocuments()
        for document in documents:
            if document.documentId == documentId:
                return document
        return None

    def _ReadDataSources(self, document: Any) -> List[Mapping[str, Any]]:
        dataSources = document.frontmatter.get("data_sources")
        if not isinstance(dataSources, list):
            return []
        return [
            dataSource
            for dataSource in dataSources
            if isinstance(dataSource, Mapping)
        ]

    def _ReadDomainScope(self, dataSource: Mapping[str, Any]) -> Optional[str]:
        resourceId = dataSource.get("resource_id")
        if not isinstance(resourceId, str):
            return None
        if resourceId.endswith("." + FOOD_DOMAIN_SCOPE):
            return FOOD_DOMAIN_SCOPE
        if resourceId.endswith("." + COSMETICS_DOMAIN_SCOPE):
            return COSMETICS_DOMAIN_SCOPE
        return None

    def _ResolvePath(self, declaredPath: str) -> Optional[Path]:
        if declaredPath == "":
            return None
        for candidatePath in [
            self.ontologyRootPath / declaredPath,
            self.projectRootPath / declaredPath,
        ]:
            if candidatePath.exists():
                return candidatePath
        return None

    def _ReadCsvRows(self, csvPath: Path) -> List[Dict[str, str]]:
        with csvPath.open("r", encoding="utf-8-sig", newline="") as csvFile:
            return [
                dict(row)
                for row in csv.DictReader(csvFile)
            ]

    def _TrimEvidenceText(self, text: str) -> str:
        normalizedText = NormalizeWhitespaceLines(text)
        if len(normalizedText) <= self.maxEvidenceTextCharacters:
            return normalizedText
        return normalizedText[: self.maxEvidenceTextCharacters].rstrip() + "..."

    def _ReadString(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalizedValue = NormalizeWhiteSpace(value)
        return normalizedValue or None

    def _NormalizeCode(self, code: str) -> str:
        return "".join(character for character in code if character.isdigit())


class CnCandidateRetriever:
    """CN leaf card CSV를 이용해 product profile과 가까운 CN8 후보를 찾는다."""

    def __init__(
        self,
        ontologyRootPath: str | Path,
        projectRootPath: Optional[str | Path] = None,
    ) -> None:
        self.ontologyRootPath = Path(ontologyRootPath)
        self.projectRootPath = (
            Path(projectRootPath)
            if projectRootPath is not None
            else self.ontologyRootPath.parent
        )
        self._rowsByDomainScope: Optional[Dict[str, List[Dict[str, str]]]] = None

    def FindCandidates(
        self,
        productInput: ProductClassificationInput,
        topK: int = DEFAULT_CN_CANDIDATE_TOP_K,
    ) -> List[CnCandidate]:
        rowsByDomainScope = self._LoadRowsByDomainScope()
        searchText, searchTextByTier, searchTermsByTier = self._BuildSearchProfile(
            productInput,
        )
        candidates: List[CnCandidate] = []

        for domainScope in productInput.domainScopes:
            for row in rowsByDomainScope.get(domainScope, []):
                candidate = self._ScoreRow(
                    row=row,
                    domainScope=domainScope,
                    searchText=searchText,
                    searchTextByTier=searchTextByTier,
                    searchTermsByTier=searchTermsByTier,
                )
                if candidate.score > 0:
                    candidates.append(candidate)

        return self._SelectTopCandidates(candidates, productInput, topK)

    def FindCandidatesWithSemanticIndex(
        self,
        productInput: ProductClassificationInput,
        semanticIndex: CnSemanticCandidateIndex,
        heuristicTopK: int = DEFAULT_CN_CANDIDATE_TOP_K,
        semanticTopK: int = DEFAULT_SEMANTIC_CANDIDATE_TOP_K,
        finalCandidateLimit: Optional[int] = None,
        minSemanticScore: float = 0.0,
    ) -> List[CnCandidate]:
        semanticHits = semanticIndex.Search(
            queryText=productInput.BuildSemanticSearchText(),
            domainScopes=productInput.domainScopes,
            topK=semanticTopK,
            minScore=minSemanticScore,
        )
        return self._FindCandidatesWithSemanticHits(
            productInput=productInput,
            semanticHits=semanticHits,
            heuristicTopK=heuristicTopK,
            finalCandidateLimit=finalCandidateLimit,
        )

    def _FindCandidatesWithSemanticHits(
        self,
        productInput: ProductClassificationInput,
        semanticHits: Sequence[CnSemanticSearchHit],
        heuristicTopK: int = DEFAULT_CN_CANDIDATE_TOP_K,
        finalCandidateLimit: Optional[int] = None,
    ) -> List[CnCandidate]:
        heuristicCandidates = self.FindCandidates(productInput, topK=heuristicTopK)
        semanticCandidates = self._BuildCandidatesFromSemanticHits(
            productInput,
            semanticHits,
        )
        return self._MergeCandidateSets(
            heuristicCandidates=heuristicCandidates,
            semanticCandidates=semanticCandidates,
            finalCandidateLimit=finalCandidateLimit,
        )

    def _BuildCandidatesFromSemanticHits(
        self,
        productInput: ProductClassificationInput,
        semanticHits: Sequence[CnSemanticSearchHit],
    ) -> List[CnCandidate]:
        if not semanticHits:
            return []

        rowsByDomainScope = self._LoadRowsByDomainScope()
        rowByDomainScopeAndCode: Dict[tuple[str, str], Mapping[str, str]] = {}
        for domainScope, rows in rowsByDomainScope.items():
            for row in rows:
                candidateCode = row.get("cn", "") or row.get("hs8", "")
                if candidateCode:
                    rowByDomainScopeAndCode[(domainScope, candidateCode)] = row

        searchText, searchTextByTier, searchTermsByTier = self._BuildSearchProfile(
            productInput,
        )

        candidates: List[CnCandidate] = []
        domainScopeSet = set(productInput.domainScopes)
        for semanticHit in semanticHits:
            if domainScopeSet and semanticHit.domainScope not in domainScopeSet:
                continue
            row = rowByDomainScopeAndCode.get(
                (semanticHit.domainScope, semanticHit.candidateCode),
            )
            if row is None:
                continue
            candidate = self._ScoreRow(
                row=row,
                domainScope=semanticHit.domainScope,
                searchText=searchText,
                searchTextByTier=searchTextByTier,
                searchTermsByTier=searchTermsByTier,
            )
            if candidate.excludeRuleMatches:
                continue
            candidates.append(
                candidate.model_copy(
                    update={
                        "retrievalSources": [RETRIEVAL_SOURCE_SEMANTIC],
                        "semanticScore": round(semanticHit.score, 6),
                        "semanticMatches": [
                            semanticMatch.model_dump(
                                mode="json",
                                by_alias=True,
                            )
                            for semanticMatch in semanticHit.matchedChunks
                        ],
                    },
                )
            )

        return candidates

    def _MergeCandidateSets(
        self,
        heuristicCandidates: Sequence[CnCandidate],
        semanticCandidates: Sequence[CnCandidate],
        finalCandidateLimit: Optional[int] = None,
    ) -> List[CnCandidate]:
        if finalCandidateLimit is not None and finalCandidateLimit <= 0:
            return []

        candidatesByCode: Dict[str, CnCandidate] = {}
        for candidate in heuristicCandidates:
            candidatesByCode[candidate.hs8] = candidate

        for semanticCandidate in semanticCandidates:
            existingCandidate = candidatesByCode.get(semanticCandidate.hs8)
            if existingCandidate is None:
                candidatesByCode[semanticCandidate.hs8] = semanticCandidate
                continue
            retrievalSources = list(existingCandidate.retrievalSources)
            for retrievalSource in semanticCandidate.retrievalSources:
                if retrievalSource not in retrievalSources:
                    retrievalSources.append(retrievalSource)
            candidatesByCode[semanticCandidate.hs8] = existingCandidate.model_copy(
                update={
                    "retrievalSources": retrievalSources,
                    "semanticScore": semanticCandidate.semanticScore,
                    "semanticMatches": list(semanticCandidate.semanticMatches),
                },
            )

        bothSourceCandidates = sorted(
            (
                candidate
                for candidate in candidatesByCode.values()
                if RETRIEVAL_SOURCE_HEURISTIC in candidate.retrievalSources
                and RETRIEVAL_SOURCE_SEMANTIC in candidate.retrievalSources
            ),
            key=lambda candidate: (
                -candidate.score,
                -(candidate.semanticScore or 0.0),
                candidate.domainScope,
                candidate.hs4Code or "",
                candidate.hs8,
            ),
        )
        heuristicOnlyCandidates = [
            candidate
            for candidate in self._SortCandidates(candidatesByCode.values())
            if RETRIEVAL_SOURCE_HEURISTIC in candidate.retrievalSources
            and RETRIEVAL_SOURCE_SEMANTIC not in candidate.retrievalSources
        ]
        semanticOnlyCandidates = sorted(
            (
                candidate
                for candidate in candidatesByCode.values()
                if RETRIEVAL_SOURCE_SEMANTIC in candidate.retrievalSources
                and RETRIEVAL_SOURCE_HEURISTIC not in candidate.retrievalSources
            ),
            key=lambda candidate: (
                -(candidate.semanticScore or 0.0),
                -candidate.score,
                candidate.domainScope,
                candidate.hs4Code or "",
                candidate.hs8,
            ),
        )

        mergedCandidatesByPriority: List[CnCandidate] = list(bothSourceCandidates)

        maxParallelLength = max(
            len(heuristicOnlyCandidates),
            len(semanticOnlyCandidates),
        )
        for index in range(maxParallelLength):
            if index < len(heuristicOnlyCandidates):
                mergedCandidatesByPriority.append(heuristicOnlyCandidates[index])
            if index < len(semanticOnlyCandidates):
                mergedCandidatesByPriority.append(semanticOnlyCandidates[index])

        if finalCandidateLimit is None:
            return mergedCandidatesByPriority

        return self._SelectBranchAwareCandidates(
            sortedCandidates=mergedCandidatesByPriority,
            topK=finalCandidateLimit,
            preferredHeadingCodes=[],
        )

    def FindSiblingCandidates(
        self,
        productInput: ProductClassificationInput,
        currentCandidates: Sequence[CnCandidate],
        excludedHs8Codes: Sequence[str],
        topK: int = DEFAULT_CN_CANDIDATE_TOP_K,
    ) -> List[CnCandidate]:
        rowsByDomainScope = self._LoadRowsByDomainScope()
        searchText, searchTextByTier, searchTermsByTier = self._BuildSearchProfile(
            productInput,
        )
        excludedHs8CodeSet = set(excludedHs8Codes)
        parentHs4CodesByDomainScope: Dict[str, Set[str]] = {}
        parentHs6CodesByDomainScope: Dict[str, Set[str]] = {}

        for candidate in currentCandidates:
            if candidate.hs4Code is not None:
                parentHs4CodesByDomainScope.setdefault(
                    candidate.domainScope,
                    set(),
                ).add(candidate.hs4Code)
            if candidate.hs6Code is None:
                continue
            parentHs6CodesByDomainScope.setdefault(
                candidate.domainScope,
                set(),
            ).add(candidate.hs6Code)

        siblingCandidates: List[CnCandidate] = []
        for domainScope in productInput.domainScopes:
            parentHs4Codes = parentHs4CodesByDomainScope.get(domainScope, set())
            parentHs6Codes = parentHs6CodesByDomainScope.get(domainScope, set())
            for row in rowsByDomainScope.get(domainScope, []):
                hs8 = row.get("cn", "") or row.get("hs8", "") or row.get("cn8", "")
                hs4 = row.get("heading", "") or row.get("hs4_code", "")
                hs6 = row.get("subheading", "") or row.get("hs6_code", "")
                if hs8 in excludedHs8CodeSet:
                    continue
                if parentHs4Codes and hs4 not in parentHs4Codes:
                    continue
                if not parentHs4Codes and parentHs6Codes and hs6 not in parentHs6Codes:
                    continue
                candidate = self._ScoreRow(
                    row=row,
                    domainScope=domainScope,
                    searchText=searchText,
                    searchTextByTier=searchTextByTier,
                    searchTermsByTier=searchTermsByTier,
                )
                if candidate.score <= 0:
                    continue
                siblingCandidates.append(candidate)

        currentHs6CodeSet = {
            candidate.hs6Code
            for candidate in currentCandidates
            if candidate.hs6Code is not None
        }
        sortedSiblingCandidates = self._SortCandidates(siblingCandidates)
        selectedCandidates: List[CnCandidate] = []
        selectedHs8Codes: Set[str] = set()
        selectedHs6Codes: Set[str] = set()

        for candidate in sortedSiblingCandidates:
            if len(selectedCandidates) >= topK:
                break
            if candidate.hs8 in selectedHs8Codes:
                continue
            if candidate.hs6Code in currentHs6CodeSet:
                continue
            if candidate.hs6Code in selectedHs6Codes:
                continue
            selectedCandidates.append(candidate)
            selectedHs8Codes.add(candidate.hs8)
            if candidate.hs6Code is not None:
                selectedHs6Codes.add(candidate.hs6Code)

        for candidate in sortedSiblingCandidates:
            if len(selectedCandidates) >= topK:
                break
            if candidate.hs8 in selectedHs8Codes:
                continue
            if candidate.hs6Code in selectedHs6Codes:
                continue
            selectedCandidates.append(candidate)
            selectedHs8Codes.add(candidate.hs8)
            if candidate.hs6Code is not None:
                selectedHs6Codes.add(candidate.hs6Code)

        for candidate in sortedSiblingCandidates:
            if len(selectedCandidates) >= topK:
                break
            if candidate.hs8 in selectedHs8Codes:
                continue
            selectedCandidates.append(candidate)
            selectedHs8Codes.add(candidate.hs8)

        return selectedCandidates[:topK]

    def FindAlternativeCandidates(
        self,
        productInput: ProductClassificationInput,
        currentCandidates: Sequence[CnCandidate],
        excludedHs8Codes: Sequence[str],
        topK: int = DEFAULT_CN_CANDIDATE_TOP_K,
    ) -> List[CnCandidate]:
        rowsByDomainScope = self._LoadRowsByDomainScope()
        searchText, searchTextByTier, searchTermsByTier = self._BuildSearchProfile(
            productInput,
        )
        excludedHs8CodeSet = set(excludedHs8Codes)
        currentHs4Codes = {
            candidate.hs4Code
            for candidate in currentCandidates
            if candidate.hs4Code is not None
        }
        candidates: List[CnCandidate] = []

        for domainScope in productInput.domainScopes:
            for row in rowsByDomainScope.get(domainScope, []):
                hs8 = row.get("cn", "") or row.get("hs8", "") or row.get("cn8", "")
                if hs8 in excludedHs8CodeSet:
                    continue
                candidate = self._ScoreRow(
                    row=row,
                    domainScope=domainScope,
                    searchText=searchText,
                    searchTextByTier=searchTextByTier,
                    searchTermsByTier=searchTermsByTier,
                )
                if candidate.score <= 0:
                    continue
                candidates.append(candidate)

        nonCurrentHs4Candidates = [
            candidate
            for candidate in candidates
            if candidate.hs4Code not in currentHs4Codes
            and (
                candidate.primaryEvidenceMatches
                or candidate.secondaryEvidenceMatches
            )
        ]
        return self._SelectTopCandidates(nonCurrentHs4Candidates, productInput, topK)

    def LoadRowsByDomainScope(self) -> Dict[str, List[Dict[str, str]]]:
        """semantic retrieval 등 외부 후보 검색기가 동일 CN table row를 재사용하게 한다."""

        rowsByDomainScope = self._LoadRowsByDomainScope()
        return {
            domainScope: [dict(row) for row in rows]
            for domainScope, rows in rowsByDomainScope.items()
        }

    def _BuildSearchProfile(
        self,
        productInput: ProductClassificationInput,
    ) -> tuple[str, Dict[str, str], Dict[str, Set[str]]]:
        primarySearchText = productInput.BuildPrimarySearchText()
        secondarySearchText = productInput.BuildSecondarySearchText()
        weakSearchText = productInput.BuildWeakSearchText()
        searchTextByTier = {
            EVIDENCE_TIER_PRIMARY: primarySearchText,
            EVIDENCE_TIER_SECONDARY: secondarySearchText,
            EVIDENCE_TIER_WEAK: weakSearchText,
        }
        searchTermsByTier = {
            tier: self._BuildExpandedSearchTerms(searchText)
            for tier, searchText in searchTextByTier.items()
        }
        return productInput.BuildSearchText(), searchTextByTier, searchTermsByTier

    def _ScoreRow(
        self,
        row: Mapping[str, str],
        domainScope: str,
        searchText: str,
        searchTextByTier: Mapping[str, str],
        searchTermsByTier: Mapping[str, Set[str]],
    ) -> CnCandidate:
        matchedTerms: Set[str] = set()
        excludedTerms: Set[str] = set()
        includeTierMatches = self._FindTieredCellMatches(
            row.get("include_rule_keywords", ""),
            searchTextByTier,
            searchTermsByTier,
        )
        searchKeywordTierMatches = self._FindTieredCellMatches(
            row.get("search_keywords", ""),
            searchTextByTier,
            searchTermsByTier,
        )
        (
            descriptionTierMatches,
            hierarchyLevelMatches,
            hierarchyLevelPoints,
            descriptionPoints,
        ) = self._ScoreHierarchyDescriptionMatches(
            row,
            searchTermsByTier,
        )
        excludeMatches = self._FindCellMatches(
            row.get("exclude_rule_keywords", ""),
            searchText,
            self._BuildExpandedSearchTerms(searchText),
        )
        includeMatches = self._FlattenTierMatches(includeTierMatches)
        searchKeywordMatches = self._FlattenTierMatches(searchKeywordTierMatches)
        descriptionMatches = self._FlattenTierMatches(descriptionTierMatches)
        includeRulePoints = self._ScoreTierMatches(
            includeTierMatches,
            TIER_INCLUDE_RULE_WEIGHTS,
        )
        searchKeywordPoints = self._ScoreTierMatches(
            searchKeywordTierMatches,
            TIER_SEARCH_KEYWORD_WEIGHTS,
        )
        score = includeRulePoints + searchKeywordPoints + descriptionPoints

        matchedTerms.update(includeMatches)
        matchedTerms.update(searchKeywordMatches)
        matchedTerms.update(descriptionMatches)
        excludedTerms.update(excludeMatches)

        if excludeMatches:
            return self._BuildCandidate(
                row=row,
                domainScope=domainScope,
                score=0.0,
                matchedTerms=sorted(matchedTerms),
                excludedTerms=sorted(excludedTerms),
                includeRuleMatches=includeMatches,
                searchKeywordMatches=searchKeywordMatches,
                descriptionMatches=descriptionMatches,
                excludeRuleMatches=excludeMatches,
                tierMatches=self._MergeTierMatches(
                    includeTierMatches,
                    searchKeywordTierMatches,
                    descriptionTierMatches,
                ),
                includeRulePoints=0.0,
                searchKeywordPoints=0.0,
                descriptionPoints=0.0,
                hierarchyLevelPoints={},
                hierarchyLevelMatches={},
            )

        if score < 0:
            score = 0.0

        return self._BuildCandidate(
            row=row,
            domainScope=domainScope,
            score=score,
            matchedTerms=sorted(matchedTerms),
            excludedTerms=sorted(excludedTerms),
            includeRuleMatches=includeMatches,
            searchKeywordMatches=searchKeywordMatches,
            descriptionMatches=descriptionMatches,
            excludeRuleMatches=excludeMatches,
            tierMatches=self._MergeTierMatches(
                includeTierMatches,
                searchKeywordTierMatches,
                descriptionTierMatches,
            ),
            includeRulePoints=includeRulePoints,
            searchKeywordPoints=searchKeywordPoints,
            descriptionPoints=descriptionPoints,
            hierarchyLevelPoints=hierarchyLevelPoints,
            hierarchyLevelMatches=hierarchyLevelMatches,
        )

    def _BuildCandidate(
        self,
        row: Mapping[str, str],
        domainScope: str,
        score: float,
        matchedTerms: Sequence[str],
        excludedTerms: Sequence[str],
        includeRuleMatches: Sequence[str],
        searchKeywordMatches: Sequence[str],
        descriptionMatches: Sequence[str],
        excludeRuleMatches: Sequence[str],
        tierMatches: Mapping[str, Sequence[str]],
        includeRulePoints: float,
        searchKeywordPoints: float,
        descriptionPoints: float,
        hierarchyLevelPoints: Mapping[str, float],
        hierarchyLevelMatches: Mapping[str, Sequence[str]],
        retrievalSources: Optional[Sequence[str]] = None,
        semanticScore: Optional[float] = None,
        semanticMatches: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> CnCandidate:
        cnCode = row.get("cn", "") or row.get("hs8", "") or row.get("cn8", "")
        chapterCode = row.get("chapter", "") or row.get("hs2_code") or None
        headingCode = row.get("heading", "") or row.get("hs4_code") or None
        subheadingCode = row.get("subheading", "") or row.get("hs6_code") or None
        cnPart = row.get("cn_part", "")
        subheadingDescription = row.get("subheading_description") or row.get(
            "hs6_description",
            "",
        )
        cnDescription = (
            row.get("cn_description")
            or row.get("hs8_description")
            or row.get("cn8_description", "")
        )
        if not subheadingDescription and (cnPart == "00" or cnCode.endswith("00")):
            subheadingDescription = cnDescription
        candidateContextText = self._BuildCandidateContextText(row)
        combinedDescription = row.get("combined_description", "") or candidateContextText

        return CnCandidate(
            hs8=cnCode,
            domainScope=domainScope,
            score=round(score, 3),
            matchedTerms=list(matchedTerms),
            excludedTerms=list(excludedTerms),
            includeRuleMatches=list(includeRuleMatches),
            searchKeywordMatches=list(searchKeywordMatches),
            descriptionMatches=list(descriptionMatches),
            excludeRuleMatches=list(excludeRuleMatches),
            primaryEvidenceMatches=list(tierMatches.get(EVIDENCE_TIER_PRIMARY, [])),
            secondaryEvidenceMatches=list(
                tierMatches.get(EVIDENCE_TIER_SECONDARY, []),
            ),
            weakEvidenceMatches=list(tierMatches.get(EVIDENCE_TIER_WEAK, [])),
            includeRulePoints=round(includeRulePoints, 3),
            searchKeywordPoints=round(searchKeywordPoints, 3),
            descriptionPoints=round(descriptionPoints, 3),
            hierarchyLevelPoints={
                level: round(points, 3)
                for level, points in hierarchyLevelPoints.items()
            },
            hierarchyLevelMatches={
                level: list(matches)
                for level, matches in hierarchyLevelMatches.items()
            },
            hs2Code=chapterCode,
            hs2Description=row.get("chapter_description")
            or row.get("hs2_description")
            or None,
            hs4Code=headingCode,
            hs4Description=row.get("heading_description")
            or row.get("hs4_description")
            or None,
            hs6Code=subheadingCode,
            hs6Description=subheadingDescription or None,
            hs8Code=cnCode or None,
            hs8Description=cnDescription or None,
            branchContext=row.get("branch_context", ""),
            candidateContextText=candidateContextText,
            combinedDescription=combinedDescription,
            includeRuleKeywords=row.get("include_rule_keywords", ""),
            excludeRuleKeywords=row.get("exclude_rule_keywords", ""),
            hardConditions=row.get("hard_conditions", ""),
            cnExplanatoryNote=row.get("cn_explanatory_note", ""),
            needsHumanReview=True,
            retrievalSources=list(retrievalSources or [RETRIEVAL_SOURCE_HEURISTIC]),
            semanticScore=semanticScore,
            semanticMatches=[
                dict(semanticMatch)
                for semanticMatch in (semanticMatches or [])
            ],
        )

    def _LoadRowsByDomainScope(self) -> Dict[str, List[Dict[str, str]]]:
        if self._rowsByDomainScope is not None:
            return self._rowsByDomainScope

        rowsByDomainScope: Dict[str, List[Dict[str, str]]] = {}
        cnTablePath = self._ResolvePath(str(CN_TABLE_RELATIVE_PATH))
        if cnTablePath is not None:
            self._rowsByDomainScope = self._ReadCnTableRowsByDomainScope(cnTablePath)
            return self._rowsByDomainScope

        leafCardDocument = self._FindLeafCardDocument()
        if leafCardDocument is None:
            self._rowsByDomainScope = rowsByDomainScope
            return rowsByDomainScope

        for dataSource in self._ReadDataSources(leafCardDocument):
            domainScope = self._ReadDomainScope(dataSource)
            resolvedPath = self._ResolvePath(str(dataSource.get("path", "")))
            if domainScope is None or resolvedPath is None:
                continue
            rowsByDomainScope[domainScope] = self._ReadCsvRows(resolvedPath)

        self._rowsByDomainScope = rowsByDomainScope
        return rowsByDomainScope

    def _FindLeafCardDocument(self) -> Optional[Any]:
        documents = OntologyDocumentLoader(self.ontologyRootPath).LoadDocuments()
        for document in documents:
            if document.documentId == CN_LEAF_CODE_CARDS_DOCUMENT_ID:
                return document
        return None

    def _ReadDataSources(self, document: Any) -> List[Mapping[str, Any]]:
        dataSources = document.frontmatter.get("data_sources")
        if not isinstance(dataSources, list):
            return []
        return [
            dataSource
            for dataSource in dataSources
            if isinstance(dataSource, Mapping)
        ]

    def _ReadDomainScope(self, dataSource: Mapping[str, Any]) -> Optional[str]:
        resourceId = dataSource.get("resource_id")
        if not isinstance(resourceId, str):
            return None
        if resourceId.endswith("." + FOOD_DOMAIN_SCOPE):
            return FOOD_DOMAIN_SCOPE
        if resourceId.endswith("." + COSMETICS_DOMAIN_SCOPE):
            return COSMETICS_DOMAIN_SCOPE
        return None

    def _ResolvePath(self, declaredPath: str) -> Optional[Path]:
        if declaredPath == "":
            return None
        for candidatePath in [
            self.ontologyRootPath / declaredPath,
            self.projectRootPath / declaredPath,
        ]:
            if candidatePath.exists():
                return candidatePath
        return None

    def _ReadCsvRows(self, csvPath: Path) -> List[Dict[str, str]]:
        with csvPath.open("r", encoding="utf-8-sig", newline="") as csvFile:
            return [
                dict(row)
                for row in csv.DictReader(csvFile)
            ]

    def _ReadCnTableRowsByDomainScope(
        self,
        csvPath: Path,
    ) -> Dict[str, List[Dict[str, str]]]:
        rowsByDomainScope = {
            FOOD_DOMAIN_SCOPE: [],
            COSMETICS_DOMAIN_SCOPE: [],
        }
        for row in self._ReadCsvRows(csvPath):
            chapter = (row.get("chapter", "") or row.get("hs2_code", "")).zfill(2)
            if chapter in FOOD_DOMAIN_SCOPE_CHAPTERS:
                rowsByDomainScope[FOOD_DOMAIN_SCOPE].append(row)
            if chapter in COSMETICS_DOMAIN_SCOPE_CHAPTERS:
                rowsByDomainScope[COSMETICS_DOMAIN_SCOPE].append(row)
        return rowsByDomainScope

    def _SelectTopCandidates(
        self,
        candidates: Sequence[CnCandidate],
        productInput: ProductClassificationInput,
        topK: int,
    ) -> List[CnCandidate]:
        sortedCandidates = self._SortCandidates(candidates)
        if topK <= 0:
            return []

        return self._SelectBranchAwareCandidates(
            sortedCandidates=sortedCandidates,
            topK=topK,
            preferredHeadingCodes=self._BuildPreferredHeadingCodes(productInput),
        )

    def _SelectBranchAwareCandidates(
        self,
        sortedCandidates: Sequence[CnCandidate],
        topK: int,
        preferredHeadingCodes: Sequence[str],
    ) -> List[CnCandidate]:
        selectedCandidates: List[CnCandidate] = []
        selectedHs8Codes: Set[str] = set()
        hs4Counts: Dict[str, int] = {}

        for headingCode in preferredHeadingCodes:
            if len(selectedCandidates) >= topK:
                break
            for candidate in sortedCandidates:
                if candidate.hs4Code != headingCode:
                    continue
                self._AppendCandidateIfNew(
                    selectedCandidates,
                    selectedHs8Codes,
                    hs4Counts,
                    candidate,
                    enforceHs4Limit=False,
                )
                break

        # 초기 후보군이 하나의 잘못된 HS4/HS6 루트에 갇히지 않도록
        # 점수순 fill 전에 계층 branch 대표 후보를 먼저 확보한다.
        self._AppendBranchRepresentatives(
            selectedCandidates=selectedCandidates,
            selectedHs8Codes=selectedHs8Codes,
            hs4Counts=hs4Counts,
            sortedCandidates=sortedCandidates,
            branchLevel="hs4",
            representativeLimit=min(
                topK,
                DEFAULT_INITIAL_HS4_BRANCH_REPRESENTATIVE_LIMIT,
            ),
            enforceHs4Limit=False,
        )
        self._AppendBranchRepresentatives(
            selectedCandidates=selectedCandidates,
            selectedHs8Codes=selectedHs8Codes,
            hs4Counts=hs4Counts,
            sortedCandidates=sortedCandidates,
            branchLevel="hs6",
            representativeLimit=min(
                topK,
                DEFAULT_INITIAL_HS6_BRANCH_REPRESENTATIVE_LIMIT,
            ),
            enforceHs4Limit=True,
        )

        for candidate in sortedCandidates:
            if len(selectedCandidates) >= topK:
                break
            self._AppendCandidateIfNew(
                selectedCandidates,
                selectedHs8Codes,
                hs4Counts,
                candidate,
                enforceHs4Limit=True,
            )

        for candidate in sortedCandidates:
            if len(selectedCandidates) >= topK:
                break
            self._AppendCandidateIfNew(
                selectedCandidates,
                selectedHs8Codes,
                hs4Counts,
                candidate,
                enforceHs4Limit=False,
            )

        return self._SortCandidates(selectedCandidates)[:topK]

    def _AppendBranchRepresentatives(
        self,
        selectedCandidates: List[CnCandidate],
        selectedHs8Codes: Set[str],
        hs4Counts: Dict[str, int],
        sortedCandidates: Sequence[CnCandidate],
        branchLevel: str,
        representativeLimit: int,
        enforceHs4Limit: bool,
    ) -> None:
        if len(selectedCandidates) >= representativeLimit:
            return

        candidatesByBranchKey = self._GroupCandidatesByBranchKey(
            sortedCandidates,
            branchLevel,
        )
        selectedBranchKeys = self._BuildSelectedBranchKeys(
            selectedCandidates,
            branchLevel,
        )

        for branchKey in self._RankBranchKeys(candidatesByBranchKey):
            if len(selectedCandidates) >= representativeLimit:
                break
            if branchKey in selectedBranchKeys:
                continue
            branchCandidates = candidatesByBranchKey.get(branchKey, [])
            representative = self._SelectBranchRepresentative(branchCandidates)
            if representative is None:
                continue
            appended = self._AppendCandidateIfNew(
                selectedCandidates,
                selectedHs8Codes,
                hs4Counts,
                representative,
                enforceHs4Limit=enforceHs4Limit,
            )
            if appended:
                selectedBranchKeys.add(branchKey)

    def _GroupCandidatesByBranchKey(
        self,
        candidates: Sequence[CnCandidate],
        branchLevel: str,
    ) -> Dict[str, List[CnCandidate]]:
        candidatesByBranchKey: Dict[str, List[CnCandidate]] = {}
        for candidate in candidates:
            branchKey = self._BuildCandidateBranchKey(candidate, branchLevel)
            if branchKey == "":
                continue
            candidatesByBranchKey.setdefault(branchKey, []).append(candidate)
        return candidatesByBranchKey

    def _BuildSelectedBranchKeys(
        self,
        candidates: Sequence[CnCandidate],
        branchLevel: str,
    ) -> Set[str]:
        return {
            branchKey
            for branchKey in (
                self._BuildCandidateBranchKey(candidate, branchLevel)
                for candidate in candidates
            )
            if branchKey != ""
        }

    def _BuildCandidateBranchKey(
        self,
        candidate: CnCandidate,
        branchLevel: str,
    ) -> str:
        if branchLevel == "hs4":
            return candidate.hs4Code or ""
        if branchLevel == "hs6":
            if candidate.hs6Code is None:
                return ""
            return "{0}:{1}".format(candidate.hs4Code or "unknown", candidate.hs6Code)
        return ""

    def _RankBranchKeys(
        self,
        candidatesByBranchKey: Mapping[str, Sequence[CnCandidate]],
    ) -> List[str]:
        return sorted(
            candidatesByBranchKey.keys(),
            key=lambda branchKey: self._BuildBranchRankKey(
                candidatesByBranchKey[branchKey],
            ),
        )

    def _BuildBranchRankKey(
        self,
        branchCandidates: Sequence[CnCandidate],
    ) -> tuple[Any, ...]:
        sortedBranchCandidates = self._SortCandidates(branchCandidates)
        representative = self._SelectBranchRepresentative(sortedBranchCandidates)
        if representative is None:
            return (1, 0.0, 0.0, 0.0, 0, 0, "", "", "", "")

        hasPrimaryOrSecondaryEvidence = any(
            self._HasPrimaryOrSecondaryEvidence(candidate)
            for candidate in sortedBranchCandidates
        )
        topBranchCandidates = sortedBranchCandidates[:2]
        aggregateScore = sum(candidate.score for candidate in topBranchCandidates)
        semanticScore = max(
            (candidate.semanticScore or 0.0)
            for candidate in sortedBranchCandidates
        )
        primaryMatchCount = sum(
            len(candidate.primaryEvidenceMatches)
            for candidate in topBranchCandidates
        )
        secondaryMatchCount = sum(
            len(candidate.secondaryEvidenceMatches)
            for candidate in topBranchCandidates
        )
        return (
            0 if hasPrimaryOrSecondaryEvidence else 1,
            -representative.score,
            -aggregateScore,
            -semanticScore,
            -primaryMatchCount,
            -secondaryMatchCount,
            representative.domainScope,
            representative.hs4Code or "",
            representative.hs6Code or "",
            representative.hs8,
        )

    def _SelectBranchRepresentative(
        self,
        branchCandidates: Sequence[CnCandidate],
    ) -> Optional[CnCandidate]:
        sortedBranchCandidates = self._SortCandidates(branchCandidates)
        for candidate in sortedBranchCandidates:
            if self._HasPrimaryOrSecondaryEvidence(candidate):
                return candidate
        if not sortedBranchCandidates:
            return None
        return sortedBranchCandidates[0]

    def _HasPrimaryOrSecondaryEvidence(
        self,
        candidate: CnCandidate,
    ) -> bool:
        return bool(
            candidate.primaryEvidenceMatches
            or candidate.secondaryEvidenceMatches
        )

    def _SortCandidates(
        self,
        candidates: Sequence[CnCandidate],
    ) -> List[CnCandidate]:
        return sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                -(candidate.semanticScore or 0.0),
                candidate.domainScope,
                candidate.hs4Code or "",
                candidate.hs8,
            ),
        )

    def _AppendCandidateIfNew(
        self,
        selectedCandidates: List[CnCandidate],
        selectedHs8Codes: Set[str],
        hs4Counts: Dict[str, int],
        candidate: CnCandidate,
        enforceHs4Limit: bool,
    ) -> bool:
        if candidate.hs8 in selectedHs8Codes:
            return False
        hs4Code = candidate.hs4Code or "unknown"
        if (
            enforceHs4Limit
            and hs4Counts.get(hs4Code, 0) >= DEFAULT_MAX_CANDIDATES_PER_HS4
        ):
            return False
        selectedCandidates.append(candidate)
        selectedHs8Codes.add(candidate.hs8)
        hs4Counts[hs4Code] = hs4Counts.get(hs4Code, 0) + 1
        return True

    def _BuildPreferredHeadingCodes(
        self,
        productInput: ProductClassificationInput,
    ) -> List[str]:
        searchText = NormalizeWhiteSpace(
            "\n".join(
                [
                    productInput.BuildPrimarySearchText(),
                    productInput.BuildSecondarySearchText(),
                ]
            )
        ).lower()
        preferredHeadingCodes: List[str] = []
        for hintText, headingCodes in PREFERRED_HEADING_HINTS.items():
            if hintText.lower() not in searchText:
                continue
            for headingCode in headingCodes:
                if headingCode not in preferredHeadingCodes:
                    preferredHeadingCodes.append(headingCode)
        return preferredHeadingCodes

    def _BuildCandidateContextText(self, row: Mapping[str, str]) -> str:
        chapterCode = row.get("chapter", "") or row.get("hs2_code", "")
        subheadingDescription = row.get("subheading_description") or row.get(
            "hs6_description",
            "",
        )
        cnCode = row.get("cn", "") or row.get("hs8", "") or row.get("cn8", "")
        cnDescription = (
            row.get("cn_description", "")
            or row.get("hs8_description", "")
            or row.get("cn8_description", "")
        )
        if not subheadingDescription and (
            row.get("cn_part", "") == "00" or cnCode.endswith("00")
        ):
            subheadingDescription = cnDescription
        parts = [
            (
                "chapter",
                chapterCode,
                row.get("chapter_description", "") or row.get("hs2_description", ""),
            ),
            (
                "heading",
                row.get("heading", "") or row.get("hs4_code", ""),
                row.get("heading_description", "") or row.get("hs4_description", ""),
            ),
            (
                "subheading",
                row.get("subheading", "") or row.get("hs6_code", ""),
                subheadingDescription,
            ),
            ("branch_context", "", row.get("branch_context", "")),
            (
                "cn_description",
                cnCode,
                cnDescription,
            ),
        ]
        contextLines = []
        for label, code, description in parts:
            normalizedCode = NormalizeWhiteSpace(code)
            normalizedDescription = NormalizeWhiteSpace(description)
            if not normalizedCode and not normalizedDescription:
                continue
            if normalizedCode and normalizedDescription:
                contextLines.append(
                    "{0}: {1} - {2}".format(
                        label,
                        normalizedCode,
                        normalizedDescription,
                    )
                )
                continue
            contextLines.append("{0}: {1}".format(label, normalizedDescription))
        return NormalizeWhitespaceLines("\n".join(contextLines))

    def _ScoreHierarchyDescriptionMatches(
        self,
        row: Mapping[str, str],
        searchTermsByTier: Mapping[str, Set[str]],
    ) -> tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, float], float]:
        candidateContextText = self._BuildCandidateContextText(row)
        descriptionTierMatches = self._FindTieredTokenMatches(
            " ".join(
                [
                    candidateContextText,
                    row.get("combined_description", ""),
                    row.get("cn_explanatory_note", ""),
                    row.get("cn_description", ""),
                    row.get("cn8_description", ""),
                    row.get("branch_context", ""),
                    row.get("hs8_description", ""),
                ]
            ),
            searchTermsByTier,
        )
        descriptionPoints = self._ScoreTierMatches(
            descriptionTierMatches,
            TIER_DESCRIPTION_WEIGHTS,
        )

        subheadingDescription = row.get("subheading_description") or row.get(
            "hs6_description",
            "",
        )
        cnCode = row.get("cn", "") or row.get("hs8", "") or row.get("cn8", "")
        cnDescription = (
            row.get("cn_description", "")
            or row.get("hs8_description", "")
            or row.get("cn8_description", "")
        )
        if not subheadingDescription and (
            row.get("cn_part", "") == "00" or cnCode.endswith("00")
        ):
            subheadingDescription = cnDescription

        hierarchyTextByLevel = {
            "hs2": row.get("chapter_description", "")
            or row.get("hs2_description", ""),
            "hs4": row.get("heading_description", "")
            or row.get("hs4_description", ""),
            "hs6": subheadingDescription,
            "branch": row.get("branch_context", ""),
            "cn8": cnDescription,
            "note": row.get("cn_explanatory_note", ""),
        }
        hierarchyLevelMatches: Dict[str, List[str]] = {}
        hierarchyLevelPoints: Dict[str, float] = {}
        for level, levelText in hierarchyTextByLevel.items():
            levelTierMatches = self._FindTieredTokenMatches(
                levelText,
                searchTermsByTier,
            )
            levelMatches = self._FlattenTierMatches(levelTierMatches)
            if not levelMatches:
                continue
            hierarchyLevelMatches[level] = levelMatches
            hierarchyLevelPoints[level] = self._ScoreTierMatches(
                levelTierMatches,
                TIER_DESCRIPTION_WEIGHTS,
            )

        return (
            descriptionTierMatches,
            hierarchyLevelMatches,
            hierarchyLevelPoints,
            descriptionPoints,
        )

    def _FindTieredCellMatches(
        self,
        cellValue: str,
        searchTextByTier: Mapping[str, str],
        searchTermsByTier: Mapping[str, Set[str]],
    ) -> Dict[str, List[str]]:
        tierMatches: Dict[str, List[str]] = {}
        alreadyMatchedTerms: Set[str] = set()
        for tier in [
            EVIDENCE_TIER_PRIMARY,
            EVIDENCE_TIER_SECONDARY,
            EVIDENCE_TIER_WEAK,
        ]:
            matches = [
                match
                for match in self._FindCellMatches(
                    cellValue,
                    searchTextByTier.get(tier, ""),
                    searchTermsByTier.get(tier, set()),
                )
                if match not in alreadyMatchedTerms
            ]
            tierMatches[tier] = matches
            alreadyMatchedTerms.update(matches)
        return tierMatches

    def _FindTieredTokenMatches(
        self,
        text: str,
        searchTermsByTier: Mapping[str, Set[str]],
    ) -> Dict[str, List[str]]:
        tierMatches: Dict[str, List[str]] = {}
        alreadyMatchedTerms: Set[str] = set()
        for tier in [
            EVIDENCE_TIER_PRIMARY,
            EVIDENCE_TIER_SECONDARY,
            EVIDENCE_TIER_WEAK,
        ]:
            matches = [
                match
                for match in self._FindTokenMatches(
                    text,
                    searchTermsByTier.get(tier, set()),
                )
                if match not in alreadyMatchedTerms
            ]
            tierMatches[tier] = matches
            alreadyMatchedTerms.update(matches)
        return tierMatches

    def _FlattenTierMatches(
        self,
        tierMatches: Mapping[str, Sequence[str]],
    ) -> List[str]:
        flattenedMatches: List[str] = []
        for tier in [
            EVIDENCE_TIER_PRIMARY,
            EVIDENCE_TIER_SECONDARY,
            EVIDENCE_TIER_WEAK,
        ]:
            for match in tierMatches.get(tier, []):
                if match not in flattenedMatches:
                    flattenedMatches.append(match)
        return flattenedMatches

    def _MergeTierMatches(
        self,
        *tierMatchGroups: Mapping[str, Sequence[str]],
    ) -> Dict[str, List[str]]:
        mergedMatches: Dict[str, List[str]] = {
            EVIDENCE_TIER_PRIMARY: [],
            EVIDENCE_TIER_SECONDARY: [],
            EVIDENCE_TIER_WEAK: [],
        }
        for tierMatchGroup in tierMatchGroups:
            for tier, matches in tierMatchGroup.items():
                tierValues = mergedMatches.setdefault(tier, [])
                for match in matches:
                    if match not in tierValues:
                        tierValues.append(match)
        return mergedMatches

    def _ScoreTierMatches(
        self,
        tierMatches: Mapping[str, Sequence[str]],
        tierWeights: Mapping[str, float],
    ) -> float:
        return sum(
            tierWeights.get(tier, 0.0) * len(matches)
            for tier, matches in tierMatches.items()
        )

    def _BuildExpandedSearchTerms(self, searchText: str) -> Set[str]:
        terms = set(self._ExtractTerms(searchText))
        loweredSearchText = searchText.lower()
        rawSearchTokens = {
            token.lower()
            for token in TOKEN_PATTERN.findall(searchText or "")
        }
        for sourceTerm, expandedTerms in TERM_EXPANSION_MAP.items():
            if not self._ShouldExpandSourceTerm(
                sourceTerm,
                loweredSearchText,
                rawSearchTokens,
            ):
                continue
            for expandedTerm in expandedTerms:
                terms.update(self._ExtractTerms(expandedTerm))
        return terms

    def _ShouldExpandSourceTerm(
        self,
        sourceTerm: str,
        loweredSearchText: str,
        rawSearchTokens: Set[str],
    ) -> bool:
        normalizedSourceTerm = sourceTerm.lower()
        if len(normalizedSourceTerm) <= 1:
            return normalizedSourceTerm in rawSearchTokens
        return normalizedSourceTerm in loweredSearchText

    def _FindCellMatches(
        self,
        cellValue: str,
        searchText: str,
        searchTerms: Set[str],
    ) -> List[str]:
        matchedTerms: List[str] = []
        normalizedSearchText = searchText.lower()
        for phrase in self._SplitKeywordCell(cellValue):
            phraseTerms = [
                term
                for term in self._ExtractTerms(phrase)
                if self._IsMatchableTerm(term)
            ]
            if not phraseTerms:
                continue
            normalizedPhrase = NormalizeWhiteSpace(phrase).lower()
            if normalizedPhrase and normalizedPhrase in normalizedSearchText:
                matchedTerms.append(normalizedPhrase)
                continue
            if all(term in searchTerms for term in phraseTerms):
                matchedTerms.append(normalizedPhrase)
        return sorted(set(matchedTerms))

    def _FindTokenMatches(self, text: str, searchTerms: Set[str]) -> List[str]:
        return sorted(
            term
            for term in set(self._ExtractTerms(text)).intersection(searchTerms)
            if self._IsMatchableTerm(term)
        )

    def _SplitKeywordCell(self, cellValue: str) -> List[str]:
        values: List[str] = []
        for rawPart in re.split(r"[;\n]", cellValue or ""):
            value = NormalizeWhiteSpace(rawPart).lower()
            if value:
                values.append(value)
        return values

    def _ExtractTerms(self, text: str) -> List[str]:
        return [
            token.lower()
            for token in TOKEN_PATTERN.findall(text or "")
            if len(token) >= 2 and not token.isdigit()
        ]

    def _IsMatchableTerm(self, term: str) -> bool:
        return term not in LOW_VALUE_MATCH_TERMS and not term.isdigit()


class Stage1RequestBuilder:
    """ProductClassificationInput과 CN 후보를 LLM 검토 요청으로 묶는다."""

    def __init__(
        self,
        systemPrompt: str = STAGE1_CLASSIFICATION_SYSTEM_PROMPT,
    ) -> None:
        self.systemPrompt = systemPrompt.strip()

    def BuildRequest(
        self,
        productInput: ProductClassificationInput,
        candidates: Sequence[CnCandidate],
        packagedContext: PackagedOntologyContext,
        evidencePackage: Optional[Stage1EvidencePackage] = None,
        userPrompt: Optional[str] = None,
    ) -> LlmRequest:
        promptCandidates = list(candidates)
        if not promptCandidates:
            raise ValueError(
                (
                    "Stage 1 classification request requires at least one CN "
                    "candidate. Stop before LLM request generation when candidate "
                    "retrieval returns no candidates."
                )
            )
        contextChunks = [
            self._BuildProductContextChunk(productInput),
            self._BuildCandidateContextChunk(promptCandidates),
        ]
        if evidencePackage is not None:
            contextChunks.append(
                self._BuildEvidencePackageContextChunk(
                    evidencePackage,
                    promptCandidates,
                )
            )
        else:
            contextChunks.extend(packagedContext.contextChunks)
        return LlmRequest(
            userPrompt=userPrompt
            or self._BuildDefaultUserPrompt(
                productInput,
                hasEvidencePackage=evidencePackage is not None,
            ),
            systemPrompt=self.systemPrompt,
            contextChunks=contextChunks,
            responseFormat=LlmResponseFormat.JSON_OBJECT,
            generationOptions=LlmGenerationOptions(
                temperature=0.0,
                maxTokens=DEFAULT_STAGE1_CLASSIFICATION_MAX_TOKENS,
            ),
        )

    def BuildOntologyQuery(
        self,
        productInput: ProductClassificationInput,
        candidates: Sequence[CnCandidate],
    ) -> str:
        candidateCodes = " ".join(candidate.hs8 for candidate in candidates)
        candidateHs6Codes = " ".join(
            candidate.hs6Code or ""
            for candidate in candidates
            if candidate.hs6Code is not None
        )
        return NormalizeWhiteSpace(
            " ".join(
                [
                    "stage1_classification HS6 CN8 candidate review",
                    productInput.productDomain,
                    " ".join(productInput.domainScopes),
                    productInput.productName or "",
                    candidateCodes,
                    candidateHs6Codes,
                    "cn_leaf_code_cards classification evidence human review",
                ]
            )
        )

    def _BuildProductContextChunk(
        self,
        productInput: ProductClassificationInput,
    ) -> str:
        productData = productInput.model_dump(mode="json", by_alias=True)
        productData["product_notice_text"] = productInput.productNoticeText
        productData["ocr_raw_text_policy"] = (
            "Raw OCR text is excluded from the default LLM request. "
            "Use normalized_ocr_fact_texts for OCR-derived product facts."
        )
        return "\n".join(
            [
                "[stage1_product_classification_input]",
                json.dumps(
                    productData,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ]
        )

    def _BuildCandidateContextChunk(
        self,
        candidates: Sequence[CnCandidate],
    ) -> str:
        return "\n".join(
            [
                "[stage1_cn_candidate_cards]",
                json.dumps(
                    [candidate.ToPromptDict() for candidate in candidates],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ]
        )

    def _BuildEvidencePackageContextChunk(
        self,
        evidencePackage: Stage1EvidencePackage,
        candidates: Sequence[CnCandidate],
    ) -> str:
        return "\n".join(
            [
                "[stage1_evidence_package]",
                "Use only evidence_id values from this package when returning evidence_refs.",
                (
                    "For every candidate_review status, cite at least one "
                    "evidence_id from candidate_citation_requirements.must_include_one_of "
                    "for the reviewed hs8."
                ),
                json.dumps(
                    evidencePackage.ToPromptDict(
                        candidateCodes=[candidate.hs8 for candidate in candidates],
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ]
        )

    def _BuildDefaultUserPrompt(
        self,
        productInput: ProductClassificationInput,
        hasEvidencePackage: bool,
    ) -> str:
        instructions = json.dumps(
            STAGE1_CLASSIFICATION_JSON_INSTRUCTIONS,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        evidenceInstructions = [
            (
                "evidence_refs에는 제공된 stage1_evidence_package의 "
                "evidence_id만 사용하라."
            ),
            (
                "모든 candidate_review는 상태와 무관하게 reviewed hs8과 "
                "candidate_hs8이 같은 후보 전용 evidence를 최소 1개 인용하라."
            ),
        ] if hasEvidencePackage else [
            (
                "stage1_evidence_package가 제공되지 않은 경우 evidence_refs는 "
                "빈 배열로 두고, 제공된 product facts와 context를 기준으로 "
                "reason을 작성하라."
            ),
        ]
        return "\n".join(
            [
                "아래 product facts와 CN candidate cards를 검토해 Stage 1 HS6/CN8 후보 검토 JSON을 작성하라.",
                "최종 법적/통관 판단으로 표현하지 말고, 후보별 가능성·배제 근거·부족 정보를 구분하라.",
                "각 후보의 code_hierarchy를 사용해 HS2, HS4, HS6, CN8 단계가 상품 정보와 논리적으로 이어지는지 검토하라.",
                "classification_rule_texts의 include_rule_keywords, exclude_rule_keywords, hard_conditions를 후보별 판단 근거로 검토하라.",
                "유사 EBTI 사례가 제공된 경우 similar_ebti_cases에 유사점과 차이점을 구분해 작성하라.",
                *evidenceInstructions,
                "상품명: {0}".format(productInput.productName or "unknown"),
                "응답 JSON 구조:",
                instructions,
            ]
        )


class Stage1ResponseValidationIssue(BaseModel):
    """Stage 1 LLM 응답 구조 검증 결과의 단일 이슈."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    severity: str
    issueCode: str = Field(alias="issue_code")
    fieldPath: str = Field(alias="field_path")
    message: str


class Stage1ResponseValidationReport(BaseModel):
    """Stage 1 LLM 응답이 후보 검토 JSON 계약을 만족하는지 나타낸다."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    isValid: bool = Field(alias="is_valid")
    parsedResponse: Dict[str, Any] = Field(
        default_factory=dict,
        alias="parsed_response",
    )
    issues: List[Stage1ResponseValidationIssue] = Field(default_factory=list)

    @computed_field(alias="error_count")
    @property
    def errorCount(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error")

    @computed_field(alias="warning_count")
    @property
    def warningCount(self) -> int:
        return sum(
            1
            for issue in self.issues
            if issue.severity == "warning"
        )


class Stage1PathLevelReviewPayload(BaseModel):
    """LLM 응답의 HS/CN 계층 단일 레벨 검토 payload."""

    model_config = ConfigDict(extra="ignore")

    code: Optional[StrictStr] = None
    consistency: Optional[StrictStr] = None
    comment: Optional[StrictStr] = None


class Stage1PathReviewPayload(BaseModel):
    """LLM 응답의 HS2/HS4/HS6/CN8 계층 검토 payload."""

    model_config = ConfigDict(extra="ignore")

    hs2: Optional[Stage1PathLevelReviewPayload] = None
    hs4: Optional[Stage1PathLevelReviewPayload] = None
    hs6: Optional[Stage1PathLevelReviewPayload] = None
    cn8: Optional[Stage1PathLevelReviewPayload] = None


class Stage1RuleReviewPayload(BaseModel):
    """LLM 응답의 분류 규칙 검토 payload."""

    model_config = ConfigDict(extra="ignore")

    include_rule_comment: Optional[StrictStr] = None
    exclude_rule_comment: Optional[StrictStr] = None
    hard_condition_comment: Optional[StrictStr] = None


class Stage1SimilarEbtiCasePayload(BaseModel):
    """LLM 응답의 유사 EBTI 사례 검토 payload."""

    model_config = ConfigDict(extra="ignore")

    evidence_ref: Optional[StrictStr] = None
    similarity_comment: Optional[StrictStr] = None
    difference_comment: Optional[StrictStr] = None


class Stage1CandidateReviewPayload(BaseModel):
    """LLM 응답의 후보별 검토 payload."""

    model_config = ConfigDict(extra="ignore")

    hs8: Optional[StrictStr] = None
    hs6_code: Optional[StrictStr] = None
    status: Optional[StrictStr] = None
    supporting_product_facts: Optional[List[StrictStr]] = None
    conflicting_or_exclusion_facts: Optional[List[StrictStr]] = None
    missing_information: Optional[List[StrictStr]] = None
    evidence_refs: Optional[List[StrictStr]] = None
    system_required_evidence_refs: List[StrictStr] = Field(default_factory=list)
    classification_path_review: Optional[Stage1PathReviewPayload] = None
    classification_rule_review: Optional[Stage1RuleReviewPayload] = None
    similar_ebti_cases: Optional[List[Stage1SimilarEbtiCasePayload]] = None
    reason: Optional[StrictStr] = None
    human_review_required: Optional[StrictBool] = None


class Stage1ClassificationResultPayload(BaseModel):
    """LLM 응답의 classification_result payload."""

    model_config = ConfigDict(extra="ignore")

    product_name: Optional[StrictStr] = None
    product_domain: Optional[StrictStr] = None
    domain_scopes: Optional[List[StrictStr]] = None
    candidate_reviews: Optional[List[Stage1CandidateReviewPayload]] = None
    not_enough_information: Optional[List[StrictStr]] = None
    recommended_next_action: Optional[StrictStr] = None
    human_review_warning: Optional[StrictStr] = None


class Stage1ClassificationResponsePayload(BaseModel):
    """LLM Stage 1 응답 root payload."""

    model_config = ConfigDict(extra="ignore")

    classification_result: Optional[Stage1ClassificationResultPayload] = None


class Stage1ResponseValidator:
    """LLM의 Stage 1 후보 검토 JSON 응답을 구조적으로 검증한다."""

    def ValidateResponse(
        self,
        llmResponse: LlmResponse,
        productInput: ProductClassificationInput,
        candidates: Sequence[CnCandidate],
        evidencePackage: Optional[Stage1EvidencePackage] = None,
    ) -> Stage1ResponseValidationReport:
        return self.ValidateText(
            llmResponse.generatedText,
            productInput,
            candidates,
            evidencePackage=evidencePackage,
        )

    def ValidateText(
        self,
        responseText: str,
        productInput: ProductClassificationInput,
        candidates: Sequence[CnCandidate],
        evidencePackage: Optional[Stage1EvidencePackage] = None,
    ) -> Stage1ResponseValidationReport:
        issues: List[Stage1ResponseValidationIssue] = []
        parsedResponse = self._ParseResponseText(responseText, issues)
        if parsedResponse is None:
            return Stage1ResponseValidationReport(
                isValid=False,
                parsedResponse={},
                issues=issues,
            )

        if evidencePackage is not None:
            self._AttachRequiredCandidateEvidenceRefs(
                parsedResponse,
                candidates,
                evidencePackage,
            )

        self._ValidateClassificationResult(
            parsedResponse,
            productInput,
            candidates,
            evidencePackage,
            issues,
        )
        self._DetectFinalDeterminationLanguage(responseText, issues)

        return Stage1ResponseValidationReport(
            isValid=not any(issue.severity == "error" for issue in issues),
            parsedResponse=parsedResponse,
            issues=issues,
        )

    def _AttachRequiredCandidateEvidenceRefs(
        self,
        parsedResponse: Dict[str, Any],
        candidates: Sequence[CnCandidate],
        evidencePackage: Stage1EvidencePackage,
    ) -> None:
        classificationResult = parsedResponse.get("classification_result")
        if not isinstance(classificationResult, dict):
            return
        candidateReviews = classificationResult.get("candidate_reviews")
        if not isinstance(candidateReviews, list):
            return

        expectedHs8Codes = {candidate.hs8 for candidate in candidates}
        validEvidenceIds = evidencePackage.validEvidenceIds
        for candidateReview in candidateReviews:
            if not isinstance(candidateReview, dict):
                continue
            hs8 = candidateReview.get("hs8")
            if not isinstance(hs8, str) or hs8 not in expectedHs8Codes:
                continue
            requiredEvidenceId = "cn_candidate:{0}".format(hs8)
            if requiredEvidenceId not in validEvidenceIds:
                continue
            if requiredEvidenceId not in evidencePackage.candidateEvidenceIds.get(
                hs8,
                [],
            ):
                continue

            systemRequiredEvidenceRefs = candidateReview.get(
                "system_required_evidence_refs",
            )
            if not isinstance(systemRequiredEvidenceRefs, list):
                systemRequiredEvidenceRefs = []
                candidateReview["system_required_evidence_refs"] = (
                    systemRequiredEvidenceRefs
                )
            if requiredEvidenceId in systemRequiredEvidenceRefs:
                continue
            systemRequiredEvidenceRefs.append(requiredEvidenceId)

    def _ParseResponseText(
        self,
        responseText: str,
        issues: List[Stage1ResponseValidationIssue],
    ) -> Optional[Dict[str, Any]]:
        strippedText = responseText.strip()
        if strippedText == "":
            self._AddIssue(
                issues,
                "error",
                "empty_response",
                "$",
                "LLM response text is empty.",
            )
            return None

        jsonText = self._ExtractJsonObjectText(strippedText)
        if jsonText is None:
            self._AddIssue(
                issues,
                "error",
                "json_object_not_found",
                "$",
                "LLM response does not contain a JSON object.",
            )
            return None

        try:
            parsedResponse = json.loads(jsonText)
        except json.JSONDecodeError as error:
            self._AddIssue(
                issues,
                "error",
                "invalid_json",
                "$",
                "LLM response is not valid JSON: {0}".format(error),
            )
            return None

        if not isinstance(parsedResponse, dict):
            self._AddIssue(
                issues,
                "error",
                "json_root_not_object",
                "$",
                "LLM response root must be a JSON object.",
            )
            return None

        try:
            responsePayload = Stage1ClassificationResponsePayload.model_validate(
                parsedResponse,
            )
        except ValidationError as error:
            for validationError in error.errors():
                location = validationError.get("loc", ())
                fieldPath = "$"
                if isinstance(location, tuple) and location:
                    fieldPath = "$." + ".".join(str(part) for part in location)
                self._AddIssue(
                    issues,
                    "error",
                    "invalid_response_schema",
                    fieldPath,
                    "LLM response field type is invalid: {0}".format(
                        validationError.get("msg", "unknown validation error"),
                    ),
                )
            return None

        return responsePayload.model_dump(mode="json")

    def _ExtractJsonObjectText(self, responseText: str) -> Optional[str]:
        directText = responseText.strip()
        if directText.startswith("{"):
            return directText

        fencedText = self._ExtractFencedJsonText(directText)
        if fencedText is not None:
            return fencedText

        return self._ExtractBalancedJsonObjectText(directText)

    def _ExtractFencedJsonText(self, responseText: str) -> Optional[str]:
        fencePattern = re.compile(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            re.IGNORECASE | re.DOTALL,
        )
        match = fencePattern.search(responseText)
        if match is None:
            return None
        return match.group(1).strip()

    def _ExtractBalancedJsonObjectText(self, responseText: str) -> Optional[str]:
        decoder = json.JSONDecoder()
        searchIndex = 0
        while searchIndex < len(responseText):
            objectStartIndex = responseText.find("{", searchIndex)
            if objectStartIndex < 0:
                return None

            try:
                parsedValue, objectEndIndex = decoder.raw_decode(
                    responseText[objectStartIndex:],
                )
            except json.JSONDecodeError:
                searchIndex = objectStartIndex + 1
                continue

            if isinstance(parsedValue, dict):
                return responseText[
                    objectStartIndex : objectStartIndex + objectEndIndex
                ].strip()

            searchIndex = objectStartIndex + 1

        return None

    def _ValidateClassificationResult(
        self,
        parsedResponse: Mapping[str, Any],
        productInput: ProductClassificationInput,
        candidates: Sequence[CnCandidate],
        evidencePackage: Optional[Stage1EvidencePackage],
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        classificationResult = parsedResponse.get("classification_result")
        if not isinstance(classificationResult, Mapping):
            self._AddIssue(
                issues,
                "error",
                "missing_classification_result",
                "$.classification_result",
                "Response must contain classification_result object.",
            )
            return

        self._ValidateProductIdentity(classificationResult, productInput, issues)
        self._ValidateCandidateReviews(
            classificationResult,
            candidates,
            evidencePackage,
            issues,
        )
        self._SanitizeContradictoryCandidateCoverageClaims(
            classificationResult,
            candidates,
            issues,
        )
        self._ValidateHumanReviewWarning(classificationResult, issues)

    def _ValidateProductIdentity(
        self,
        classificationResult: Mapping[str, Any],
        productInput: ProductClassificationInput,
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        productName = classificationResult.get("product_name")
        if not isinstance(productName, str) or productName.strip() == "":
            self._AddIssue(
                issues,
                "warning",
                "missing_product_name",
                "$.classification_result.product_name",
                "classification_result.product_name is missing or empty.",
            )

        productDomain = classificationResult.get("product_domain")
        expectedDomain = productInput.productDomain
        if productDomain != expectedDomain:
            self._AddIssue(
                issues,
                "warning",
                "product_domain_mismatch",
                "$.classification_result.product_domain",
                "Expected product_domain '{0}', got '{1}'.".format(
                    expectedDomain,
                    productDomain,
                ),
            )

    def _ValidateCandidateReviews(
        self,
        classificationResult: Mapping[str, Any],
        candidates: Sequence[CnCandidate],
        evidencePackage: Optional[Stage1EvidencePackage],
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        candidateReviews = classificationResult.get("candidate_reviews")
        if not isinstance(candidateReviews, list):
            self._AddIssue(
                issues,
                "error",
                "missing_candidate_reviews",
                "$.classification_result.candidate_reviews",
                "candidate_reviews must be a list.",
            )
            return

        candidateByHs8 = {candidate.hs8: candidate for candidate in candidates}
        expectedHs8Set = set(candidateByHs8.keys())
        reviewedHs8List: List[str] = []

        for index, candidateReview in enumerate(candidateReviews):
            fieldPath = "$.classification_result.candidate_reviews[{0}]".format(
                index,
            )
            if not isinstance(candidateReview, Mapping):
                self._AddIssue(
                    issues,
                    "error",
                    "candidate_review_not_object",
                    fieldPath,
                    "Each candidate review must be an object.",
                )
                continue

            hs8 = candidateReview.get("hs8")
            if not isinstance(hs8, str) or hs8.strip() == "":
                self._AddIssue(
                    issues,
                    "error",
                    "missing_candidate_hs8",
                    fieldPath + ".hs8",
                    "Candidate review must include hs8.",
                )
            elif hs8 not in expectedHs8Set:
                self._AddIssue(
                    issues,
                    "error",
                    "unknown_candidate_hs8",
                    fieldPath + ".hs8",
                    "Candidate review contains unknown hs8: {0}.".format(hs8),
                )
            else:
                reviewedHs8List.append(hs8)

            status = candidateReview.get("status")
            if status not in STAGE1_CLASSIFICATION_ALLOWED_STATUSES:
                self._AddIssue(
                    issues,
                    "error",
                    "invalid_candidate_status",
                    fieldPath + ".status",
                    "Candidate status is not one of the allowed values.",
                )

            humanReviewRequired = candidateReview.get("human_review_required")
            if humanReviewRequired is not True:
                self._AddIssue(
                    issues,
                    "error",
                    "human_review_required_not_true",
                    fieldPath + ".human_review_required",
                    "human_review_required must be true for every candidate.",
                )

            reason = candidateReview.get("reason")
            if not isinstance(reason, str) or reason.strip() == "":
                self._AddIssue(
                    issues,
                    "warning",
                    "missing_candidate_reason",
                    fieldPath + ".reason",
                    "Candidate review should include a reason.",
                )

            if isinstance(hs8, str) and hs8 in expectedHs8Set:
                self._ValidateEvidenceRefs(
                    candidateReview,
                    hs8,
                    evidencePackage,
                    fieldPath,
                    issues,
                )
                self._ValidateClassificationPathReview(
                    candidateReview,
                    candidateByHs8[hs8],
                    fieldPath,
                    issues,
                )
                self._ValidateClassificationRuleReview(
                    candidateReview,
                    fieldPath,
                    issues,
                )
                self._ValidateSimilarEbtiCases(
                    candidateReview,
                    hs8,
                    evidencePackage,
                    fieldPath,
                    issues,
                )

        self._ValidateCandidateCoverage(
            reviewedHs8List,
            expectedHs8Set,
            issues,
        )

    def _ValidateEvidenceRefs(
        self,
        candidateReview: Mapping[str, Any],
        hs8: str,
        evidencePackage: Optional[Stage1EvidencePackage],
        fieldPath: str,
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        if evidencePackage is None:
            return

        evidenceRefs = candidateReview.get("evidence_refs")
        if not isinstance(evidenceRefs, list):
            self._AddIssue(
                issues,
                "error",
                "missing_evidence_refs",
                fieldPath + ".evidence_refs",
                "Candidate review must include evidence_refs when evidence package is provided.",
            )
            return

        if not evidenceRefs:
            self._AddIssue(
                issues,
                "error",
                "empty_evidence_refs",
                fieldPath + ".evidence_refs",
                "Candidate review must cite at least one evidence_ref when evidence package is provided.",
            )
            return

        validEvidenceIds = evidencePackage.validEvidenceIds
        candidateEvidenceIds = set(evidencePackage.candidateEvidenceIds.get(hs8, []))
        candidateSpecificEvidenceIds = {
            evidenceRecord.evidenceId
            for evidenceRecord in evidencePackage.evidenceRecords
            if evidenceRecord.candidateHs8 == hs8
        }
        citedValidEvidenceIds: Set[str] = set()
        for evidenceIndex, evidenceRef in enumerate(evidenceRefs):
            evidencePath = "{0}.evidence_refs[{1}]".format(fieldPath, evidenceIndex)
            if not isinstance(evidenceRef, str) or evidenceRef.strip() == "":
                self._AddIssue(
                    issues,
                    "error",
                    "invalid_evidence_ref",
                    evidencePath,
                    "evidence_refs entries must be non-empty strings.",
                )
                continue
            if evidenceRef not in validEvidenceIds:
                self._AddIssue(
                    issues,
                    "error",
                    "unknown_evidence_ref",
                    evidencePath,
                    "Evidence ref is not in the provided evidence package: {0}.".format(
                        evidenceRef,
                    ),
                )
                continue
            citedValidEvidenceIds.add(evidenceRef)
            if evidenceRef not in candidateEvidenceIds:
                self._AddIssue(
                    issues,
                    "warning",
                    "candidate_unrelated_evidence_ref",
                    evidencePath,
                    "Evidence ref is valid but not mapped to candidate hs8 {0}: {1}.".format(
                        hs8,
                        evidenceRef,
                    ),
                )

        candidateSpecificCitations = citedValidEvidenceIds.intersection(
            candidateSpecificEvidenceIds,
        )
        status = candidateReview.get("status")
        if (
            status in {"strong_candidate", "possible_candidate"}
            and not candidateSpecificCitations
        ):
            self._AddIssue(
                issues,
                "error",
                "missing_candidate_specific_evidence_ref",
                fieldPath + ".evidence_refs",
                (
                    "strong_candidate or possible_candidate must cite at least "
                    "one candidate-specific evidence ref for hs8 {0}."
                ).format(hs8),
            )
        elif (
            status == "unlikely_candidate"
            and not candidateSpecificCitations
        ):
            self._AddIssue(
                issues,
                "warning",
                "missing_candidate_specific_evidence_ref",
                fieldPath + ".evidence_refs",
                (
                    "unlikely_candidate should cite candidate-specific evidence "
                    "when rejecting hs8 {0}."
                ).format(hs8),
            )

    def _ValidateCandidateCoverage(
        self,
        reviewedHs8List: Sequence[str],
        expectedHs8Set: Set[str],
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        reviewedHs8Set = set(reviewedHs8List)
        missingHs8Codes = sorted(expectedHs8Set.difference(reviewedHs8Set))
        duplicateHs8Codes = sorted(
            hs8
            for hs8 in reviewedHs8Set
            if reviewedHs8List.count(hs8) > 1
        )

        if missingHs8Codes:
            self._AddIssue(
                issues,
                "warning",
                "missing_candidate_reviews_for_input_codes",
                "$.classification_result.candidate_reviews",
                "Missing reviews for input hs8 codes: {0}.".format(
                    ", ".join(missingHs8Codes),
                ),
            )

        if duplicateHs8Codes:
            self._AddIssue(
                issues,
                "warning",
                "duplicate_candidate_reviews",
                "$.classification_result.candidate_reviews",
                "Duplicate candidate reviews for hs8 codes: {0}.".format(
                    ", ".join(duplicateHs8Codes),
                ),
            )

    def _SanitizeContradictoryCandidateCoverageClaims(
        self,
        classificationResult: Mapping[str, Any],
        candidates: Sequence[CnCandidate],
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        if not isinstance(classificationResult, dict):
            return

        providedCandidateCodes = self._BuildProvidedCandidateCodeSet(candidates)
        if not providedCandidateCodes:
            return

        self._SanitizeMissingInformationList(
            classificationResult,
            "not_enough_information",
            "$.classification_result.not_enough_information",
            providedCandidateCodes,
            issues,
        )

        candidateReviews = classificationResult.get("candidate_reviews")
        if not isinstance(candidateReviews, list):
            return

        for index, candidateReview in enumerate(candidateReviews):
            if not isinstance(candidateReview, dict):
                continue
            self._SanitizeMissingInformationList(
                candidateReview,
                "missing_information",
                "$.classification_result.candidate_reviews[{0}].missing_information".format(
                    index,
                ),
                providedCandidateCodes,
                issues,
            )

    def _BuildProvidedCandidateCodeSet(
        self,
        candidates: Sequence[CnCandidate],
    ) -> Set[str]:
        candidateCodes: Set[str] = set()
        for candidate in candidates:
            for value in [
                candidate.hs2Code,
                candidate.hs4Code,
                candidate.hs6Code,
                candidate.hs8Code,
                candidate.hs8,
            ]:
                if not isinstance(value, str):
                    continue
                normalizedCode = re.sub(r"\D", "", value)
                if normalizedCode:
                    candidateCodes.add(normalizedCode)
        return candidateCodes

    def _SanitizeMissingInformationList(
        self,
        targetPayload: Dict[str, Any],
        fieldName: str,
        fieldPath: str,
        providedCandidateCodes: Set[str],
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        missingInformation = targetPayload.get(fieldName)
        if not isinstance(missingInformation, list):
            return

        sanitizedValues: List[Any] = []
        for index, value in enumerate(missingInformation):
            if not isinstance(value, str):
                sanitizedValues.append(value)
                continue

            referencedCodes = self._ExtractReferencedCandidateCodes(value)
            matchedCodes = sorted(providedCandidateCodes.intersection(referencedCodes))
            if matchedCodes and self._IsCandidateCoverageMissingClaim(value):
                self._AddIssue(
                    issues,
                    "warning",
                    "contradictory_candidate_coverage_missing_information",
                    "{0}[{1}]".format(fieldPath, index),
                    (
                        "Missing-information item claims candidate coverage is "
                        "unavailable, but referenced code is already present in "
                        "provided candidates: {0}. Removed from parsed response."
                    ).format(", ".join(matchedCodes)),
                )
                continue

            sanitizedValues.append(value)

        targetPayload[fieldName] = sanitizedValues

    def _ExtractReferencedCandidateCodes(self, text: str) -> Set[str]:
        referencedCodes: Set[str] = set()
        for matchedText in CANDIDATE_COVERAGE_REFERENCE_PATTERN.findall(text):
            normalizedCode = re.sub(r"\D", "", matchedText)
            if 4 <= len(normalizedCode) <= 10:
                referencedCodes.add(normalizedCode)
        return referencedCodes

    def _IsCandidateCoverageMissingClaim(self, text: str) -> bool:
        normalizedText = NormalizeWhiteSpace(text).lower()
        hasCoverageTerm = any(
            coverageTerm.lower() in normalizedText
            for coverageTerm in CANDIDATE_COVERAGE_TERMS
        )
        hasMissingTerm = any(
            missingTerm.lower() in normalizedText
            for missingTerm in CANDIDATE_COVERAGE_MISSING_TERMS
        )
        return hasCoverageTerm and hasMissingTerm

    def _ValidateClassificationPathReview(
        self,
        candidateReview: Mapping[str, Any],
        candidate: CnCandidate,
        fieldPath: str,
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        pathReview = candidateReview.get("classification_path_review")
        pathFieldPath = fieldPath + ".classification_path_review"
        if not isinstance(pathReview, Mapping):
            self._AddIssue(
                issues,
                "error",
                "missing_classification_path_review",
                pathFieldPath,
                "Candidate review must include classification_path_review object.",
            )
            return

        expectedCodes = {
            "hs2": candidate.hs2Code,
            "hs4": candidate.hs4Code,
            "hs6": candidate.hs6Code,
            "cn8": candidate.hs8Code or candidate.hs8,
        }
        reviewedCodes: Dict[str, str] = {}

        for level in STAGE1_CLASSIFICATION_PATH_LEVELS:
            levelReview = pathReview.get(level)
            levelFieldPath = "{0}.{1}".format(pathFieldPath, level)
            if not isinstance(levelReview, Mapping):
                self._AddIssue(
                    issues,
                    "error",
                    "missing_classification_path_level_review",
                    levelFieldPath,
                    "classification_path_review must include {0} object.".format(
                        level,
                    ),
                )
                continue

            code = levelReview.get("code")
            if code is not None and not isinstance(code, str):
                self._AddIssue(
                    issues,
                    "error",
                    "invalid_classification_path_code",
                    levelFieldPath + ".code",
                    "Path review code must be string or null.",
                )
            elif isinstance(code, str) and code.strip() != "":
                normalizedCode = NormalizeWhiteSpace(code)
                reviewedCodes[level] = normalizedCode
                expectedCode = expectedCodes.get(level)
                if expectedCode is not None and normalizedCode != expectedCode:
                    self._AddIssue(
                        issues,
                        "warning",
                        "classification_path_candidate_code_mismatch",
                        levelFieldPath + ".code",
                        (
                            "Path review {0} code should match candidate code "
                            "{1}, got {2}."
                        ).format(level, expectedCode, normalizedCode),
                    )

            consistency = levelReview.get("consistency")
            if consistency not in STAGE1_CLASSIFICATION_PATH_ALLOWED_CONSISTENCIES:
                self._AddIssue(
                    issues,
                    "error",
                    "invalid_classification_path_consistency",
                    levelFieldPath + ".consistency",
                    (
                        "Path review consistency must be one of: {0}."
                    ).format(
                        ", ".join(
                            sorted(
                                STAGE1_CLASSIFICATION_PATH_ALLOWED_CONSISTENCIES,
                            )
                        ),
                    ),
                )

            comment = levelReview.get("comment")
            if not isinstance(comment, str) or comment.strip() == "":
                self._AddIssue(
                    issues,
                    "warning",
                    "missing_classification_path_comment",
                    levelFieldPath + ".comment",
                    "Path review should include a non-empty comment.",
                )

        for childLevel, parentLevel in [
            ("hs4", "hs2"),
            ("hs6", "hs4"),
            ("cn8", "hs6"),
        ]:
            childCode = reviewedCodes.get(childLevel)
            parentCode = reviewedCodes.get(parentLevel)
            if childCode is None or parentCode is None:
                continue
            if not childCode.startswith(parentCode):
                self._AddIssue(
                    issues,
                    "error",
                    "classification_path_prefix_mismatch",
                    "{0}.{1}.code".format(pathFieldPath, childLevel),
                    "{0} code {1} must start with {2} code {3}.".format(
                        childLevel,
                        childCode,
                        parentLevel,
                        parentCode,
                    ),
                )

    def _ValidateClassificationRuleReview(
        self,
        candidateReview: Mapping[str, Any],
        fieldPath: str,
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        ruleReview = candidateReview.get("classification_rule_review")
        ruleFieldPath = fieldPath + ".classification_rule_review"
        if not isinstance(ruleReview, Mapping):
            self._AddIssue(
                issues,
                "error",
                "missing_classification_rule_review",
                ruleFieldPath,
                "Candidate review must include classification_rule_review object.",
            )
            return

        for fieldName in [
            "include_rule_comment",
            "exclude_rule_comment",
            "hard_condition_comment",
        ]:
            value = ruleReview.get(fieldName)
            if not isinstance(value, str) or value.strip() == "":
                self._AddIssue(
                    issues,
                    "warning",
                    "missing_classification_rule_comment",
                    "{0}.{1}".format(ruleFieldPath, fieldName),
                    "{0} should be a non-empty string.".format(fieldName),
                )

    def _ValidateSimilarEbtiCases(
        self,
        candidateReview: Mapping[str, Any],
        hs8: str,
        evidencePackage: Optional[Stage1EvidencePackage],
        fieldPath: str,
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        similarCases = candidateReview.get("similar_ebti_cases")
        similarCasesFieldPath = fieldPath + ".similar_ebti_cases"
        if not isinstance(similarCases, list):
            self._AddIssue(
                issues,
                "warning",
                "missing_similar_ebti_cases",
                similarCasesFieldPath,
                "Candidate review should include similar_ebti_cases list.",
            )
            return

        if evidencePackage is None:
            return

        evidenceRecordsById: Dict[str, List[Stage1EvidenceRecord]] = {}
        for evidenceRecord in evidencePackage.evidenceRecords:
            evidenceRecordsById.setdefault(evidenceRecord.evidenceId, []).append(
                evidenceRecord,
            )
        for caseIndex, similarCase in enumerate(similarCases):
            caseFieldPath = "{0}[{1}]".format(similarCasesFieldPath, caseIndex)
            if not isinstance(similarCase, Mapping):
                self._AddIssue(
                    issues,
                    "error",
                    "similar_ebti_case_not_object",
                    caseFieldPath,
                    "similar_ebti_cases entries must be objects.",
                )
                continue

            evidenceRef = similarCase.get("evidence_ref")
            if not isinstance(evidenceRef, str) or evidenceRef.strip() == "":
                self._AddIssue(
                    issues,
                    "error",
                    "invalid_similar_ebti_evidence_ref",
                    caseFieldPath + ".evidence_ref",
                    "similar_ebti_cases evidence_ref must be a non-empty string.",
                )
                continue

            evidenceRecords = evidenceRecordsById.get(evidenceRef, [])
            if not evidenceRecords:
                self._AddIssue(
                    issues,
                    "error",
                    "unknown_similar_ebti_evidence_ref",
                    caseFieldPath + ".evidence_ref",
                    "similar EBTI evidence_ref is not in evidence package: {0}.".format(
                        evidenceRef,
                    ),
                )
                continue

            if not any(
                evidenceRecord.evidenceType == "bti_case_chunk"
                for evidenceRecord in evidenceRecords
            ):
                self._AddIssue(
                    issues,
                    "warning",
                    "similar_ebti_ref_not_bti_case",
                    caseFieldPath + ".evidence_ref",
                    "similar EBTI evidence_ref should point to bti_case_chunk.",
                )
            if not any(
                evidenceRecord.candidateHs8 == hs8
                for evidenceRecord in evidenceRecords
            ):
                self._AddIssue(
                    issues,
                    "warning",
                    "similar_ebti_ref_candidate_mismatch",
                    caseFieldPath + ".evidence_ref",
                    "similar EBTI evidence_ref is not mapped to candidate hs8 {0}.".format(
                        hs8,
                    ),
                )

            for fieldName in ["similarity_comment", "difference_comment"]:
                value = similarCase.get(fieldName)
                if not isinstance(value, str) or value.strip() == "":
                    self._AddIssue(
                        issues,
                        "warning",
                        "missing_similar_ebti_comment",
                        "{0}.{1}".format(caseFieldPath, fieldName),
                        "{0} should be a non-empty string.".format(fieldName),
                    )

    def _ValidateHumanReviewWarning(
        self,
        classificationResult: Mapping[str, Any],
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        humanReviewWarning = classificationResult.get("human_review_warning")
        if (
            not isinstance(humanReviewWarning, str)
            or humanReviewWarning.strip() == ""
        ):
            self._AddIssue(
                issues,
                "warning",
                "missing_human_review_warning",
                "$.classification_result.human_review_warning",
                "Response should include a human review warning.",
            )

    def _DetectFinalDeterminationLanguage(
        self,
        responseText: str,
        issues: List[Stage1ResponseValidationIssue],
    ) -> None:
        normalizedResponseText = responseText.lower()
        for warningTerm in FINAL_DETERMINATION_WARNING_TERMS:
            if warningTerm.lower() not in normalizedResponseText:
                continue
            self._AddIssue(
                issues,
                "warning",
                "final_determination_language_detected",
                "$",
                "Response contains final-determination language: {0}.".format(
                    warningTerm,
                ),
            )
            return

    def _AddIssue(
        self,
        issues: List[Stage1ResponseValidationIssue],
        severity: str,
        issueCode: str,
        fieldPath: str,
        message: str,
    ) -> None:
        issues.append(
            Stage1ResponseValidationIssue(
                severity=severity,
                issueCode=issueCode,
                fieldPath=fieldPath,
                message=message,
            )
        )
