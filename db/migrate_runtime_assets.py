"""Create and load database-backed runtime assets.

This is the single transition loader retained until the pre-4-table migration
has been applied and verified. It reads the current local source assets and
writes idempotent PostgreSQL tables through the shared db connection.

The command intentionally does not accept credentials. Run it in the normal
project runtime where the shared DbSessionManager is already configured.

    PYTHONPATH=src python db/migrate_runtime_assets.py --plan
    PYTHONPATH=src python db/migrate_runtime_assets.py --apply
    PYTHONPATH=src python db/migrate_runtime_assets.py --verify
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from db.db_session_manager import DbSessionManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_VERSION = "pre4-20260725-v1"

SINGLETON_ASSETS = {
    "heading_axis_map": PROJECT_ROOT / "db/artifacts/heading_axis_map.json",
    "subheading_axis_map": PROJECT_ROOT / "db/artifacts/subheading_axis_map.json",
    "bti_recall_index": PROJECT_ROOT / "db/artifacts/bti_recall_index.json",
    "heading_vocab": PROJECT_ROOT / "db/artifacts/heading_vocab.json",
    "axis_field_binding": PROJECT_ROOT / "artifacts/binding_v1.json",
    "commodity_taxonomy": PROJECT_ROOT / "data/taxonomy/commodity_taxonomy.json",
    "species_taxonomy": (
        PROJECT_ROOT
        / "src/bussiness_logic/classification/rules/assets/species_taxonomy.json"
    ),
}


DDL = """
CREATE TABLE IF NOT EXISTS {table_name} (
    asset_version text PRIMARY KEY,
    source_hash text NOT NULL,
    payload jsonb NOT NULL,
    is_active boolean NOT NULL DEFAULT TRUE,
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

ROW_TABLE_DDL = (
    """
CREATE TABLE IF NOT EXISTS std_name_dictionary (
    dictionary_id bigserial PRIMARY KEY,
    ko text NOT NULL,
    en text NOT NULL,
    hs6 text,
    hs10 text,
    spec_ko text,
    spec_en text,
    source_hash text NOT NULL,
    UNIQUE (ko, en, hs6, hs10)
);
CREATE TABLE IF NOT EXISTS curated_term_bridge (
    bridge_id bigserial PRIMARY KEY,
    ko text NOT NULL,
    en text NOT NULL,
    source text NOT NULL,
    derivation text,
    approved_by text,
    approved_date text,
    hs6 text,
    source_hash text NOT NULL,
    UNIQUE (ko, en, hs6)
);
CREATE TABLE IF NOT EXISTS std_name_review_queue (
    normalized_term text PRIMARY KEY,
    raw_term text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    seen_count integer NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS food_type_dictionary (
    ko_term text PRIMARY KEY,
    en_term text NOT NULL,
    source_hash text NOT NULL
);
CREATE TABLE IF NOT EXISTS classification_criterion_taxonomy (
    criterion_id bigserial PRIMARY KEY,
    payload jsonb NOT NULL,
    source_hash text NOT NULL
);
CREATE TABLE IF NOT EXISTS product_input_dictionary (
    term_id text PRIMARY KEY,
    canonical_name text NOT NULL,
    term_type text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_name text,
    source_id text,
    source_url text,
    updated_at text
);
CREATE TABLE IF NOT EXISTS coi_form (
    form_key text PRIMARY KEY,
    product_name text,
    source_hash text NOT NULL,
    payload jsonb NOT NULL,
    is_active boolean NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS coi_product_map (
    product_name text NOT NULL,
    form_key text NOT NULL REFERENCES coi_form(form_key),
    PRIMARY KEY (product_name, form_key)
);
CREATE TABLE IF NOT EXISTS co_form (
    form_key text PRIMARY KEY,
    product_name text,
    source_hash text NOT NULL,
    payload jsonb NOT NULL,
    is_active boolean NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS co_product_map (
    product_name text NOT NULL,
    form_key text NOT NULL REFERENCES co_form(form_key),
    PRIMARY KEY (product_name, form_key)
);
CREATE TABLE IF NOT EXISTS classification_smoke_case (
    case_id bigserial PRIMARY KEY,
    product_name text,
    product_url text NOT NULL,
    expected_code text NOT NULL,
    source_hash text NOT NULL,
    UNIQUE (product_url, expected_code)
)
"""
)


def _HashBytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _JsonPayload(path: Path) -> tuple[str, str]:
    content = path.read_bytes()
    payload = json.loads(content.decode("utf-8"))
    return json.dumps(payload, ensure_ascii=False), _HashBytes(content)


def _JsonLines(path: Path) -> tuple[list[dict[str, Any]], str]:
    content = path.read_bytes()
    rows = [
        json.loads(line)
        for line in content.decode("utf-8").splitlines()
        if line.strip()
    ]
    return rows, _HashBytes(content)


def _CsvRows(path: Path) -> tuple[list[dict[str, str]], str]:
    content = path.read_bytes()
    rows = list(csv.DictReader(content.decode("utf-8-sig").splitlines()))
    return rows, _HashBytes(content)


def _ExecuteMany(cursor: Any, sql: str, rows: Iterable[tuple[Any, ...]]) -> None:
    from psycopg2.extras import execute_batch

    execute_batch(cursor, sql, list(rows), page_size=500)


def _LoadSingletons(cursor: Any) -> None:
    for tableName, path in SINGLETON_ASSETS.items():
        if not path.is_file():
            raise FileNotFoundError(f"Required runtime asset is missing: {path}")
        cursor.execute(DDL.format(table_name=tableName))
        payload, sourceHash = _JsonPayload(path)
        cursor.execute(
            f"""
            UPDATE {tableName}
            SET is_active = FALSE
            WHERE asset_version <> %s
            """,
            (ASSET_VERSION,),
        )
        cursor.execute(
            f"""
            INSERT INTO {tableName}
                (asset_version, source_hash, payload, is_active, updated_at)
            VALUES (%s, %s, %s::jsonb, TRUE, %s)
            ON CONFLICT (asset_version) DO UPDATE SET
                source_hash = EXCLUDED.source_hash,
                payload = EXCLUDED.payload,
                is_active = TRUE,
                updated_at = EXCLUDED.updated_at
            """,
            (
                ASSET_VERSION,
                sourceHash,
                payload,
                datetime.now(timezone.utc),
            ),
        )


def _LoadDictionaries(cursor: Any) -> None:
    stdPath = PROJECT_ROOT / "data/std_name_dictionary.jsonl"
    curatedPath = PROJECT_ROOT / "data/curated_term_bridge.jsonl"
    foodPath = PROJECT_ROOT / "data/food_type_dictionary.json"
    criterionPath = PROJECT_ROOT / "data/decription_taxonomy.csv"

    stdRows, stdHash = _JsonLines(stdPath)
    cursor.execute("TRUNCATE std_name_dictionary RESTART IDENTITY")
    _ExecuteMany(
        cursor,
        """
        INSERT INTO std_name_dictionary
            (ko, en, hs6, hs10, spec_ko, spec_en, source_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ko, en, hs6, hs10) DO UPDATE SET
            spec_ko = EXCLUDED.spec_ko,
            spec_en = EXCLUDED.spec_en,
            source_hash = EXCLUDED.source_hash
        """,
        (
            (
                str(row.get("ko") or ""),
                str(row.get("en") or ""),
                str(row.get("hs6") or ""),
                str(row.get("hs10") or ""),
                str(row.get("spec_ko") or ""),
                str(row.get("spec_en") or ""),
                stdHash,
            )
            for row in stdRows
        ),
    )

    curatedRows, curatedHash = _JsonLines(curatedPath)
    cursor.execute("TRUNCATE curated_term_bridge RESTART IDENTITY")
    _ExecuteMany(
        cursor,
        """
        INSERT INTO curated_term_bridge
            (ko, en, source, derivation, approved_by, approved_date, hs6, source_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ko, en, hs6) DO UPDATE SET
            source = EXCLUDED.source,
            derivation = EXCLUDED.derivation,
            approved_by = EXCLUDED.approved_by,
            approved_date = EXCLUDED.approved_date,
            source_hash = EXCLUDED.source_hash
        """,
        (
            (
                str(row.get("ko") or ""),
                str(row.get("en") or ""),
                str(row.get("source") or ""),
                str(row.get("derivation") or ""),
                str(row.get("approved_by") or ""),
                str(row.get("date") or ""),
                str(row.get("hs6") or ""),
                curatedHash,
            )
            for row in curatedRows
        ),
    )

    foodContent = foodPath.read_bytes()
    foodRows = json.loads(foodContent.decode("utf-8"))
    foodHash = _HashBytes(foodContent)
    cursor.execute("TRUNCATE food_type_dictionary")
    _ExecuteMany(
        cursor,
        """
        INSERT INTO food_type_dictionary (ko_term, en_term, source_hash)
        VALUES (%s, %s, %s)
        ON CONFLICT (ko_term) DO UPDATE SET
            en_term = EXCLUDED.en_term,
            source_hash = EXCLUDED.source_hash
        """,
        (
            (str(ko), str(en), foodHash)
            for ko, en in foodRows.items()
        ),
    )

    criterionRows, criterionHash = _CsvRows(criterionPath)
    cursor.execute("TRUNCATE classification_criterion_taxonomy RESTART IDENTITY")
    _ExecuteMany(
        cursor,
        """
        INSERT INTO classification_criterion_taxonomy (payload, source_hash)
        VALUES (%s::jsonb, %s)
        """,
        (
            (json.dumps(row, ensure_ascii=False), criterionHash)
            for row in criterionRows
        ),
    )


def _LoadFormDirectory(
    cursor: Any,
    *,
    root: Path,
    formTable: str,
    mapTable: str,
    mapColumn: str,
) -> None:
    if not root.is_dir():
        if formTable == "co_form":
            cursor.execute(f"TRUNCATE {mapTable}, {formTable}")
            return
        raise FileNotFoundError(f"Required form directory is missing: {root}")

    mapPath = root / "product_map.csv"
    relations: list[tuple[str, str]] = []
    if mapPath.is_file():
        mapRows, _ = _CsvRows(mapPath)
        for row in mapRows:
            productName = str(
                row.get("제품명")
                or row.get("product_name")
                or ""
            ).strip()
            formKeys = str(row.get(mapColumn) or "").split(";")
            for formKey in formKeys:
                formKey = formKey.strip()
                if productName and formKey:
                    relations.append((productName, formKey))

    referencedFormKeys = {formKey for _, formKey in relations}
    if referencedFormKeys:
        paths = [root / formKey for formKey in sorted(referencedFormKeys)]
        missing = [path for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Mapped normalized form is missing: "
                + ", ".join(str(path) for path in missing[:5])
            )
    else:
        paths = [
            path
            for path in sorted(root.glob("*.json"))
            if path.name not in {"index.json", "ingredient_dictionary.json"}
        ]

    formRows: list[tuple[str, str, str, str]] = []
    for path in paths:
        content = path.read_bytes()
        payload = json.loads(content.decode("utf-8"))
        productName = str(
            payload.get("product_name")
            or payload.get("product_name_hint")
            or ""
        )
        formRows.append(
            (
                path.name,
                productName,
                _HashBytes(content),
                json.dumps(payload, ensure_ascii=False),
            )
        )
    # PostgreSQL rejects truncating the referenced form table in a separate
    # statement even when the child table was emptied immediately beforehand.
    cursor.execute(f"TRUNCATE {mapTable}, {formTable}")
    _ExecuteMany(
        cursor,
        f"""
        INSERT INTO {formTable}
            (form_key, product_name, source_hash, payload, is_active)
        VALUES (%s, %s, %s, %s::jsonb, TRUE)
        """,
        formRows,
    )
    _ExecuteMany(
        cursor,
        f"""
        INSERT INTO {mapTable} (product_name, form_key)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        """,
        relations,
    )


def _BuildSmokeCaseValues(
    rows: Iterable[dict[str, str]],
    sourceHash: str,
) -> list[tuple[str, str, str, str]]:
    values: list[tuple[str, str, str, str]] = []
    for row in rows:
        productName = str(
            row.get("품목 분류 명칭")
            or row.get("상품명")
            or row.get("product_name")
            or ""
        ).strip()
        url = str(row.get("상품 상세") or row.get("url") or "").strip()
        expected = "".join(
            character
            for character in str(
                row.get("EU HS CODE")
                or row.get("answer")
                or ""
            )
            if character.isdigit()
        )
        if url and expected:
            values.append((productName, url, expected, sourceHash))
    return values


def _LoadSmokeCases(cursor: Any) -> None:
    path = PROJECT_ROOT / "data/EU_HS_test.csv"
    rows, sourceHash = _CsvRows(path)
    values = _BuildSmokeCaseValues(rows, sourceHash)
    cursor.execute("TRUNCATE classification_smoke_case RESTART IDENTITY")
    _ExecuteMany(
        cursor,
        """
        INSERT INTO classification_smoke_case
            (product_name, product_url, expected_code, source_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (product_url, expected_code) DO UPDATE SET
            product_name = EXCLUDED.product_name,
            source_hash = EXCLUDED.source_hash
        """,
        values,
    )


def Apply() -> None:
    manager = DbSessionManager.GetInstance()
    with manager.OpenRawConnection(timeout_seconds=120) as connection:
        try:
            with connection.cursor() as cursor:
                print("[1/5] loading singleton runtime assets")
                _LoadSingletons(cursor)
                print("[2/5] creating row-backed runtime tables")
                cursor.execute(ROW_TABLE_DDL)
                print("[3/5] loading dictionaries and classification taxonomy")
                _LoadDictionaries(cursor)
                print("[4/5] loading normalized COI/CO forms")
                _LoadFormDirectory(
                    cursor,
                    root=PROJECT_ROOT / "data/coi_normalized",
                    formTable="coi_form",
                    mapTable="coi_product_map",
                    mapColumn="coi_file",
                )
                _LoadFormDirectory(
                    cursor,
                    root=PROJECT_ROOT / "data/co_normalized",
                    formTable="co_form",
                    mapTable="co_product_map",
                    mapColumn="co_file",
                )
                print("[5/5] loading classification smoke cases")
                _LoadSmokeCases(cursor)
            connection.commit()
            print("runtime asset migration committed")
        except Exception as error:
            connection.rollback()
            print(
                "runtime asset migration rolled back: "
                f"{type(error).__name__}: {error}"
            )
            raise


EXPECTED_TABLES = (
    *SINGLETON_ASSETS.keys(),
    "std_name_dictionary",
    "curated_term_bridge",
    "std_name_review_queue",
    "food_type_dictionary",
    "classification_criterion_taxonomy",
    "product_input_dictionary",
    "coi_form",
    "coi_product_map",
    "co_form",
    "co_product_map",
    "classification_smoke_case",
)


def Verify() -> None:
    manager = DbSessionManager.GetInstance()
    failures: list[str] = []
    for tableName in EXPECTED_TABLES:
        if not manager.TableExists(tableName):
            failures.append(f"{tableName}: missing")
            continue
        count = manager.FetchOne(f"SELECT count(*) FROM {tableName}")
        print(f"{tableName}: {count}")
        if tableName not in (
            "co_form",
            "co_product_map",
            "std_name_review_queue",
            "product_input_dictionary",
        ) and not int(count or 0):
            failures.append(f"{tableName}: empty")
    if failures:
        raise RuntimeError("Runtime asset verification failed: " + "; ".join(failures))


def Plan() -> None:
    """Validate local migration inputs without opening a database connection."""
    for tableName, path in SINGLETON_ASSETS.items():
        payload, sourceHash = _JsonPayload(path)
        print(
            f"{tableName}: json_bytes={len(payload.encode('utf-8'))} "
            f"sha256={sourceHash[:12]}"
        )

    stdRows, _ = _JsonLines(PROJECT_ROOT / "data/std_name_dictionary.jsonl")
    curatedRows, _ = _JsonLines(PROJECT_ROOT / "data/curated_term_bridge.jsonl")
    foodPayload, _ = _JsonPayload(PROJECT_ROOT / "data/food_type_dictionary.json")
    criterionRows, _ = _CsvRows(
        PROJECT_ROOT / "data/decription_taxonomy.csv"
    )
    smokeRows, smokeHash = _CsvRows(PROJECT_ROOT / "data/EU_HS_test.csv")
    smokeValues = _BuildSmokeCaseValues(smokeRows, smokeHash)

    def mappedFormCount(root: Path, mapColumn: str) -> int:
        mapPath = root / "product_map.csv"
        if not mapPath.is_file():
            return 0
        rows, _ = _CsvRows(mapPath)
        return len(
            {
                formKey.strip()
                for row in rows
                for formKey in str(row.get(mapColumn) or "").split(";")
                if formKey.strip()
            }
        )

    print(f"std_name_dictionary: rows={len(stdRows)}")
    print(f"curated_term_bridge: rows={len(curatedRows)}")
    print(f"food_type_dictionary: json_bytes={len(foodPayload.encode('utf-8'))}")
    print(f"classification_criterion_taxonomy: rows={len(criterionRows)}")
    print(
        "coi_form: mapped_files="
        + str(
            mappedFormCount(
                PROJECT_ROOT / "data/coi_normalized",
                "coi_file",
            )
        )
    )
    print(
        "co_form: mapped_files="
        + str(
            mappedFormCount(
                PROJECT_ROOT / "data/co_normalized",
                "co_file",
            )
        )
    )
    print(f"classification_smoke_case: rows={len(smokeValues)}")


def Main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    if arguments.plan:
        Plan()
    elif arguments.apply:
        Apply()
    else:
        Verify()


if __name__ == "__main__":
    Main()
