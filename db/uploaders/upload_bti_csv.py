"""BTI CSV → Supabase 테이블 적재 스크립트 (범용).

테이블의 실제 컬럼(information_schema)과 CSV 헤더의 교집합만 넣으므로
스키마가 CSV와 조금 달라도 안전하다. 실행은 설계자 직접:

  # 1) 소량 스모크 (10행)
  python db/upload_bti_csv.py --csv ~/ASAP_A/data/processed/bti_for_upload/bti_case_evidence.csv \
      --table bti_case_evidence --limit 10

  # 2) 전체 적재 (기존 행 비우고)
  python db/upload_bti_csv.py --csv ... --table bti_case_evidence --truncate
  python db/upload_bti_csv.py --csv ~/ASAP_A/data/processed/bti_for_upload/bti_case_full.csv \
      --table <full_테이블명> --truncate

주의: full CSV는 155만 라인(멀티라인 셀 포함, 실제 ~117k 레코드)이라 수 분 걸린다.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parents[1])]

from sqlalchemy import text

from db.db_session_manager import DbSessionManager


def _table_columns(manager: DbSessionManager, table: str) -> list[str]:
    rows = manager.FetchRows(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :tableName ORDER BY ordinal_position"
        ),
        {"tableName": table},
    )
    return [str(row["column_name"]) for row in rows]


def _normalize(value: object) -> object:
    if value is None:
        return None
    textValue = str(value).strip()
    return textValue if textValue != "" else None


def main() -> int:
    parser = argparse.ArgumentParser(description="BTI CSV -> db 테이블 적재")
    parser.add_argument("--csv", required=True, help="원본 CSV 경로")
    parser.add_argument("--table", required=True, help="대상 테이블명")
    parser.add_argument("--limit", type=int, default=0, help="N행만 적재 (스모크용, 0=전체)")
    parser.add_argument("--batch", type=int, default=1000, help="커밋 배치 크기")
    parser.add_argument("--truncate", action="store_true", help="적재 전 테이블 비우기")
    args = parser.parse_args()

    csvPath = Path(args.csv).expanduser()
    if not csvPath.is_file():
        print(f"CSV 없음: {csvPath}")
        return 1

    manager = DbSessionManager.GetInstance()
    tableColumns = _table_columns(manager, args.table)
    if not tableColumns:
        print(f"테이블 '{args.table}'을 찾을 수 없거나 컬럼이 없음")
        return 1

    with open(csvPath, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        csvColumns = list(reader.fieldnames or [])
        common = [column for column in csvColumns if column in tableColumns]
        skippedCsv = [c for c in csvColumns if c not in common]
        skippedTable = [c for c in tableColumns if c not in common]
        print(f"적재 컬럼 {len(common)}개: {common}")
        if skippedCsv:
            print(f"CSV에만 있어 건너뜀: {skippedCsv}")
        if skippedTable:
            print(f"테이블에만 있음(NULL로 남음): {skippedTable}")
        if not common:
            print("공통 컬럼이 없어 중단")
            return 1

        insertSql = text(
            f'INSERT INTO {args.table} ({", ".join(common)}) '
            f'VALUES ({", ".join(":" + c for c in common)})'
        )

        with manager.OpenSession(timeout_seconds=120) as session:
            before = session.execute(
                text(f"SELECT count(*) FROM {args.table}")
            ).scalar()
            print(f"적재 전 행 수: {before}")
            if args.truncate:
                session.execute(text(f"TRUNCATE TABLE {args.table}"))
                session.commit()
                print("TRUNCATE 완료")

            batch: list[dict] = []
            total = 0
            startedAt = time.time()
            for row in reader:
                batch.append({c: _normalize(row.get(c)) for c in common})
                if len(batch) >= args.batch:
                    session.execute(insertSql, batch)
                    session.commit()
                    total += len(batch)
                    batch = []
                    print(f"  {total}행 적재... ({time.time() - startedAt:.0f}s)")
                if args.limit and total + len(batch) >= args.limit:
                    break
            if batch:
                session.execute(insertSql, batch)
                session.commit()
                total += len(batch)

            after = session.execute(
                text(f"SELECT count(*) FROM {args.table}")
            ).scalar()
            print(f"완료: +{total}행 적재, 테이블 행 수 {before} → {after} "
                  f"({time.time() - startedAt:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
