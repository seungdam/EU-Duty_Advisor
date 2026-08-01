"""ECHA 규제물질 목록 → Supabase `echa_substance_screen` 적재.

원천 (아카이브 정리본, 2026-04~06 스냅샷):
  restriction_list_*.xlsx   REACH Annex XVII 제한물질 (조건·규제 결과)
  svhc_identification_*.xlsx SVHC 후보목록
  authorisation_list_*.xlsx  허가 대상(Annex XIV)

용도: COA(cas_no)·INCI(성분명) entries와 CAS/명칭 조인 → 수출불가/제한
스크리닝. post_taric_requirement_master의 external_dataset_ids가 참조
선언해둔 자리의 실데이터. 분류 파이프라인과 무접점(요건 단계 전용).

테이블은 drop-and-recreate라 재실행 안전. 실행은 설계자 직접:
  PYTHONPATH=src python db/uploaders/load_echa_chem.py
"""
from __future__ import annotations

import re
from pathlib import Path

ECHA_DIR = Path(
    "/Users/snu/ASAP_mock_v1/_archive_from_ASAP_20260615_151351_pre_clean"
    "_experiment_workspace/data/ECHA_CHEM"
)
SOURCES = (
    ("restriction", "restriction_list_full-*.xlsx",
     {"conditions": "Conditions", "outcome": "Regulatory outcome"}),
    ("svhc_candidate", "svhc_identification_full-*.xlsx",
     {"stage": "Current stage"}),
    ("authorisation", "authorisation_list_full-*.xlsx", {}),
)


def main() -> int:
    import pandas as pd
    from sqlalchemy import text
    from db.db_session_manager import DbSessionManager

    rows: list[dict] = []
    for source_list, pattern, extra_cols in SOURCES:
        paths = sorted(ECHA_DIR.glob(pattern))
        if not paths:
            print(f"  ! {pattern}: 파일 없음 — 건너뜀")
            continue
        df = pd.read_excel(paths[-1])
        col = {c.lower().strip(): c for c in df.columns}
        for _, r in df.iterrows():
            name = str(r.get(col.get("substance name", ""), "") or "").strip()
            cas = str(r.get(col.get("cas number", ""), "") or "").strip()
            ec = str(r.get(col.get("ec number", ""), "") or "").strip()
            if not name and not cas:
                continue
            detail = "; ".join(
                f"{k}={str(r.get(cn, '') or '')[:180]}"
                for k, cn in ((k, col.get(v.lower())) for k, v in extra_cols.items())
                if cn and str(r.get(cn, "") or "").strip()
            )
            # CAS 셀에 복수 CAS가 개행 병기되는 경우 분해
            cas_list = re.findall(r"\d{2,7}-\d{2}-\d", cas) or [""]
            for one_cas in cas_list:
                rows.append({
                    "source_list": source_list,
                    "substance_name": name[:300],
                    "ec_number": ec[:60],
                    "cas_number": one_cas,
                    "detail": detail[:600],
                    "source_file": paths[-1].name,
                })
        print(f"  {source_list}: {paths[-1].name} → 누적 {len(rows)}행")

    manager = DbSessionManager.GetInstance()
    with manager.OpenSession() as session:
        session.execute(text('DROP TABLE IF EXISTS "echa_substance_screen"'))
        session.execute(text(
            'CREATE TABLE "echa_substance_screen" ('
            "source_list text, substance_name text, ec_number text,"
            "cas_number text, detail text, source_file text)"))
        insert_sql = text(
            'INSERT INTO "echa_substance_screen" VALUES ('
            ":source_list, :substance_name, :ec_number, :cas_number,"
            " :detail, :source_file)")
        for start in range(0, len(rows), 1000):
            session.execute(insert_sql, rows[start:start + 1000])
        session.commit()
    print(f"-> echa_substance_screen에 {len(rows)}행 기록")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
