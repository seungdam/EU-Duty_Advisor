"""branch_index 3테이블(hs4/hs6/cn8)을 Supabase에 적재한다 (P2-1).

원천: tests/cn_hierarchy_analysis_grouping_20260702/branch_indexes_20260702/*.csv
(cn_table 전수 대조 검증 완료본). 테이블은 drop-and-recreate라 재실행 안전.

실행(설계자 — env 필요):
  set -a; . ./.env; set +a
  conda run -n asap python load_branch_indexes.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
csv.field_size_limit(10_000_000)

SOURCE_DIR = PROJECT_ROOT / "tests" / "cn_hierarchy_analysis_grouping_20260702" / "branch_indexes_20260702"
TABLES = {
    "hs4_branch_index": "hs4_branch_index_20260702.csv",
    "hs6_branch_index": "hs6_branch_index_20260702.csv",
    "cn8_branch_index": "cn8_branch_index_20260702.csv",
}
# CSV 컬럼 전부 TEXT로 적재 (소비자가 파싱). code/parent에만 인덱스.
COLUMNS = [
    "branch_id", "stage", "wco_group_id", "wco_group_name", "chapter_code",
    "parent_code", "code", "code_level", "option_label_en", "branch_question_en",
    "branch_context", "hard_conditions", "primary_criterion", "criterion_types",
    "criterion_modifiers", "criterion_evidence", "decision_axes",
    "required_dto_fields", "positive_terms", "negative_terms",
    "quantitative_conditions", "residual_other_flag", "grouping_confidence",
    "needs_review", "data_quality_flags", "child_count",
    "description_resolution", "description_source", "source_row_id", "source_file",
]


def main() -> int:
    import io

    from db.db_session_manager import DbSessionManager

    manager = DbSessionManager.GetInstance()
    with manager.OpenRawConnection() as conn:
        cur = conn.cursor()
        for table, filename in TABLES.items():
            path = SOURCE_DIR / filename
            with open(path, newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            colsSql = ", ".join(f'"{c}" text' for c in COLUMNS)
            cur.execute(f'DROP TABLE IF EXISTS "{table}"')
            cur.execute(f'CREATE TABLE "{table}" ({colsSql})')
            # COPY = one round-trip per table. executemany was one round-trip
            # per ROW (~100ms x 16.6k rows to ap-south-1 = 30+ min). Never again.
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            for r in rows:
                writer.writerow([str(r.get(c) or "") for c in COLUMNS])
            buffer.seek(0)
            colList = ", ".join(f'"{c}"' for c in COLUMNS)
            cur.copy_expert(
                f'COPY "{table}" ({colList}) FROM STDIN WITH (FORMAT csv)',
                buffer,
            )
            cur.execute(f'CREATE INDEX ON "{table}" (parent_code)')
            cur.execute(f'CREATE INDEX ON "{table}" (code)')
            conn.commit()
            cur.execute(f'SELECT count(*) FROM "{table}"')
            print(f"{table}: {cur.fetchone()[0]} rows loaded (source {len(rows)})")
        cur.close()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
