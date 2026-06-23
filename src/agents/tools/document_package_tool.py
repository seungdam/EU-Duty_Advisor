"""
DocumentPackageTool — thin wrapper around the existing
``agents/document_package.py`` so Document_Agent can call it as a Tool.

The underlying ``document_package.get_document_package(taric10)`` already
returns a ``DocumentPackage`` dataclass with:
  - taric10 / cn8 / total_measure_rows
  - requirements[] (cert / declaration / license / ...)
  - verification_urls (Access2Markets, TARIC consultation)
  - data_source / notes

The new ``Document_Agent`` v2 schema (5-section card: 세관확인/기본관세/우대증빙/
요구서류/제품규제) is built BY THE AGENT from this raw resolver output —
not by this tool. The tool just preserves the existing investment.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

# Lazy import so cosmetic tooling (e.g. dash UI) does not pull psycopg2 if
# the deployment is CSV-only. agents/document_package.py decides.
from agents import document_package as _dp


class DocumentPackageTool:
    """Wrap the existing document_package resolver as a single-method tool."""

    def __init__(self, include_celex_excerpt: bool = False) -> None:
        self._include_celex_excerpt = include_celex_excerpt

    def resolve(
        self,
        *,
        taric10: str,
        product_facts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the raw DocumentPackage as a dict (dataclass-flattened).

        Caller (Document_Agent) decides how to bucket the rows into
        세관확인사항 / 기본관세 / 우대증빙 / 요구서류 / 제품규제 sections.
        """
        pkg = _dp.get_document_package(
            taric10,
            include_celex_excerpt=self._include_celex_excerpt,
            product_facts=product_facts,
        )
        return self._to_dict(pkg)

    @staticmethod
    def _to_dict(obj: Any) -> Any:
        if is_dataclass(obj):
            return {k: DocumentPackageTool._to_dict(v) for k, v in asdict(obj).items()}
        if isinstance(obj, list):
            return [DocumentPackageTool._to_dict(x) for x in obj]
        if isinstance(obj, dict):
            return {k: DocumentPackageTool._to_dict(v) for k, v in obj.items()}
        return obj
