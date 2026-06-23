"""Ontology frontmatter와 참조 관계 검증기."""

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from pydantic import BaseModel, ConfigDict, Field, computed_field

from bussiness_logic.core.context_retrieval.schema import OntologyDocument
from bussiness_logic.utils import NormalizeWhiteSpace


class OntologyValidationSeverity(str, Enum):
    """core validation issue의 심각도."""

    ERROR = "error"
    WARNING = "warning"


class OntologyValidationIssue(BaseModel):
    """core 문서 검증 중 발견된 문제."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    severity: OntologyValidationSeverity
    issueCode: str = Field(alias="issue_code")
    message: str
    relativePath: Optional[str] = Field(default=None, alias="relative_path")
    documentId: Optional[str] = Field(default=None, alias="document_id")
    fieldName: Optional[str] = Field(default=None, alias="field_name")


class OntologyValidationReport(BaseModel):
    """core validation 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    issues: List[OntologyValidationIssue] = Field(default_factory=list)

    @computed_field(alias="error_count")
    @property
    def errorCount(self) -> int:
        return sum(
            1
            for issue in self.issues
            if issue.severity == OntologyValidationSeverity.ERROR
        )

    @computed_field(alias="warning_count")
    @property
    def warningCount(self) -> int:
        return sum(
            1
            for issue in self.issues
            if issue.severity == OntologyValidationSeverity.WARNING
        )

    @computed_field(alias="is_valid")
    @property
    def isValid(self) -> bool:
        return self.errorCount == 0


class OntologyGraphValidator:
    """Markdown core package의 graph metadata를 검증한다."""

    REQUIRED_FRONTMATTER_FIELDS = frozenset(
        {
            "doc_id",
            "doc_type",
            "title",
            "status",
            "path",
        },
    )
    DOC_REFERENCE_FIELDS = frozenset(
        {
            "active_tables",
            "declares_layers",
            "declares_stages",
            "declares_tables",
            "depends_on",
            "read_after",
            "related_docs",
            "tables",
            "uses_layers",
        },
    )
    FILE_REFERENCE_FIELDS = frozenset(
        {
            "related_files",
        },
    )

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

    def Validate(
        self,
        documents: Sequence[OntologyDocument],
    ) -> OntologyValidationReport:
        issues: List[OntologyValidationIssue] = []
        documentsById = self._BuildDocumentIdIndex(documents, issues)

        for document in documents:
            issues.extend(self._ValidateDocumentFrontmatter(document))
            issues.extend(self._ValidateDocumentReferences(document, documentsById))
            issues.extend(self._ValidateFileReferences(document))

        issues.extend(self._ValidateDependsOnCycles(documentsById))

        return OntologyValidationReport(issues=issues)

    def _BuildDocumentIdIndex(
        self,
        documents: Sequence[OntologyDocument],
        issues: List[OntologyValidationIssue],
    ) -> Dict[str, OntologyDocument]:
        documentsById: Dict[str, OntologyDocument] = {}
        for document in documents:
            documentId = self._ReadOptionalString(document.frontmatter.get("doc_id"))
            if documentId is None:
                continue
            if documentId in documentsById:
                issues.append(
                    self._BuildIssue(
                        severity=OntologyValidationSeverity.ERROR,
                        issueCode="duplicate_doc_id",
                        message=f"Duplicate core doc_id: {documentId}",
                        document=document,
                        fieldName="doc_id",
                    ),
                )
                continue
            documentsById[documentId] = document
        return documentsById

    def _ValidateDocumentFrontmatter(
        self,
        document: OntologyDocument,
    ) -> List[OntologyValidationIssue]:
        issues: List[OntologyValidationIssue] = []
        if self._IsSchemaDocument(document):
            return issues

        if not document.frontmatter:
            return [
                self._BuildIssue(
                    severity=OntologyValidationSeverity.WARNING,
                    issueCode="missing_frontmatter",
                    message="Document has no machine-readable frontmatter.",
                    document=document,
                ),
            ]

        for fieldName in sorted(self.REQUIRED_FRONTMATTER_FIELDS):
            if self._IsMissingValue(document.frontmatter.get(fieldName)):
                issues.append(
                    self._BuildIssue(
                        severity=OntologyValidationSeverity.ERROR,
                        issueCode="missing_required_frontmatter_field",
                        message=f"Required frontmatter field is missing: {fieldName}",
                        document=document,
                        fieldName=fieldName,
                    ),
                )

        declaredPath = self._ReadOptionalString(document.frontmatter.get("path"))
        if declaredPath is not None and declaredPath != document.relativePath:
            issues.append(
                self._BuildIssue(
                    severity=OntologyValidationSeverity.ERROR,
                    issueCode="frontmatter_path_mismatch",
                    message=(
                        "Frontmatter path does not match document path: "
                        f"{declaredPath} != {document.relativePath}"
                    ),
                    document=document,
                    fieldName="path",
                ),
            )

        status = self._ReadOptionalString(document.frontmatter.get("status"))
        activePhases = self._ReadStringList(
            document.frontmatter.get("active_in_phase"),
        )
        if status == "active" and not activePhases:
            issues.append(
                self._BuildIssue(
                    severity=OntologyValidationSeverity.WARNING,
                    issueCode="active_document_without_phase",
                    message="Active document has no active_in_phase values.",
                    document=document,
                    fieldName="active_in_phase",
                ),
            )

        return issues

    def _ValidateDocumentReferences(
        self,
        document: OntologyDocument,
        documentsById: Dict[str, OntologyDocument],
    ) -> List[OntologyValidationIssue]:
        if not document.frontmatter:
            return []

        issues: List[OntologyValidationIssue] = []
        for fieldName in sorted(self.DOC_REFERENCE_FIELDS):
            for referenceId in self._ReadReferenceIds(
                document.frontmatter.get(fieldName),
            ):
                if referenceId not in documentsById:
                    issues.append(
                        self._BuildIssue(
                            severity=OntologyValidationSeverity.ERROR,
                            issueCode="broken_document_reference",
                            message=(
                                f"Document reference does not resolve: {referenceId}"
                            ),
                            document=document,
                            fieldName=fieldName,
                        ),
                    )
        return issues

    def _ValidateFileReferences(
        self,
        document: OntologyDocument,
    ) -> List[OntologyValidationIssue]:
        if not document.frontmatter:
            return []

        issues: List[OntologyValidationIssue] = []
        for fieldName in sorted(self.FILE_REFERENCE_FIELDS):
            for relativeFilePath in self._ReadReferenceIds(
                document.frontmatter.get(fieldName),
            ):
                if not self._DoesReferencedFileExist(relativeFilePath):
                    issues.append(
                        self._BuildIssue(
                            severity=OntologyValidationSeverity.WARNING,
                            issueCode="missing_related_file",
                            message=(
                                "Related file path does not exist from core root: "
                                f"{relativeFilePath}"
                            ),
                            document=document,
                            fieldName=fieldName,
                        ),
                    )
        return issues

    def _DoesReferencedFileExist(self, relativeFilePath: str) -> bool:
        return (
            (self.ontologyRootPath / relativeFilePath).exists()
            or (self.projectRootPath / relativeFilePath).exists()
        )

    def _IsSchemaDocument(self, document: OntologyDocument) -> bool:
        normalizedPath = document.relativePath.replace("\\", "/")
        return normalizedPath.startswith("schema/") and normalizedPath.endswith(
            (".yaml", ".yml"),
        )

    def _ValidateDependsOnCycles(
        self,
        documentsById: Dict[str, OntologyDocument],
    ) -> List[OntologyValidationIssue]:
        graph = {
            documentId: self._ReadReferenceIds(
                document.frontmatter.get("depends_on"),
            )
            for documentId, document in documentsById.items()
        }
        issues: List[OntologyValidationIssue] = []
        visiting: Set[str] = set()
        visited: Set[str] = set()

        def Visit(documentId: str, path: List[str]) -> None:
            if documentId in visiting:
                cyclePath = [*path, documentId]
                document = documentsById.get(documentId)
                issues.append(
                    self._BuildIssue(
                        severity=OntologyValidationSeverity.ERROR,
                        issueCode="depends_on_cycle",
                        message="depends_on cycle detected: "
                        + " -> ".join(cyclePath),
                        document=document,
                        fieldName="depends_on",
                    ),
                )
                return

            if documentId in visited:
                return

            visiting.add(documentId)
            for nextDocumentId in graph.get(documentId, []):
                if nextDocumentId in documentsById:
                    Visit(nextDocumentId, [*path, documentId])
            visiting.remove(documentId)
            visited.add(documentId)

        for documentId in documentsById:
            Visit(documentId, [])

        return issues

    def _ReadReferenceIds(self, value: Any) -> List[str]:
        if isinstance(value, str):
            normalizedValue = NormalizeWhiteSpace(value)
            return [normalizedValue] if normalizedValue else []
        if isinstance(value, list):
            references: List[str] = []
            for item in value:
                if isinstance(item, str):
                    normalizedItem = NormalizeWhiteSpace(item)
                    if normalizedItem:
                        references.append(normalizedItem)
            return references
        return []

    def _ReadStringList(self, value: Any) -> List[str]:
        return self._ReadReferenceIds(value)

    def _ReadOptionalString(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalizedValue = NormalizeWhiteSpace(value)
        return normalizedValue or None

    def _IsMissingValue(self, value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return NormalizeWhiteSpace(value) == ""
        if isinstance(value, list):
            return len(value) == 0
        return False

    def _BuildIssue(
        self,
        severity: OntologyValidationSeverity,
        issueCode: str,
        message: str,
        document: Optional[OntologyDocument],
        fieldName: Optional[str] = None,
    ) -> OntologyValidationIssue:
        return OntologyValidationIssue(
            severity=severity,
            issueCode=issueCode,
            message=message,
            relativePath=document.relativePath if document is not None else None,
            documentId=document.documentId if document is not None else None,
            fieldName=fieldName,
        )
