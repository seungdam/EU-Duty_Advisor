"""cn_hs8_pair_rows.csv의 including/excluding 서술 → 레벨별 어휘 아티팩트.

including/excluding 텍스트는 HS 해설서 유래(당국 저자) — 어휘 갭의 무저자
후보다. 예: 1902의 "undried (moist or fresh) and frozen products"가
냉동 밀키트를 1902로 보증한다. 이 어휘를 임의 저작 없이 수확해
chapter/heading/subheading 단위 JSON으로 만든다.

수확 원칙 (synonym 임의 저작 금지 원칙과의 경계):
  - 원문 구문을 그대로 보존 — 우리가 단어를 만들지 않는다
  - 세미콜론/불릿 단위 구문 분해 + 공백 정규화만 수행
  - 판단(어느 구문이 어느 상품에 해당하는가)은 런타임 매칭층의 몫

산출: db/artifacts/heading_vocab.json
  {"chapter": {"16": {"including": [...], "excluding": [...]}, ...},
   "heading": {"1602": {...}, ...},
   "subheading": {"160249": {...}, ...}}

실행: python db/pair_rows_vocab_harvest.py
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

PAIR_ROWS_CSV = "/Users/snu/ASAP/data/processed/cn_hs8_pair_rows.csv"
OUT_DIR = Path(__file__).resolve().parents[1] / "artifacts"

LEVELS = (
    ("chapter", "chapter", 2),
    ("heading", "heading", 4),
    ("subheading", "subheading", 6),
)


def _phrases(text: str) -> list[str]:
    """세미콜론·개행·불릿 단위로 구문 분해 (원문 보존, 정규화만)."""
    out = []
    for seg in re.split(r"[;\n•·]|(?<=\.)\s{2,}", str(text or "")):
        seg = re.sub(r"\s+", " ", seg).strip(" .,-")
        if len(seg) >= 4 and not seg.lower().startswith(("nan", "none")):
            out.append(seg)
    return out


def main() -> int:
    df = pd.read_csv(PAIR_ROWS_CSV, dtype=str)
    vocab: dict[str, dict[str, dict[str, list[str]]]] = {
        lvl: defaultdict(lambda: {"including": [], "excluding": []}) for lvl, _, _ in LEVELS
    }
    for _, row in df.iterrows():
        for lvl, col, width in LEVELS:
            code = re.sub(r"\D", "", str(row.get(col) or ""))[:width]
            if not code or len(code) < width:
                continue
            bucket = vocab[lvl][code]
            for kind in ("including", "excluding"):
                for ph in _phrases(row.get(f"{col}_{kind}")):
                    if ph not in bucket[kind]:
                        bucket[kind].append(ph)

    result = {lvl: dict(v) for lvl, v in vocab.items()}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "heading_vocab.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    for lvl, v in result.items():
        n_inc = sum(len(b["including"]) for b in v.values())
        n_exc = sum(len(b["excluding"]) for b in v.values())
        filled = sum(1 for b in v.values() if b["including"] or b["excluding"])
        print(f"{lvl:<11} 코드 {len(v):>5}개 (어휘 보유 {filled:>5}) | including {n_inc:>6}구문 | excluding {n_exc:>6}구문")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
