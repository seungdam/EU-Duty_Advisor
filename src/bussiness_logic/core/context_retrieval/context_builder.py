"""Ontology 문서 로드, 검색, context 포장을 묶는 외부용 facade."""

from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from bussiness_logic.core.context_retrieval.context import ContextPackager
from bussiness_logic.core.context_retrieval.loader import OntologyDocumentLoader
from bussiness_logic.core.context_retrieval.retriever import OntologyRetriever
from bussiness_logic.core.context_retrieval.schema import (
    OntologyChunk,
    OntologyDocument,
    PackagedOntologyContext,
)
from bussiness_logic.utils import NormalizeWhiteSpace


ACTIVE_DOCUMENT_STATUS = "active"
EXCLUDED_RAG_POLICIES = frozenset(
    {
        "exclude",
        "exclude_by_default",
        "never",
    },
)
REFERENCE_DOCUMENT_TYPES = frozenset(
    {
        "design_note",
        "frontmatter_design",
        "internal_note",
    },
)


class OntologyContextBuilder:
    """core RAG context를 한 번에 구성하는 공개 진입점."""

    def __init__(
        self,
        ontologyRootPath: str | Path,
        documentLoader: Optional[OntologyDocumentLoader] = None,
        retriever: Optional[OntologyRetriever] = None,
        contextPackager: Optional[ContextPackager] = None,
    ) -> None:
        self.ontologyRootPath = Path(ontologyRootPath)
        self.documentLoader = documentLoader or OntologyDocumentLoader(
            self.ontologyRootPath,
        )
        self.retriever = retriever or OntologyRetriever()
        self.contextPackager = contextPackager or ContextPackager()
        self._documents: Optional[List[OntologyDocument]] = None
        self._chunksByRetrievalKey: dict[
            Tuple[Optional[str], bool, bool],
            List[OntologyChunk],
        ] = {}

    def BuildContext(
        self,
        query: str,
        phaseId: Optional[str] = None,
        topK: int = 8,
        maxResultCount: int = 8,
        includeReferenceDocuments: bool = False,
        includeInactiveDocuments: bool = False,
    ) -> PackagedOntologyContext:
        chunks = self._LoadChunks(
            phaseId=phaseId,
            includeReferenceDocuments=includeReferenceDocuments,
            includeInactiveDocuments=includeInactiveDocuments,
        )
        retrievalResults = self.retriever.RetrieveFromChunks(
            query=query,
            chunks=chunks,
            topK=topK,
        )
        return self.contextPackager.Package(
            retrievalResults=retrievalResults,
            maxResultCount=maxResultCount,
        )

    def LoadDocuments(self, forceReload: bool = False) -> Sequence[OntologyDocument]:
        if forceReload:
            self._documents = None
            self._chunksByRetrievalKey = {}

        return list(self._LoadDocuments())

    def LoadRetrievalDocuments(
        self,
        phaseId: Optional[str] = None,
        includeReferenceDocuments: bool = False,
        includeInactiveDocuments: bool = False,
    ) -> Sequence[OntologyDocument]:
        return list(
            self._FilterDocumentsForRetrieval(
                documents=self._LoadDocuments(),
                phaseId=phaseId,
                includeReferenceDocuments=includeReferenceDocuments,
                includeInactiveDocuments=includeInactiveDocuments,
            ),
        )

    def _LoadDocuments(self) -> List[OntologyDocument]:
        if self._documents is None:
            self._documents = self.documentLoader.LoadDocuments()
        return self._documents

    def _LoadChunks(
        self,
        phaseId: Optional[str],
        includeReferenceDocuments: bool,
        includeInactiveDocuments: bool,
    ) -> List[OntologyChunk]:
        retrievalKey = (
            phaseId,
            includeReferenceDocuments,
            includeInactiveDocuments,
        )
        if retrievalKey not in self._chunksByRetrievalKey:
            self._chunksByRetrievalKey[retrievalKey] = self.retriever.BuildChunks(
                self.LoadRetrievalDocuments(
                    phaseId=phaseId,
                    includeReferenceDocuments=includeReferenceDocuments,
                    includeInactiveDocuments=includeInactiveDocuments,
                ),
            )
        return self._chunksByRetrievalKey[retrievalKey]

    def _FilterDocumentsForRetrieval(
        self,
        documents: Sequence[OntologyDocument],
        phaseId: Optional[str],
        includeReferenceDocuments: bool,
        includeInactiveDocuments: bool,
    ) -> List[OntologyDocument]:
        return [
            document
            for document in documents
            if self._CanUseDocumentForRetrieval(
                document=document,
                phaseId=phaseId,
                includeReferenceDocuments=includeReferenceDocuments,
                includeInactiveDocuments=includeInactiveDocuments,
            )
        ]

    def _CanUseDocumentForRetrieval(
        self,
        document: OntologyDocument,
        phaseId: Optional[str],
        includeReferenceDocuments: bool,
        includeInactiveDocuments: bool,
    ) -> bool:
        frontmatter = document.frontmatter
        if not frontmatter:
            return includeReferenceDocuments

        ragPolicy = self._ReadOptionalString(frontmatter.get("rag_policy"))
        if (
            ragPolicy in EXCLUDED_RAG_POLICIES
            and not includeReferenceDocuments
        ):
            return False

        documentType = self._ReadOptionalString(frontmatter.get("doc_type"))
        if (
            documentType in REFERENCE_DOCUMENT_TYPES
            and not includeReferenceDocuments
        ):
            return False

        status = self._ReadOptionalString(frontmatter.get("status"))
        if (
            not includeInactiveDocuments
            and status is not None
            and status != ACTIVE_DOCUMENT_STATUS
        ):
            return False

        if phaseId is None:
            return True

        activePhases = self._ReadStringList(frontmatter.get("active_in_phase"))
        return phaseId in activePhases

    @staticmethod
    def _ReadOptionalString(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalizedValue = NormalizeWhiteSpace(value).lower()
        return normalizedValue or None

    @staticmethod
    def _ReadStringList(value: Any) -> List[str]:
        if isinstance(value, str):
            normalizedValue = NormalizeWhiteSpace(value)
            return [normalizedValue] if normalizedValue else []
        if isinstance(value, list):
            values: List[str] = []
            for item in value:
                if not isinstance(item, str):
                    continue
                normalizedItem = NormalizeWhiteSpace(item)
                if normalizedItem:
                    values.append(normalizedItem)
            return values
        return []
