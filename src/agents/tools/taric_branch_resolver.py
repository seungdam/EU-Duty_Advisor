"""
TaricBranchResolverTool — CN8 → 모든 가능한 TARIC10 branch 후보.

Owned by Classification_Agent. Pure deterministic lookup — no LLM. The Agent
calls this to enumerate the deterministic universe of TARIC10 lines under a
CN8 before asking the LLM to rank/select one.

Data source: Supabase ``taric_master_table``.

Output (per branch):
  TaricBranch(
    cn8="19023010", taric10="1902301010", line_id="1902301010:80",
    productline_suffix="10", branch_description="Dried, containing eggs",
    measure_type_summary=["Third country duty", "Veterinary control"],
    applies_to_origin_kr=True, is_declarable_leaf=True,
    needs_review=False,
  )
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bussiness_logic.app_config import LoadAppConfig
from agents.document_package import _connect_db, _release_db

PROJECT_ROOT = Path(os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[3])).resolve()
APP_CONFIG = LoadAppConfig(PROJECT_ROOT)
DEFAULT_MASTER_CSV = APP_CONFIG.paths.ResolvePath(
    PROJECT_ROOT,
    APP_CONFIG.paths.taric_master_table,
)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "t", "1", "yes", "y"}


@dataclass
class TaricBranch:
    cn8: str
    taric10: str
    line_id: str = ""
    productline_suffix: str = ""
    branch_description: str = ""
    measure_type_summary: list[str] = field(default_factory=list)
    applies_to_origin_kr: bool = True
    is_declarable_leaf: bool = True
    needs_review: bool = False
    measure_row_count: int = 0

    def to_dict(self) -> dict:
        return {
            "cn8": self.cn8,
            "taric10": self.taric10,
            "line_id": self.line_id,
            "productline_suffix": self.productline_suffix,
            "branch_description": self.branch_description,
            "measure_type_summary": list(self.measure_type_summary),
            "applies_to_origin_kr": self.applies_to_origin_kr,
            "is_declarable_leaf": self.is_declarable_leaf,
            "needs_review": self.needs_review,
            "measure_row_count": self.measure_row_count,
        }


@functools.lru_cache(maxsize=512)
def _load_rows_for_cn8(cn8: str) -> tuple[dict, ...]:
    """Rows from Supabase taric_master_table for one CN8. Cached per process."""
    conn = _connect_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM taric_master_table WHERE cn8 = %s", (cn8,))
        cols = [d[0] for d in cur.description]
        rows = [
            {str(k): "" if v is None else str(v) for k, v in zip(cols, row)}
            for row in cur.fetchall()
        ]
        cur.close()
        return tuple(rows)
    finally:
        _release_db(conn)


class TaricBranchResolverTool:
    """Enumerate all TARIC10 branches under a CN8.

    Usage:
        tool = TaricBranchResolverTool()
        branches = tool.resolve(cn8="19023010")
        # → list[TaricBranch]
    """

    def __init__(
        self,
        master_csv: Optional[Path] = None,
    ) -> None:
        self._master_csv = Path(master_csv) if master_csv else DEFAULT_MASTER_CSV

    def resolve(
        self,
        cn8: str,
        *,
        only_declarable_leaf: bool = False,
        only_kr_applicable: bool = False,
    ) -> list[TaricBranch]:
        """Return every distinct TARIC10 line under the given CN8."""
        cn8 = (cn8 or "").strip()
        if not cn8 or len(cn8) < 8:
            return []
        rows = list(_load_rows_for_cn8(cn8))
        if not rows:
            return []

        # Group by goods_code_10 (= TARIC10).
        by_taric10: dict[str, list[dict]] = {}
        for r in rows:
            t10 = (r.get("goods_code_10") or "").strip()
            if not t10:
                continue
            by_taric10.setdefault(t10, []).append(r)

        branches: list[TaricBranch] = []
        for t10, group in by_taric10.items():
            # Prefer a nomenclature_only row for descriptive fields when present.
            descriptor = next(
                (g for g in group if g.get("row_kind") == "nomenclature_only"),
                group[0],
            )
            measure_rows = [g for g in group if g.get("row_kind") == "measure_line"]

            measure_types: list[str] = []
            seen: set[str] = set()
            for m in measure_rows:
                mt = (m.get("measure_type_description") or "").strip()
                if mt and mt not in seen:
                    seen.add(mt)
                    measure_types.append(mt)

            branch = TaricBranch(
                cn8=cn8,
                taric10=t10,
                line_id=(descriptor.get("line_id") or "").strip(),
                productline_suffix=(descriptor.get("productline_suffix") or "").strip(),
                branch_description=(descriptor.get("leaf_description_en") or "").strip(),
                measure_type_summary=measure_types,
                applies_to_origin_kr=any(_truthy(m.get("applies_to_korea")) for m in measure_rows) if measure_rows else True,
                is_declarable_leaf=_truthy(descriptor.get("is_declarable_leaf")),
                needs_review=any(_truthy(g.get("needs_review")) for g in group),
                measure_row_count=len(measure_rows),
            )

            if only_declarable_leaf and not branch.is_declarable_leaf:
                continue
            if only_kr_applicable and not branch.applies_to_origin_kr:
                continue
            branches.append(branch)

        # Sort: declarable leaves first, then more measures first, then taric10.
        branches.sort(key=lambda b: (
            0 if b.is_declarable_leaf else 1,
            -b.measure_row_count,
            b.taric10,
        ))
        return branches
