"""CN table row를 semantic retrieval용 chunk로 정규화한다."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, computed_field

from bussiness_logic.bridge import TextEmbeddingAdapter, TextEmbeddingRequest
from bussiness_logic.utils import NormalizeWhiteSpace, NormalizeWhitespaceLines


SEMANTIC_CHUNK_TYPE_HIERARCHY = "hierarchy"
SEMANTIC_CHUNK_TYPE_KEYWORD = "keyword"
SEMANTIC_CHUNK_TYPE_EXPLANATORY_NOTE = "explanatory_note"


class CnSemanticChunk(BaseModel):
    """CN 후보 row의 특정 의미 영역을 embedding할 단위."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    candidateCode: str = Field(alias="candidate_code")
    domainScope: str = Field(alias="domain_scope")
    chunkType: str = Field(alias="chunk_type")
    text: str
    sourceFields: List[str] = Field(default_factory=list, alias="source_fields")

    @computed_field(alias="chunk_id")
    @property
    def chunkId(self) -> str:
        return "{0}:{1}".format(self.candidateCode, self.chunkType)

    @computed_field(alias="text_length")
    @property
    def textLength(self) -> int:
        return len(self.text)


class CnSemanticChunkMatch(BaseModel):
    """semantic 검색에서 후보에 매칭된 chunk 점수."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    chunkId: str = Field(alias="chunk_id")
    chunkType: str = Field(alias="chunk_type")
    score: float
    sourceFields: List[str] = Field(default_factory=list, alias="source_fields")


class CnSemanticSearchHit(BaseModel):
    """semantic 검색으로 발견한 CN 후보."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    candidateCode: str = Field(alias="candidate_code")
    domainScope: str = Field(alias="domain_scope")
    score: float
    bestChunkType: str = Field(alias="best_chunk_type")
    matchedChunks: List[CnSemanticChunkMatch] = Field(
        default_factory=list,
        alias="matched_chunks",
    )


class CnSemanticChunkBuilder:
    """cn_table.csv row를 hierarchy/keyword/explanatory note chunk로 분리한다."""

    def BuildChunksForRows(
        self,
        rows: Sequence[Mapping[str, str]],
        domainScope: str,
    ) -> List[CnSemanticChunk]:
        chunks: List[CnSemanticChunk] = []
        for row in rows:
            chunks.extend(self.BuildChunksForRow(row, domainScope))
        return chunks

    def BuildChunksForRow(
        self,
        row: Mapping[str, str],
        domainScope: str,
    ) -> List[CnSemanticChunk]:
        candidateCode = self._Read(row, "cn", "hs8")
        if not candidateCode:
            return []

        chunks = [
            self._BuildHierarchyChunk(row, domainScope, candidateCode),
            self._BuildKeywordChunk(row, domainScope, candidateCode),
            self._BuildExplanatoryNoteChunk(row, domainScope, candidateCode),
        ]
        return [chunk for chunk in chunks if chunk is not None]

    def _BuildHierarchyChunk(
        self,
        row: Mapping[str, str],
        domainScope: str,
        candidateCode: str,
    ) -> CnSemanticChunk | None:
        subheadingDescription = self._Read(row, "subheading_description", "hs6_description")
        if not subheadingDescription and self._Read(row, "cn_part") == "00":
            subheadingDescription = self._Read(row, "cn_description", "hs8_description")

        lines = [
            self._BuildCodeLine(
                "chapter",
                self._Read(row, "chapter", "hs2_code"),
                self._Read(row, "chapter_description", "hs2_description"),
            ),
            self._BuildCodeLine(
                "heading",
                self._Read(row, "heading", "hs4_code"),
                self._Read(row, "heading_description", "hs4_description"),
            ),
            self._BuildCodeLine(
                "subheading",
                self._Read(row, "subheading", "hs6_code"),
                subheadingDescription,
            ),
            self._BuildCodeLine(
                "cn",
                candidateCode,
                self._Read(row, "cn_description", "hs8_description"),
            ),
            self._BuildPlainLine(
                "combined_description",
                self._Read(row, "combined_description"),
            ),
            self._BuildPlainLine("branch_context", self._Read(row, "branch_context")),
        ]
        return self._BuildChunk(
            candidateCode=candidateCode,
            domainScope=domainScope,
            chunkType=SEMANTIC_CHUNK_TYPE_HIERARCHY,
            textLines=lines,
            sourceFields=[
                "chapter_description",
                "heading_description",
                "subheading_description",
                "cn_description",
                "combined_description",
                "branch_context",
            ],
        )

    def _BuildKeywordChunk(
        self,
        row: Mapping[str, str],
        domainScope: str,
        candidateCode: str,
    ) -> CnSemanticChunk | None:
        keywordFields = [
            "search_keywords",
            "cn_keywords",
            "subheading_keywords",
            "heading_keywords",
            "chapter_keywords",
            "branch_keywords",
        ]
        keywords: List[str] = []
        for fieldName in keywordFields:
            keywords.extend(self._SplitKeywordCell(self._Read(row, fieldName)))

        deduplicatedKeywords = []
        seenKeywords = set()
        for keyword in keywords:
            if keyword in seenKeywords:
                continue
            seenKeywords.add(keyword)
            deduplicatedKeywords.append(keyword)

        return self._BuildChunk(
            candidateCode=candidateCode,
            domainScope=domainScope,
            chunkType=SEMANTIC_CHUNK_TYPE_KEYWORD,
            textLines=["; ".join(deduplicatedKeywords)],
            sourceFields=keywordFields,
        )

    def _BuildExplanatoryNoteChunk(
        self,
        row: Mapping[str, str],
        domainScope: str,
        candidateCode: str,
    ) -> CnSemanticChunk | None:
        lines = [
            self._BuildPlainLine(
                "cn_explanatory_note",
                self._Read(row, "cn_explanatory_note"),
            ),
            self._BuildPlainLine("cn_note_keywords", self._Read(row, "cn_note_keywords")),
        ]
        return self._BuildChunk(
            candidateCode=candidateCode,
            domainScope=domainScope,
            chunkType=SEMANTIC_CHUNK_TYPE_EXPLANATORY_NOTE,
            textLines=lines,
            sourceFields=["cn_explanatory_note", "cn_note_keywords"],
        )

    def _BuildChunk(
        self,
        candidateCode: str,
        domainScope: str,
        chunkType: str,
        textLines: Sequence[str],
        sourceFields: Sequence[str],
    ) -> CnSemanticChunk | None:
        text = NormalizeWhitespaceLines(
            "\n".join(line for line in textLines if line.strip())
        )
        if not text:
            return None
        return CnSemanticChunk(
            candidateCode=candidateCode,
            domainScope=domainScope,
            chunkType=chunkType,
            text=text,
            sourceFields=list(sourceFields),
        )

    def _BuildCodeLine(self, label: str, code: str, description: str) -> str:
        normalizedCode = NormalizeWhiteSpace(code)
        normalizedDescription = NormalizeWhiteSpace(description)
        if normalizedCode and normalizedDescription:
            return "{0}: {1} - {2}".format(
                label,
                normalizedCode,
                normalizedDescription,
            )
        if normalizedCode:
            return "{0}: {1}".format(label, normalizedCode)
        if normalizedDescription:
            return "{0}: {1}".format(label, normalizedDescription)
        return ""

    def _BuildPlainLine(self, label: str, value: str) -> str:
        normalizedValue = NormalizeWhiteSpace(value)
        if not normalizedValue:
            return ""
        return "{0}: {1}".format(label, normalizedValue)

    def _Read(self, row: Mapping[str, Any], *fieldNames: str) -> str:
        for fieldName in fieldNames:
            value = row.get(fieldName)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    def _SplitKeywordCell(self, value: str) -> List[str]:
        return [
            NormalizeWhiteSpace(rawKeyword).lower()
            for rawKeyword in value.split(";")
            if NormalizeWhiteSpace(rawKeyword)
        ]


class CnSemanticCandidateIndex:
    """CN semantic chunk를 embedding해 in-memory 후보 검색을 수행한다."""

    def __init__(
        self,
        embeddingAdapter: TextEmbeddingAdapter,
        chunkBuilder: CnSemanticChunkBuilder | None = None,
    ) -> None:
        self.embeddingAdapter = embeddingAdapter
        self.chunkBuilder = chunkBuilder or CnSemanticChunkBuilder()
        self.indexedChunks: List[CnSemanticChunk] = []
        self.indexedEmbeddings: List[List[float]] = []

    @property
    def chunkCount(self) -> int:
        return len(self.indexedChunks)

    def Build(
        self,
        rowsByDomainScope: Mapping[str, Sequence[Mapping[str, str]]],
    ) -> None:
        chunks: List[CnSemanticChunk] = []
        for domainScope, rows in rowsByDomainScope.items():
            chunks.extend(self.chunkBuilder.BuildChunksForRows(rows, domainScope))

        self.indexedChunks = chunks
        self.indexedEmbeddings = []
        if not chunks:
            return

        response = self.embeddingAdapter.EmbedTexts(
            TextEmbeddingRequest(texts=[chunk.text for chunk in chunks])
        )
        if len(response.embeddings) != len(chunks):
            raise ValueError(
                "Embedding response count does not match semantic chunk count."
            )
        self.indexedEmbeddings = response.embeddings

    def Search(
        self,
        queryText: str,
        domainScopes: Sequence[str],
        topK: int,
        minScore: float = 0.0,
        maxMatchedChunksPerCandidate: int = 3,
    ) -> List[CnSemanticSearchHit]:
        normalizedQueryText = NormalizeWhitespaceLines(queryText)
        if not normalizedQueryText or topK <= 0 or not self.indexedChunks:
            return []

        response = self.embeddingAdapter.EmbedTexts(
            TextEmbeddingRequest(texts=[normalizedQueryText])
        )
        if not response.embeddings:
            return []

        domainScopeSet = set(domainScopes)
        queryEmbedding = response.embeddings[0]
        matchesByCandidate: Dict[str, List[CnSemanticChunkMatch]] = {}
        candidateDomainScope: Dict[str, str] = {}

        for chunk, embedding in zip(self.indexedChunks, self.indexedEmbeddings):
            if domainScopeSet and chunk.domainScope not in domainScopeSet:
                continue
            score = _ComputeCosineSimilarity(queryEmbedding, embedding)
            if score < minScore:
                continue
            matchesByCandidate.setdefault(chunk.candidateCode, []).append(
                CnSemanticChunkMatch(
                    chunkId=chunk.chunkId,
                    chunkType=chunk.chunkType,
                    score=round(score, 6),
                    sourceFields=chunk.sourceFields,
                )
            )
            candidateDomainScope[chunk.candidateCode] = chunk.domainScope

        hits: List[CnSemanticSearchHit] = []
        for candidateCode, chunkMatches in matchesByCandidate.items():
            sortedChunkMatches = sorted(
                chunkMatches,
                key=lambda match: (-match.score, match.chunkId),
            )
            bestMatch = sortedChunkMatches[0]
            hits.append(
                CnSemanticSearchHit(
                    candidateCode=candidateCode,
                    domainScope=candidateDomainScope.get(candidateCode, ""),
                    score=bestMatch.score,
                    bestChunkType=bestMatch.chunkType,
                    matchedChunks=sortedChunkMatches[:maxMatchedChunksPerCandidate],
                )
            )

        return sorted(
            hits,
            key=lambda hit: (-hit.score, hit.domainScope, hit.candidateCode),
        )[:topK]


def _ComputeCosineSimilarity(
    leftVector: Sequence[float],
    rightVector: Sequence[float],
) -> float:
    if not leftVector or not rightVector or len(leftVector) != len(rightVector):
        return 0.0

    dotProduct = 0.0
    leftNorm = 0.0
    rightNorm = 0.0
    for leftValue, rightValue in zip(leftVector, rightVector):
        dotProduct += leftValue * rightValue
        leftNorm += leftValue * leftValue
        rightNorm += rightValue * rightValue

    if leftNorm <= 0.0 or rightNorm <= 0.0:
        return 0.0
    return dotProduct / (math.sqrt(leftNorm) * math.sqrt(rightNorm))
