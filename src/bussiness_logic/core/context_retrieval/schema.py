"""Ontology RAG 계층의 데이터 계약."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


class OntologyDocumentKind(str, Enum):
    """core loader가 읽은 원본 문서의 형식."""

    MARKDOWN = "markdown"
    YAML = "yaml"
    UNKNOWN = "unknown"


class OntologyDocument(BaseModel):
    """Markdown/YAML core 문서 하나를 나타내는 원본 단위."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    documentId: str = Field(alias="document_id")
    sourcePath: str = Field(alias="source_path")
    relativePath: str = Field(alias="relative_path")
    title: Optional[str] = None
    content: str = Field(default="", exclude=True)
    frontmatter: Dict[str, Any] = Field(default_factory=dict)
    documentKind: OntologyDocumentKind = Field(
        default=OntologyDocumentKind.UNKNOWN,
        alias="document_kind",
    )

    # 함수 호출 결과를 json 포맷에 직렬화 시킬 수 있도록 하는 문법
    @computed_field(alias="content_length")
    @property
    def contentLength(self) -> int:
        return len(self.content)


class OntologyChunk(BaseModel):
    """검색과 LLM 컨텍스트 주입에 사용할 문서 조각."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    chunkId: str = Field(alias="chunk_id")
    documentId: str = Field(alias="document_id")
    sourcePath: str = Field(alias="source_path")
    relativePath: str = Field(alias="relative_path")
    text: str = Field(exclude=True)
    headingPath: List[str] = Field(default_factory=list, alias="heading_path")
    tokenEstimate: int = Field(default=0, alias="token_estimate")
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def ToContextText(self) -> str:
        headingText = " > ".join(self.headingPath)
        headerLines = [
            f"[source] {self.relativePath}",
            f"[document_id] {self.documentId}",
        ]
        if headingText:
            headerLines.append(f"[heading] {headingText}")

        return "\n".join([*headerLines, "", self.text]).strip()

    @computed_field(alias="text_length")
    @property
    def textLength(self) -> int:
        return len(self.text)

    @computed_field(alias="text_preview")
    @property
    def textPreview(self) -> str:
        return self.text[:300]


class OntologyRetrievalResult(BaseModel):
    """검색 결과 청크와 랭킹 근거."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    chunk: OntologyChunk
    score: float
    matchedTerms: List[str] = Field(default_factory=list, alias="matched_terms")


class PackagedOntologyContext(BaseModel):
    """LLM 요청에 넣을 수 있도록 예산 안에 묶은 core context."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    contextChunks: List[str] = Field(
        default_factory=list,
        alias="context_chunks",
        exclude=True,
    )
    selectedResults: List[OntologyRetrievalResult] = Field(
        default_factory=list,
        alias="selected_results",
    )
    totalTokenEstimate: int = Field(default=0, alias="total_token_estimate")
    omittedResultCount: int = Field(default=0, alias="omitted_result_count")
    warnings: List[str] = Field(default_factory=list)

    @computed_field(alias="context_chunk_count")
    @property
    def contextChunkCount(self) -> int:
        return len(self.contextChunks)
