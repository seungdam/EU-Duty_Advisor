"""kr_audit_20260714 산출물을 Supabase에 적용하는 원커맨드 스크립트.

  python db/apply_taric_kr_audit.py --dry-run             # 뭐가 들어갈지 미리보기 (db 쓰기 없음)
  python db/apply_taric_kr_audit.py                       # INSERT 적용 (celex 5 + guidance 27, 중복 자동 스킵)
  python db/apply_taric_kr_audit.py --with-scope-update   # + post_master chapter_scope 확장 76건까지

- 중복 가드: celex는 celex_id, guidance는 certificate_code가 이미 있으면 스킵.
- 전부 단일 트랜잭션 — 중간 실패 시 통째로 롤백.
- db 자격증명은 실행 쉘 환경에서 읽는다 (백엔드 돌리는 그 터미널에서 실행할 것).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parents[1])]

from sqlalchemy import text

from db.db_session_manager import DbSessionManager

AUDIT_DIR = Path(__file__).resolve().parent / "artifacts" / "taric_kr_audit_20260714"


def _load_csv(name: str) -> list[dict]:
    with open(AUDIT_DIR / name, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _insert_rows(session, table: str, rows: list[dict], key_column: str, dry_run: bool) -> tuple[int, int]:
    inserted = skipped = 0
    for row in rows:
        keyValue = row[key_column]
        exists = session.execute(
            text(f"SELECT 1 FROM {table} WHERE {key_column} = :keyValue LIMIT 1"),
            {"keyValue": keyValue},
        ).first()
        if exists:
            skipped += 1
            print(f"  [스킵] {table}.{key_column}={keyValue} 이미 존재")
            continue
        if not dry_run:
            columns = list(row.keys())
            session.execute(
                text(
                    f'INSERT INTO {table} ({", ".join(columns)}) '
                    f'VALUES ({", ".join(":" + c for c in columns)})'
                ),
                {c: (row[c] if row[c] != "" else None) for c in columns},
            )
        inserted += 1
        print(f"  [{'예정' if dry_run else '추가'}] {table}.{key_column}={keyValue}")
    return inserted, skipped


def _scope_updates(session, dry_run: bool) -> int:
    updated = 0
    for row in _load_csv("02_scope_gaps.csv"):
        cert = row["cert_code"]
        current = set(row["마스터_현재scope"].split("/")) - {""}
        add = set(row["추가필요_챕터들"].split("/")) - {""}
        merged = "/".join(sorted(current | add))
        if not dry_run:
            result = session.execute(
                text(
                    "UPDATE post_taric_requirement_master SET chapter_scope = :merged "
                    "WHERE trigger_certificate_code = :cert"
                ),
                {"merged": merged, "cert": cert},
            )
            updated += result.rowcount
            print(f"  [스코프] {cert}: {result.rowcount}행 → {merged[:60]}")
        else:
            print(f"  [예정] {cert} → {merged[:60]}")
            updated += 1
    return updated


def _verify(session) -> int:
    """적용 결과를 기대값과 대조 — 전부 PASS면 0, 아니면 1 반환."""
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures += 1

    print("== 검증 ==")
    n = session.execute(text(
        "SELECT count(*) FROM taric_celex_table WHERE matched_method = 'manual_kr_audit_20260714'"
    )).scalar()
    check("celex 수동 추가 5행", n == 5, f"실제 {n}행")

    row = session.execute(text(
        "SELECT legal_base FROM taric_celex_table WHERE celex_id = '32003R1829'"
    )).first()
    check("32003R1829(GMO) 존재", row is not None, f"legal_base={row[0] if row else '없음'}")

    n = session.execute(text(
        "SELECT count(*) FROM taric_certificate_declaration_guidance "
        "WHERE source_basis LIKE 'kr_audit_20260714%'"
    )).scalar()
    check("guidance 수동 추가 27행", n == 27, f"실제 {n}행")

    n = session.execute(text(
        "SELECT count(*) FROM taric_certificate_declaration_guidance "
        "WHERE source_basis LIKE 'kr_audit_20260714%' AND source_confidence = 'low'"
    )).scalar()
    check("미확인 코드(L261/L271) 격리 2행", n == 2, f"실제 {n}행")

    scopes = dict(session.execute(text(
        "SELECT trigger_certificate_code, string_agg(DISTINCT chapter_scope, ' | ') "
        "FROM post_taric_requirement_master "
        "WHERE trigger_certificate_code IN ('C673','C644','Y155') "
        "GROUP BY trigger_certificate_code"
    )).fetchall())
    check("C673 스코프에 16류 포함(IUU 갭)", "16" in (scopes.get("C673") or ""), scopes.get("C673", "없음"))
    check("C644 스코프 확장(19류 포함)", "19" in (scopes.get("C644") or ""), (scopes.get("C644") or "없음")[:60])

    print(f"\n검증 결과: {'전부 PASS ✅' if failures == 0 else f'{failures}건 FAIL ❌'}")
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="kr_audit_20260714 Supabase 적용")
    parser.add_argument("--dry-run", action="store_true", help="쓰기 없이 미리보기")
    parser.add_argument("--with-scope-update", action="store_true", help="chapter_scope 확장까지 적용")
    parser.add_argument("--verify", action="store_true", help="적용 결과 검증만 수행 (쓰기 없음)")
    args = parser.parse_args()

    manager = DbSessionManager.GetInstance()
    if args.verify:
        with manager.OpenSession(timeout_seconds=60) as session:
            return _verify(session)

    with manager.OpenSession(timeout_seconds=120) as session:
        print("== ① taric_celex_table 추가 (5행) ==")
        celexInserted, celexSkipped = _insert_rows(
            session, "taric_celex_table", _load_csv("04_taric_celex_additions.csv"),
            "celex_id", args.dry_run,
        )
        print(f"== ② taric_certificate_declaration_guidance 추가 (27행) ==")
        guideInserted, guideSkipped = _insert_rows(
            session, "taric_certificate_declaration_guidance",
            _load_csv("05_certificate_guidance_additions.csv"),
            "certificate_code", args.dry_run,
        )
        scopeUpdated = 0
        if args.with_scope_update:
            print("== ③ post_taric_requirement_master 스코프 확장 (76 cert) ==")
            scopeUpdated = _scope_updates(session, args.dry_run)

        if args.dry_run:
            print("\n[dry-run] 쓰기 없음 — 위 목록이 실제 적용 대상입니다.")
        else:
            session.commit()
            print(
                f"\n적용 완료: celex +{celexInserted}(스킵 {celexSkipped}) | "
                f"guidance +{guideInserted}(스킵 {guideSkipped})"
                + (f" | scope UPDATE {scopeUpdated}행" if args.with_scope_update else "")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
