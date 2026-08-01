"""관세청 표준품명 XLSX → 한↔영 법정 어휘 사전 아티팩트.

원천: data/관세청_표준품명_*.xlsx (공공데이터포털, 연 1회 갱신, 26,873행)
  표준품명_한글 ↔ 표준품명_영문 (+HS부호 10자리, 필수규격, 유효기간)

용도: '용어 브리지'의 결정론 1층 — composition entries의 한글 성분명을
법정 영문 어휘로 잇는다 (LLM 0회, 창작 0 — 관세청 공포 쌍의 색인).
소비자(다음 배치): quant term_aliases, entries 영문 병기, (2차) 라우팅 힌트.

산출: data/std_name_dictionary.jsonl — {ko, en, hs6, hs10, spec_ko, spec_en}
런타임 브리지는 이 파일을 로드해 2-gram 역색인을 메모리에 구축한다.

실행: python db/sources/std_name_dictionary.py [--probe 돼지등갈비]
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = PROJECT_ROOT / "data" / "std_name_dictionary.jsonl"

_GENERIC_KO = {"기타", "그밖의것", "기타의것"}


def _nfc(t: object) -> str:
    return unicodedata.normalize("NFC", str(t or "")).strip()


def build() -> list[dict]:
    import pandas as pd
    # macOS NFD 파일명은 NFC 글롭에 안 걸린다 — 전체 xlsx를 NFC 대조로 탐색
    src = sorted(
        f for f in PROJECT_ROOT.glob("data/*.xlsx")
        if "표준품명" in unicodedata.normalize("NFC", f.name)
    )
    if not src:
        raise SystemExit("data/ 안에 표준품명 xlsx 없음")
    df = pd.read_excel(src[-1])
    rows: list[dict] = []
    seen: set[tuple] = set()
    for _, r in df.iterrows():
        ko = _nfc(r.get("표준품명_한글"))
        en = _nfc(r.get("표준품명_영문"))
        if not ko or not en or ko in _GENERIC_KO or en.lower() == "other":
            continue
        hs10 = re.sub(r"\D", "", _nfc(r.get("HS부호"))).zfill(10)
        key = (ko, en, hs10[:6])
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "ko": ko, "en": en,
            "hs6": hs10[:6], "hs10": hs10,
            "spec_ko": _nfc(r.get("필수규격_한글")) or None,
            "spec_en": _nfc(r.get("필수규격_영문")) or None,
        })
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({k: v for k, v in row.items() if v}, ensure_ascii=False) + "\n")
    return rows


def _bigrams(text: str) -> set[str]:
    ko = re.sub(r"[^가-힣]", "", text)
    return {ko[i:i + 2] for i in range(len(ko) - 1)}


def lookup(rows: list[dict], query: str, top: int = 3) -> list[tuple]:
    """2-gram 겹침 점수 top-k — 런타임 브리지와 동일 판정 (검증용 구현)."""
    q = _bigrams(_nfc(query))
    if not q:
        return []
    scored = []
    for r in rows:
        g = _bigrams(r["ko"])
        if not g:
            continue
        inter = len(q & g)
        if inter:
            scored.append((inter / max(1, len(q | g)), inter, r))
    scored.sort(key=lambda x: (-x[1], -x[0]))
    return [(round(s, 2), i, r["ko"], r["en"], r["hs6"]) for s, i, r in scored[:top]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", default="")
    args = ap.parse_args()
    rows = build()
    hs2 = {r["hs6"][:2] for r in rows}
    print(f"사전 {len(rows)}쌍 → {OUT_PATH.name} | 챕터 커버 {len(hs2)}개")
    if args.probe:
        for hit in lookup(rows, args.probe):
            print("  ", hit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
