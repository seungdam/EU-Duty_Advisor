"""Markdown 기반 core 문서 loader."""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bussiness_logic.core.context_retrieval.schema import OntologyDocument, OntologyDocumentKind
from bussiness_logic.utils import NormalizeWhiteSpace


DEFAULT_ONTOLOGY_EXTENSIONS = frozenset({".md", ".yaml", ".yml"})


class OntologyDocumentLoader:
    """core root 아래의 Markdown/YAML 문서를 읽어 표준 문서 객체로 변환한다."""

    def __init__(
        self,
        ontologyRootPath: str | Path,
        includeExtensions: Optional[Sequence[str]] = None,
    ) -> None:
        self.ontologyRootPath = Path(ontologyRootPath)
        self.includeExtensions = self._NormalizeExtensions(
            includeExtensions or DEFAULT_ONTOLOGY_EXTENSIONS,
        )

    def LoadDocuments(self) -> List[OntologyDocument]:
        documents: List[OntologyDocument] = []

        if not self.ontologyRootPath.exists():
            return documents

        for filePath in self._IterDocumentPaths():
            rawText = filePath.read_text(encoding="utf-8")
            frontmatter, bodyText = self._SplitFrontmatter(rawText)
            relativePath = filePath.relative_to(self.ontologyRootPath).as_posix()
            documentId = self._BuildDocumentId(relativePath, frontmatter)
            documentKind = self._DetectDocumentKind(filePath)
            title = self._ReadTitle(relativePath, bodyText, frontmatter)
            documents.append(
                OntologyDocument(
                    documentId=documentId,
                    sourcePath=str(filePath),
                    relativePath=relativePath,
                    title=title,
                    content=bodyText,
                    frontmatter=frontmatter,
                    documentKind=documentKind,
                ),
            )

        return documents

    def _IterDocumentPaths(self) -> Iterable[Path]:
        for filePath in sorted(self.ontologyRootPath.rglob("*")):
            if not filePath.is_file():
                continue
            if self._IsHiddenPath(filePath):
                continue
            if filePath.suffix.lower() not in self.includeExtensions:
                continue
            yield filePath

    def _SplitFrontmatter(self, rawText: str) -> Tuple[Dict[str, Any], str]:
        lines = rawText.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, rawText.strip()

        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                frontmatterText = "\n".join(lines[1:index])
                bodyText = "\n".join(lines[index + 1 :]).strip()
                return self._ParseFrontmatter(frontmatterText), bodyText

        return {}, rawText.strip()

    def _ParseFrontmatter(self, frontmatterText: str) -> Dict[str, Any]:
        parsedByYaml = self._ParseWithYamlIfAvailable(frontmatterText)
        if isinstance(parsedByYaml, dict):
            return parsedByYaml

        return self._ParseSimpleYamlSubset(frontmatterText)

    def _ParseWithYamlIfAvailable(self, frontmatterText: str) -> Optional[Any]:
        try:
            import yaml
        except Exception:
            return None

        try:
            return yaml.safe_load(frontmatterText) or {}
        except Exception:
            return None

    def _ParseSimpleYamlSubset(self, frontmatterText: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        currentKey: Optional[str] = None

        for rawLine in frontmatterText.splitlines():
            line = rawLine.rstrip()
            strippedLine = line.strip()
            if not strippedLine or strippedLine.startswith("#"):
                continue

            if strippedLine.startswith("- ") and currentKey is not None:
                currentValue = result.get(currentKey)
                if not isinstance(currentValue, list):
                    currentValue = []
                    result[currentKey] = currentValue
                currentValue.append(strippedLine[2:].strip())
                continue

            if ":" not in strippedLine:
                currentKey = None
                continue

            key, value = strippedLine.split(":", 1)
            currentKey = key.strip()
            value = value.strip()
            result[currentKey] = value if value else []

        return result

    def _ReadTitle(
        self,
        relativePath: str,
        bodyText: str,
        frontmatter: Dict[str, Any],
    ) -> str:
        frontmatterTitle = frontmatter.get("title")
        if isinstance(frontmatterTitle, str) and frontmatterTitle.strip():
            return NormalizeWhiteSpace(frontmatterTitle)

        for line in bodyText.splitlines():
            strippedLine = line.strip()
            if strippedLine.startswith("#"):
                return NormalizeWhiteSpace(strippedLine.lstrip("#"))

        return Path(relativePath).stem

    def _BuildDocumentId(
        self,
        relativePath: str,
        frontmatter: Dict[str, Any],
    ) -> str:
        frontmatterId = frontmatter.get("doc_id")
        if isinstance(frontmatterId, str) and frontmatterId.strip():
            return NormalizeWhiteSpace(frontmatterId)

        pathWithoutSuffix = str(Path(relativePath).with_suffix(""))
        return pathWithoutSuffix.replace("/", ".")

    def _DetectDocumentKind(self, filePath: Path) -> OntologyDocumentKind:
        extension = filePath.suffix.lower()
        if extension == ".md":
            return OntologyDocumentKind.MARKDOWN
        if extension in {".yaml", ".yml"}:
            return OntologyDocumentKind.YAML
        return OntologyDocumentKind.UNKNOWN

    def _IsHiddenPath(self, filePath: Path) -> bool:
        try:
            relativeParts = filePath.relative_to(self.ontologyRootPath).parts
        except ValueError:
            relativeParts = filePath.parts

        return any(part.startswith(".") for part in relativeParts)

    def _NormalizeExtensions(self, extensions: Sequence[str]) -> frozenset[str]:
        normalizedExtensions = set()
        for extension in extensions:
            normalizedExtension = extension.lower()
            if not normalizedExtension.startswith("."):
                normalizedExtension = f".{normalizedExtension}"
            normalizedExtensions.add(normalizedExtension)
        return frozenset(normalizedExtensions)
