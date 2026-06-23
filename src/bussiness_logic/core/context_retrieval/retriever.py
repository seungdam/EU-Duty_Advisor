"""Ontology 문서에 대한 경량 검색기."""

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from bussiness_logic.core.context_retrieval.schema import (
    OntologyChunk,
    OntologyDocument,
    OntologyDocumentKind,
    OntologyRetrievalResult,
)
from bussiness_logic.utils import FindContainedTerms, NormalizeWhiteSpace


DEFAULT_MAX_CHUNK_CHARACTERS = 2200
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣_./-]+")
SKIPPED_MARKDOWN_SECTION_HEADINGS = frozenset(
    {
        "related files",
        "status",
    },
)


class OntologyRetriever:
    """초기 RAG 구현을 위한 deterministic keyword retriever."""

    def __init__(
        self,
        maxChunkCharacters: int = DEFAULT_MAX_CHUNK_CHARACTERS,
        includeMetadataChunks: bool = False,
    ) -> None:
        self.maxChunkCharacters = max(400, maxChunkCharacters)
        self.includeMetadataChunks = includeMetadataChunks

    def BuildChunks(self, documents: Sequence[OntologyDocument]) -> List[OntologyChunk]:
        chunks: List[OntologyChunk] = []
        for document in documents:
            chunks.extend(self._BuildDocumentChunks(document))
        return chunks

    def Retrieve(
        self,
        query: str,
        documents: Sequence[OntologyDocument],
        topK: int = 8,
    ) -> List[OntologyRetrievalResult]:
        chunks = self.BuildChunks(documents)
        return self.RetrieveFromChunks(query=query, chunks=chunks, topK=topK)

    def RetrieveFromChunks(
        self,
        query: str,
        chunks: Sequence[OntologyChunk],
        topK: int = 8,
    ) -> List[OntologyRetrievalResult]:
        queryTerms = self._ExtractTerms(query)
        if not queryTerms:
            return []

        results: List[OntologyRetrievalResult] = []
        queryPhrase = NormalizeWhiteSpace(query).lower()
        queryTermSet = set(queryTerms)

        for chunk in chunks:
            score, matchedTerms = self._ScoreChunk(
                chunk=chunk,
                queryPhrase=queryPhrase,
                queryTerms=queryTermSet,
            )
            if score <= 0:
                continue
            results.append(
                OntologyRetrievalResult(
                    chunk=chunk,
                    score=score,
                    matchedTerms=matchedTerms,
                ),
            )

        return sorted(
            results,
            key=lambda result: (
                -result.score,
                result.chunk.relativePath,
                result.chunk.chunkId,
            ),
        )[: max(0, topK)]

    def _BuildDocumentChunks(self, document: OntologyDocument) -> List[OntologyChunk]:
        chunks: List[OntologyChunk] = []
        pathBucket = self._ReadPathBucket(document.relativePath)
        metadataText = self._BuildFrontmatterSummaryText(document, pathBucket)

        if self.includeMetadataChunks and metadataText:
            chunks.extend(
                self._BuildTextChunks(
                    document=document,
                    headingPath=[
                        document.title or document.documentId,
                        "core metadata",
                    ],
                    text=metadataText,
                    chunkKind=f"{pathBucket}_metadata",
                    pathBucket=pathBucket,
                ),
            )

        if (
            document.documentKind == OntologyDocumentKind.YAML
            or pathBucket == "schema"
        ):
            chunks.extend(
                self._BuildTextChunks(
                    document=document,
                    headingPath=[document.title or document.documentId, "schema"],
                    text=document.content,
                    chunkKind="schema_content",
                    pathBucket=pathBucket,
                ),
            )
            return chunks

        sections = self._SplitMarkdownSections(document)
        for headingPath, sectionText in sections:
            chunks.extend(
                self._BuildTextChunks(
                    document=document,
                    headingPath=headingPath,
                    text=sectionText,
                    chunkKind=f"{pathBucket}_section",
                    pathBucket=pathBucket,
                ),
            )

        return chunks

    def _BuildTextChunks(
        self,
        document: OntologyDocument,
        headingPath: Sequence[str],
        text: str,
        chunkKind: str,
        pathBucket: str,
    ) -> List[OntologyChunk]:
        chunks: List[OntologyChunk] = []
        for partIndex, chunkText in enumerate(self._SplitLongText(text)):
            if not chunkText:
                continue
            chunks.append(
                self._BuildChunk(
                    document=document,
                    headingPath=headingPath,
                    text=chunkText,
                    chunkKind=chunkKind,
                    pathBucket=pathBucket,
                    partIndex=partIndex,
                ),
            )
        return chunks

    @staticmethod
    def _BuildChunk(
        document: OntologyDocument,
        headingPath: Sequence[str],
        text: str,
        chunkKind: str,
        pathBucket: str,
        partIndex: int,
    ) -> OntologyChunk:
        chunkSeed = (
            f"{document.documentId}:{chunkKind}:{'/'.join(headingPath)}:{partIndex}"
        )
        chunkId = hashlib.sha1(chunkSeed.encode("utf-8")).hexdigest()[:16]
        return OntologyChunk(
            chunkId=chunkId,
            documentId=document.documentId,
            sourcePath=document.sourcePath,
            relativePath=document.relativePath,
            text=text,
            headingPath=list(headingPath),
            tokenEstimate=max(1, len(text) // 4),
            metadata={
                "document_title": document.title,
                "doc_type": document.frontmatter.get("doc_type"),
                "authority_rank": document.frontmatter.get("authority_rank"),
                "tags": document.frontmatter.get("tags", []),
                "path_bucket": pathBucket,
                "chunk_kind": chunkKind,
            },
        )

    @staticmethod
    def _BuildFrontmatterSummaryText(
        document: OntologyDocument,
        pathBucket: str,
    ) -> str:
        if not document.frontmatter:
            return ""

        frontmatterSummary = {
            "document_id": document.documentId,
            "relative_path": document.relativePath,
            "path_bucket": pathBucket,
            "frontmatter": document.frontmatter,
        }
        return "\n".join(
            [
                "# Ontology metadata",
                json.dumps(frontmatterSummary, ensure_ascii=False, indent=2),
            ],
        )

    @staticmethod
    def _ReadPathBucket(relativePath: str) -> str:
        normalizedPath = relativePath.replace("\\", "/")
        if normalizedPath == "README.md":
            return "root_index"
        if normalizedPath.startswith("stage_contract/"):
            return "stage_contract"
        if normalizedPath.startswith("layers/"):
            return "layer"
        if normalizedPath.startswith("tables/"):
            return "table"
        if normalizedPath.startswith("schema/"):
            return "schema"
        return "supporting_document"

    @staticmethod
    def _SplitMarkdownSections(document: OntologyDocument) -> List[Tuple[List[str], str]]:
        if not document.content:
            return [([document.title or document.documentId], "")]

        sections: List[Tuple[List[str], List[str]]] = []
        headingStack: Dict[int, str] = {}
        currentHeadingPath = [document.title or document.documentId]
        currentLines: List[str] = []

        for line in document.content.splitlines():
            headingMatch = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
            if headingMatch is not None:
                if currentLines:
                    sections.append((currentHeadingPath, currentLines))
                level = len(headingMatch.group(1))
                headingText = NormalizeWhiteSpace(headingMatch.group(2))
                headingStack = {
                    headingLevel: heading
                    for headingLevel, heading in headingStack.items()
                    if headingLevel < level
                }
                headingStack[level] = headingText
                currentHeadingPath = [
                    headingStack[key] for key in sorted(headingStack.keys())
                ]
                currentLines = [line]
                continue

            currentLines.append(line)

        if currentLines:
            sections.append((currentHeadingPath, currentLines))

        return [
            (headingPath, "\n".join(lines).strip())
            for headingPath, lines in sections
            if "\n".join(lines).strip()
            and (
                not headingPath
                or headingPath[-1].lower() not in SKIPPED_MARKDOWN_SECTION_HEADINGS
            )
        ]

    def _SplitLongText(self, text: str) -> Iterable[str]:
        normalizedText = text.strip()
        if len(normalizedText) <= self.maxChunkCharacters:
            yield normalizedText
            return

        paragraphs = [paragraph.strip() for paragraph in normalizedText.split("\n\n")]
        currentParts: List[str] = []
        currentLength = 0

        for paragraph in paragraphs:
            nextLength = currentLength + len(paragraph) + 2
            if currentParts and nextLength > self.maxChunkCharacters:
                yield "\n\n".join(currentParts).strip()
                currentParts = []
                currentLength = 0

            if len(paragraph) > self.maxChunkCharacters:
                for index in range(0, len(paragraph), self.maxChunkCharacters):
                    yield paragraph[index : index + self.maxChunkCharacters].strip()
                continue

            currentParts.append(paragraph)
            currentLength += len(paragraph) + 2

        if currentParts:
            yield "\n\n".join(currentParts).strip()

    def _ScoreChunk(
        self,
        chunk: OntologyChunk,
        queryPhrase: str,
        queryTerms: set[str],
    ) -> Tuple[float, List[str]]:
        text = chunk.text.lower()
        headingText = " ".join(chunk.headingPath).lower()
        metadataText = " ".join(
            NormalizeWhiteSpace(str(value)).lower()
            for value in chunk.metadata.values()
        )
        pathText = chunk.relativePath.lower()
        matchedTerms = FindContainedTerms(
            " ".join([text, headingText, metadataText, pathText]),
            queryTerms,
        )

        if not matchedTerms:
            return 0.0, []

        score = 0.0
        if queryPhrase and queryPhrase in text:
            score += 10.0

        for term in matchedTerms:
            textCount = text.count(term)
            headingCount = headingText.count(term)
            metadataCount = metadataText.count(term)
            pathCount = pathText.count(term)
            score += min(textCount, 5) * 1.0
            score += headingCount * 3.0
            score += metadataCount * 2.0
            score += pathCount * 1.5

        authorityRank = self._ReadAuthorityRank(chunk.metadata.get("authority_rank"))
        if authorityRank is not None:
            score += max(0.0, 5.0 - authorityRank) * 0.2

        return score, matchedTerms

    @staticmethod
    def _ExtractTerms(query: str) -> List[str]:
        terms: List[str] = []
        for match in TOKEN_PATTERN.finditer(query.lower()):
            term = match.group(0).strip()
            if not term:
                continue
            if len(term) == 1 and not term.isdigit():
                continue
            terms.append(term)
        return sorted(set(terms))

    @staticmethod
    def _ReadAuthorityRank(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (float, int)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None
