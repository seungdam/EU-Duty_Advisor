"""Ontology frontmatter data_sources를 실제 파일 리소스로 검증하는 resolver."""

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, computed_field

from bussiness_logic.core.context_retrieval.schema import OntologyDocument
from bussiness_logic.utils import NormalizeWhiteSpace


class OntologyDataSourceCheck(BaseModel):
    """frontmatter data_sources 항목 하나에 대한 파일/컬럼 검증 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    resourceId: str = Field(alias="resource_id")
    documentId: str = Field(alias="document_id")
    relativeDocumentPath: str = Field(alias="relative_document_path")
    declaredPath: str = Field(alias="declared_path")
    resolvedPath: Optional[str] = Field(default=None, alias="resolved_path")
    exists: bool = False
    format: Optional[str] = None
    tableName: Optional[str] = Field(default=None, alias="table_name")
    primaryKey: Optional[str] = Field(default=None, alias="primary_key")
    requiredColumns: List[str] = Field(
        default_factory=list,
        alias="required_columns",
    )
    availableColumns: List[str] = Field(
        default_factory=list,
        alias="available_columns",
    )
    missingColumns: List[str] = Field(
        default_factory=list,
        alias="missing_columns",
    )
    rowCount: Optional[int] = Field(default=None, alias="row_count")
    primaryKeyPreview: List[str] = Field(
        default_factory=list,
        alias="primary_key_preview",
    )
    error: Optional[str] = None

    @computed_field(alias="is_loadable")
    @property
    def isLoadable(self) -> bool:
        return self.exists and self.error is None and len(self.missingColumns) == 0


class OntologyResourceResolutionReport(BaseModel):
    """core data_sources 전체 확인 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    dataSourceChecks: List[OntologyDataSourceCheck] = Field(
        default_factory=list,
        alias="data_source_checks",
    )

    @computed_field(alias="total_count")
    @property
    def totalCount(self) -> int:
        return len(self.dataSourceChecks)

    @computed_field(alias="loadable_count")
    @property
    def loadableCount(self) -> int:
        return sum(1 for check in self.dataSourceChecks if check.isLoadable)

    @computed_field(alias="missing_count")
    @property
    def missingCount(self) -> int:
        return sum(1 for check in self.dataSourceChecks if not check.exists)

    @computed_field(alias="invalid_count")
    @property
    def invalidCount(self) -> int:
        return sum(
            1
            for check in self.dataSourceChecks
            if check.exists and not check.isLoadable
        )

    @computed_field(alias="is_valid")
    @property
    def isValid(self) -> bool:
        return self.missingCount == 0 and self.invalidCount == 0


class OntologyResourceResolver:
    """frontmatter data_sources의 CSV 파일 존재와 컬럼 계약을 확인한다."""

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

    def Resolve(
        self,
        documents: Sequence[OntologyDocument],
    ) -> OntologyResourceResolutionReport:
        checks: List[OntologyDataSourceCheck] = []
        for document in documents:
            for dataSource in self._ReadDataSources(document):
                checks.append(self._CheckDataSource(document, dataSource))

        return OntologyResourceResolutionReport(dataSourceChecks=checks)

    def _CheckDataSource(
        self,
        document: OntologyDocument,
        dataSource: Dict[str, Any],
    ) -> OntologyDataSourceCheck:
        resourceId = self._ReadString(dataSource.get("resource_id")) or "unknown"
        declaredPath = self._ReadString(dataSource.get("path")) or ""
        resourceFormat = self._ReadString(dataSource.get("format"))
        tableName = self._ReadString(dataSource.get("table_name"))
        primaryKey = self._ReadString(dataSource.get("primary_key"))
        requiredColumns = self._ReadStringList(dataSource.get("required_columns"))
        resolvedPath = self._ResolvePath(declaredPath)

        if resolvedPath is None:
            return OntologyDataSourceCheck(
                resourceId=resourceId,
                documentId=document.documentId,
                relativeDocumentPath=document.relativePath,
                declaredPath=declaredPath,
                exists=False,
                format=resourceFormat,
                tableName=tableName,
                primaryKey=primaryKey,
                requiredColumns=requiredColumns,
                missingColumns=list(requiredColumns),
                error="data source file does not exist",
            )

        if resourceFormat != "csv":
            return OntologyDataSourceCheck(
                resourceId=resourceId,
                documentId=document.documentId,
                relativeDocumentPath=document.relativePath,
                declaredPath=declaredPath,
                resolvedPath=str(resolvedPath),
                exists=True,
                format=resourceFormat,
                tableName=tableName,
                primaryKey=primaryKey,
                requiredColumns=requiredColumns,
                error=f"unsupported data source format: {resourceFormat}",
            )

        return self._CheckCsvDataSource(
            document=document,
            resourceId=resourceId,
            declaredPath=declaredPath,
            resolvedPath=resolvedPath,
            resourceFormat=resourceFormat,
            tableName=tableName,
            primaryKey=primaryKey,
            requiredColumns=requiredColumns,
        )

    def _CheckCsvDataSource(
        self,
        document: OntologyDocument,
        resourceId: str,
        declaredPath: str,
        resolvedPath: Path,
        resourceFormat: Optional[str],
        tableName: Optional[str],
        primaryKey: Optional[str],
        requiredColumns: List[str],
    ) -> OntologyDataSourceCheck:
        try:
            with resolvedPath.open("r", encoding="utf-8-sig", newline="") as csvFile:
                reader = csv.DictReader(csvFile)
                availableColumns = list(reader.fieldnames or [])
                missingColumns = [
                    column
                    for column in requiredColumns
                    if column not in availableColumns
                ]
                primaryKeyPreview: List[str] = []
                rowCount = 0
                for row in reader:
                    rowCount += 1
                    if (
                        primaryKey is not None
                        and primaryKey in row
                        and len(primaryKeyPreview) < 5
                    ):
                        value = NormalizeWhiteSpace(row.get(primaryKey) or "")
                        if value:
                            primaryKeyPreview.append(value)

                if primaryKey is not None and primaryKey not in availableColumns:
                    missingColumns = [*missingColumns, primaryKey]

                return OntologyDataSourceCheck(
                    resourceId=resourceId,
                    documentId=document.documentId,
                    relativeDocumentPath=document.relativePath,
                    declaredPath=declaredPath,
                    resolvedPath=str(resolvedPath),
                    exists=True,
                    format=resourceFormat,
                    tableName=tableName,
                    primaryKey=primaryKey,
                    requiredColumns=requiredColumns,
                    availableColumns=availableColumns,
                    missingColumns=sorted(set(missingColumns)),
                    rowCount=rowCount,
                    primaryKeyPreview=primaryKeyPreview,
                )
        except Exception as error:
            return OntologyDataSourceCheck(
                resourceId=resourceId,
                documentId=document.documentId,
                relativeDocumentPath=document.relativePath,
                declaredPath=declaredPath,
                resolvedPath=str(resolvedPath),
                exists=True,
                format=resourceFormat,
                tableName=tableName,
                primaryKey=primaryKey,
                requiredColumns=requiredColumns,
                error=str(error),
            )

    def _ReadDataSources(
        self,
        document: OntologyDocument,
    ) -> List[Dict[str, Any]]:
        dataSources = document.frontmatter.get("data_sources")
        if not isinstance(dataSources, list):
            return []
        return [
            dataSource
            for dataSource in dataSources
            if isinstance(dataSource, dict)
        ]

    def _ResolvePath(self, declaredPath: str) -> Optional[Path]:
        if declaredPath == "":
            return None

        candidatePaths = [
            self.ontologyRootPath / declaredPath,
            self.projectRootPath / declaredPath,
        ]
        for candidatePath in candidatePaths:
            if candidatePath.exists():
                return candidatePath
        return None

    def _ReadString(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalizedValue = NormalizeWhiteSpace(value)
        return normalizedValue or None

    def _ReadStringList(self, value: Any) -> List[str]:
        if isinstance(value, str):
            normalizedValue = NormalizeWhiteSpace(value)
            return [normalizedValue] if normalizedValue else []
        if isinstance(value, list):
            values: List[str] = []
            for item in value:
                normalizedItem = self._ReadString(item)
                if normalizedItem is not None:
                    values.append(normalizedItem)
            return values
        return []
