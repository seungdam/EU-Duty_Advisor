"""binding 관측 집계기 — '축×필드×판정×정답' 행렬 축적 (학습 1단계).

원칙(설계자 지시): 구현만 해둔다 — 적용(binding-v2 자격 산출·모델 학습)은
프로젝트 후반. 런이 돌 때마다 이 집계기를 실행하면 학습 데이터가
공짜로 쌓인다.

원리: 모든 런의 decision_detail에 (cond축, 응답 필드(why의 field_hit),
verdict)가 남고, 답지와 대조하면 그 판정이 정답 코드를 지지했는지/
오답을 지지했는지가 라벨이 된다. 이 카운트가 binding-v2(자격쌍을
데이터가 결정)와 3단계(필드 선택 학습)의 훈련 행렬이다.

실행: python db/sources/binding_observatory.py
산출: data/binding_observation.jsonl (런 스탬프별 append)
"""
from __future__ import annotations

import csv
import glob
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = PROJECT_ROOT / "data" / "binding_observation.jsonl"
ANSWER_CSV = PROJECT_ROOT / "data" / "EU_HS_test.csv"


def _digits(v) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    if d and len(d) % 2 == 1:
        d = "0" + d
    return d


def _answers() -> dict[str, str]:
    out = {}
    try:
        for row in csv.reader(open(ANSWER_CSV, encoding="utf-8-sig")):
            url = next((c for c in row if "kurly" in str(c)), "")
            m = re.search(r"(?:products|goods)/([A-Za-z0-9]+)", url)
            code = next((c for c in row if re.fullmatch(r"\d{6,10}", str(c).strip())), "")
            if m and code:
                pid = m.group(1)
                out[pid if pid.isdigit() else f"global-{pid}"] = _digits(code)
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> int:
    answers = _answers()
    matrix: Counter = Counter()
    runs_seen = 0
    for d in sorted(glob.glob(str(PROJECT_ROOT / "artifacts/kurly-market-smoke/*/classification-smoke"))):
        pid = d.split("/")[-2]
        ans = answers.get(pid)
        if not ans:
            continue
        run_files = sorted(glob.glob(f"{d}/*/blackboard.json"))
        if not run_files:
            continue
        try:
            bb = json.load(open(run_files[-1]))
        except Exception:  # noqa: BLE001
            continue
        runs_seen += 1
        ccs = (bb.get("candidate_code_sets") or [{}])[-1]
        tr = ccs.get("classification_trace") or {}
        for stg in tr.get("stages") or []:
            level = str(stg.get("stage") or "")
            width = {"hs4": 4, "hs6": 6, "cn8": 8}.get(level)
            if not width:
                continue
            for cand in stg.get("candidates_considered") or []:
                code = _digits(cand.get("code"))
                supports_answer = ans[:width] == code[:width].ljust(width, "0")[:width] if code else False
                for det in cand.get("decision_detail") or []:
                    verdict = str(det.get("verdict") or "")
                    if verdict not in ("true", "false"):
                        continue
                    why = str(det.get("why") or "")
                    m = re.match(r"(?:field_hit|alias_hit):([a-z_,]+)", why)
                    fields = m.group(1).split(",") if m else ["(무필드)"]
                    for f in fields[:3]:
                        # 키: 축|필드|판정|정답지지 여부
                        matrix[(str(det.get("cond")), f, verdict,
                                "supports_answer" if supports_answer else "supports_wrong")] += 1
    stamp_rows = [
        {"axis": k[0], "field": k[1], "verdict": k[2], "label": k[3], "count": v}
        for k, v in sorted(matrix.items(), key=lambda x: -x[1])
    ]
    with open(OUT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"products": runs_seen, "cells": stamp_rows},
                           ensure_ascii=False) + "\n")
    print(f"관측: 상품 {runs_seen}건 → 행렬 셀 {len(stamp_rows)}개 → {OUT_PATH.name} append")
    for r in stamp_rows[:10]:
        print(f"  {r['axis']:<22} {r['field']:<22} {r['verdict']:<6} {r['label']:<15} {r['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
