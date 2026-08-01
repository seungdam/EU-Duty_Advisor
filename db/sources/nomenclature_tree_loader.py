"""Nomenclature EN.xlsx → 계층 트리 아티팩트 (다음 컴파일 배치의 원천).

원천 구조 (실측 2026-07-14):
  Goods code = 10자리 + 공백 + suffix 2자리 (총 13자)
    suffix 80        = declarable 라인 (실제 신고 가능 코드)
    suffix 10/20/... = 중간 그룹 헤더 (번호 없는 dash 행 — 자식 전체에
                       걸리는 조상 조건. 예: 1602491100-20
                       "Containing by weight 80% or more of meat")
  Indent = dash 문자열 → 트리 깊이의 유일한 진실
  End date 채워진 행 = 만료 코드 (기본 제외)

산출 (db/artifacts/):
  nomenclature_tree.jsonl — 행당 1노드:
    code10, suffix, depth, description, declarable,
    parent(code10-suffix), ancestor_conditions(중간 헤더 서술 체인),
    path_descriptions(루트→자기 서술 전체), cn8, taric10_leaf
  nomenclature_tree_summary.json — 검수 지표

실행:
  python db/nomenclature_tree_loader.py                 # 전체
  python db/nomenclature_tree_loader.py --chapters 16,19,21
  python db/nomenclature_tree_loader.py --verify        # pair_rows 정합 검사
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

DEFAULT_XLSX = (
    "/Users/snu/ASAP_mock_v1/_archive_from_ASAP_20260615_151351_pre_clean_"
    "experiment_workspace/data/TARIC/Nomenclature EN.xlsx"
)
PAIR_ROWS_CSV = "/Users/snu/ASAP/data/processed/cn_hs8_pair_rows.csv"
OUT_DIR = Path(__file__).resolve().parents[1] / "artifacts"


def _norm_ws(s: str) -> str:
    # 원천의 '80|%' 파이프 표기(비분리 구분자)를 일반 표기로 정규화
    return re.sub(r"\s+", " ", str(s or "").replace("|", " ")).strip()


def LoadNomenclatureTree(
    xlsx_path: str = DEFAULT_XLSX,
    chapters: set[str] | None = None,
    include_expired: bool = False,
) -> list[dict]:
    df = pd.read_excel(xlsx_path)
    nodes: list[dict] = []
    # 파일은 트리의 전위 순회 순서로 정렬되어 있다 — 스택으로 부모 복원
    stack: list[dict] = []  # depth 오름차순 유지
    for _, row in df.iterrows():
        gc = str(row.get("Goods code") or "")
        m = re.match(r"^(\d{10})\s+(\d{2})$", gc.strip())
        if not m:
            continue
        code10, suffix = m.group(1), m.group(2)
        if chapters and code10[:2] not in chapters:
            # 챕터 필터 중에도 스택 일관성은 유지해야 하므로 여기서 스택을
            # 건드리지 않는다 — 필터 챕터 진입 시 스택은 어차피 얕은
            # 루트(챕터 행)부터 다시 쌓인다.
            stack = [n for n in stack if n["code10"][:2] == code10[:2]]
            continue
        if not include_expired and pd.notna(row.get("End date")):
            continue
        depth = str(row.get("Indent") or "").count("-")
        desc = _norm_ws(row.get("Description"))
        while stack and stack[-1]["depth"] >= depth:
            stack.pop()
        parent = stack[-1] if stack else None
        node = {
            "code10": code10,
            "suffix": suffix,
            "depth": depth,
            "description": desc,
            "declarable": suffix == "80",
            "parent": f"{parent['code10']}-{parent['suffix']}" if parent else None,
            "ancestor_conditions": (
                (parent["ancestor_conditions"] if parent else [])
                + ([parent["description"]] if parent and not parent["declarable"] else [])
            ),
            "path_descriptions": (
                (parent["path_descriptions"] + [parent["description"]]) if parent else []
            ),
            "cn8": code10[:8],
            "taric10_leaf": suffix == "80" and code10[8:10] != "00",
        }
        nodes.append(node)
        stack.append(node)
    return nodes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=DEFAULT_XLSX)
    ap.add_argument("--chapters", default="", help="쉼표 구분 2자리 챕터 필터")
    ap.add_argument("--include-expired", action="store_true")
    ap.add_argument("--verify", action="store_true", help="pair_rows cn8 정합 검사")
    args = ap.parse_args()

    chapters = {c.strip().zfill(2) for c in args.chapters.split(",") if c.strip()} or None
    nodes = LoadNomenclatureTree(args.xlsx, chapters, args.include_expired)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "nomenclature_tree.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for n in nodes:
            f.write(json.dumps(n, ensure_ascii=False) + "\n")

    n_decl = sum(1 for n in nodes if n["declarable"])
    n_group = len(nodes) - n_decl
    n_t10 = sum(1 for n in nodes if n["taric10_leaf"])
    n_anc = sum(1 for n in nodes if n["declarable"] and n["ancestor_conditions"])
    summary = {
        "total_nodes": len(nodes),
        "declarable": n_decl,
        "group_headers": n_group,
        "taric10_leaves": n_t10,
        "declarable_with_ancestor_conditions": n_anc,
        "max_depth": max((n["depth"] for n in nodes), default=0),
        "chapters": sorted({n["code10"][:2] for n in nodes}),
    }

    if args.verify:
        pr = pd.read_csv(PAIR_ROWS_CSV, dtype=str, usecols=["cn8"])
        pr8 = {re.sub(r"\D", "", str(c))[:8] for c in pr["cn8"].dropna()}
        pr8.discard("")
        nom8 = {n["cn8"] for n in nodes if n["declarable"]}
        summary["verify"] = {
            "pair_rows_cn8": len(pr8),
            "nomenclature_declarable_cn8": len(nom8),
            "pair_rows_missing_in_tree": sorted(pr8 - nom8)[:20],
            "missing_count": len(pr8 - nom8),
        }

    with open(OUT_DIR / "nomenclature_tree_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n→ {out} ({len(nodes)}행)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
