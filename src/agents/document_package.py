"""
Document Package Resolver — TARIC10 코드 → EU 수입 시 요구 서류 패키지.

⚠️ 프로토타입 — 시제품 검증용. 모듈 구조 안정화되면 정식 위치 (LinkML / etc) 로 이동.

설계:
  - LLM 0건. SQL + 룰 기반.
  - 한국 수출자 입장: KR origin 으로 EU 가 요구하는 측정/서류 추출.
  - Access2Markets / TARIC consultation 사이트와 1:1 검증 가능.
  - A2M처럼 입력 TARIC10 exact row뿐 아니라 상위 code-level measure도 함께 조회.
    예: 2103901000 -> 2103900000 -> 2103000000 -> 2100000000

사용:
    PYTHONPATH=src python -m agents.document_package 0101210000
    PYTHONPATH=src python -m agents.document_package 0101210000 --format json
    PYTHONPATH=src python -m agents.document_package 0101210000 --format text
    PYTHONPATH=src python -m agents.document_package 0101210000 --include-celex-excerpt

입력 코드는 10자리 TARIC. 8자리만 주면 자동으로 '00' 패딩 + 모든 TARIC10 변형 그룹.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

try:
    import psycopg2
    from psycopg2 import pool as psycopg2_pool
except ImportError:
    sys.exit("[fatal] psycopg2 missing. pip install psycopg2-binary")


DEFAULT_DB_NAME = "postgres"
DB_SOURCE_NAME = os.environ.get("ASAP_DB_SOURCE_NAME", "supabase")
DB_POOL_MAX_CONNECTIONS = int(os.environ.get("ASAP_DB_POOL_MAX_CONNECTIONS", "4"))
_DB_POOL_LOCK = threading.Lock()
_DB_POOL = None
_DB_POOL_KEY = None
_TABLE_EXISTS_CACHE: dict[str, bool] = {}

# canonical TARIC table:
#   taric_master_table = leaf-aware canonical table
#   taric_master_selection_table = legacy compatibility table, do not use here

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_local_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


def _db_connect_config():
    """Return psycopg2 connection config without exposing secret values."""
    database_url = os.environ.get("ASAP_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if database_url:
        return ("dsn", database_url)

    host = os.environ.get("PGHOST")
    if host:
        conn_kwargs = {
            "host": host,
            "dbname": os.environ.get("PGDATABASE", DEFAULT_DB_NAME),
            "user": os.environ.get("PGUSER"),
            "password": os.environ.get("PGPASSWORD"),
            "port": os.environ.get("PGPORT", "5432"),
        }
        sslmode = os.environ.get("PGSSLMODE")
        if sslmode:
            conn_kwargs["sslmode"] = sslmode
        return ("kwargs", {k: v for k, v in conn_kwargs.items() if v})

    raise RuntimeError(
        "Database connection is not configured. Set ASAP_DATABASE_URL "
        "(preferred) or DATABASE_URL, or set PGHOST/PGDATABASE/PGUSER/"
        "PGPASSWORD/PGPORT. For Supabase, include sslmode=require."
    )


def _get_db_pool():
    """Return a process-local connection pool.

    Production/default usage is Supabase via ASAP_DATABASE_URL or DATABASE_URL.
    Standard libpq PG* variables are also supported for local development.
    """
    global _DB_POOL, _DB_POOL_KEY
    config_kind, config_value = _db_connect_config()
    key = (config_kind, repr(config_value), DB_POOL_MAX_CONNECTIONS)
    with _DB_POOL_LOCK:
        if _DB_POOL is not None and _DB_POOL_KEY == key:
            return _DB_POOL
        if _DB_POOL is not None:
            _DB_POOL.closeall()
        _TABLE_EXISTS_CACHE.clear()
        if config_kind == "dsn":
            _DB_POOL = psycopg2_pool.ThreadedConnectionPool(
                1, DB_POOL_MAX_CONNECTIONS, config_value
            )
        else:
            _DB_POOL = psycopg2_pool.ThreadedConnectionPool(
                1, DB_POOL_MAX_CONNECTIONS, **config_value
            )
        _DB_POOL_KEY = key
        return _DB_POOL


def _connect_db():
    return _get_db_pool().getconn()


def _release_db(conn) -> None:
    if conn is None:
        return
    if _DB_POOL is None:
        conn.close()
        return
    _DB_POOL.putconn(conn)

# ---------------------------------------------------------------------------
# Certificate code 분류
# ---------------------------------------------------------------------------
# EU TARIC certificate prefix 의미:
#   C-xxx : 필수 서류 (mandatory document, 필요 시 제출)
#   N-xxx : 국제 표준 서류 (national or international document)
#   U-xxx : 우대 관세 적용 서류 (preferential origin)
#   Y-xxx : 면제 사유 선언 (declaration of exemption / waiver)
#   L-xxx : 라이센스 (license)
def cert_category(code: str) -> str:
    if not code:
        return "unknown"
    code = code.strip().upper()
    if code.startswith("Y"):
        return "exemption_declaration"  # 면제 사유 선언 (의무 아님)
    if code.startswith("C"):
        return "mandatory_certificate"
    if code.startswith("N"):
        return "national_document"
    if code.startswith("U"):
        return "preferential_origin"
    if code.startswith("L"):
        return "import_license"
    return "other"


# ---------------------------------------------------------------------------
# duty_text 파서 (Cond: X cert: Y-NNN 같은 raw 형식 → 구조화)
# ---------------------------------------------------------------------------
COND_RE = re.compile(r"Cond:\s*([A-Z])\s*cert:\s*([A-Z]-?\d+)\s*\((\d+)\):")
PERCENT_RE = re.compile(r"^\s*\d+[.,]\d*\s*%\s*$")


def parse_duty_text(raw: str) -> dict:
    if not raw:
        return {"raw": "", "rate": None, "conditions": []}
    if PERCENT_RE.match(raw):
        return {"raw": raw.strip(), "rate": raw.strip(), "conditions": []}
    conds = []
    for m in COND_RE.finditer(raw):
        conds.append({
            "condition_code": m.group(1),
            "certificate": m.group(2),
            "action_code": m.group(3),
        })
    rate = raw.split("Cond:")[0].strip().rstrip(";") or None
    return {"raw": raw, "rate": rate, "conditions": conds}


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------
@dataclass
class Certificate:
    code: str
    description: str
    category: str  # mandatory_certificate | exemption_declaration | ...
    guidance: dict = field(default_factory=dict)


@dataclass
class CelexRef:
    celex_id: Optional[str]
    title: Optional[str]
    match_status: Optional[str]
    excerpt: Optional[str] = None


@dataclass
class DetailedRequirement:
    requirement_master_id: str
    match_reason: str
    trigger_certificate_code: str
    source_layer: str
    domain_route: str
    domain: str
    requirement_type: str
    required_document: str
    required_action: str
    required_level: str
    condition_text: str
    exemption_text: str
    required_facts: list[str]
    blocking_facts: list[str]
    external_lookup_required: str
    external_dataset_ids: list[str]
    external_lookup_mode: str
    user_fallback_evidence: list[str]
    data_gap_status: str
    needs_review: bool
    decision_status: str
    decision_label: str
    decision_reason: str
    missing_facts: list[str]
    satisfied_facts: list[str]


@dataclass
class Requirement:
    measure_type: str
    applies_to_korea: bool
    origins: list[str]
    source_goods_codes: list[str]
    duty: dict
    certificates: list[Certificate]
    conditions_count: int
    footnotes: list[str]
    legal_base: Optional[str]
    celex: Optional[CelexRef]
    needs_review: bool
    detailed_requirements: list[DetailedRequirement] = field(default_factory=list)


@dataclass
class DocumentPackage:
    taric10: str
    cn8: str
    total_measure_rows: int
    has_data: bool
    requirements: list[Requirement]
    verification_urls: dict
    data_source: str
    notes: list[str] = field(default_factory=list)
    product_facts: dict = field(default_factory=dict)
    checklist_summary: dict = field(default_factory=dict)


def _resolve_parent_goods_code(nomenclature_row: dict) -> Optional[str]:
    """nomenclature 행의 parent_line_id 에서 부모 goods_code_10 추출."""
    plid = nomenclature_row.get("parent_line_id") or ""
    if not plid:
        return None
    # parent_line_id 형식 예: '2102201100:80'
    return plid.split(":", 1)[0] if ":" in plid else plid


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "t", "1", "yes", "y"}


def _split_semicolon(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_split_semicolon(item))
        return out
    return [x.strip() for x in str(value).split(";") if x.strip()]


UNKNOWN_FACT_VALUES = {"", "unknown", "unk", "n/a", "na", "none", "null", "tbd", "미상", "모름"}


def _fact_is_known(product_facts: dict, fact_name: str) -> bool:
    if fact_name not in product_facts:
        return False
    value = product_facts.get(fact_name)
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in UNKNOWN_FACT_VALUES
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _fact_bool(product_facts: dict, fact_name: str) -> bool | None:
    if fact_name not in product_facts:
        return None
    value = product_facts.get(fact_name)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "t", "1", "yes", "y", "included", "present", "해당", "예"}:
            return True
        if normalized in {"false", "f", "0", "no", "n", "not_included", "absent", "none", "비해당", "아니오"}:
            return False
    return None


def _is_exemption_requirement(detail: "DetailedRequirement") -> bool:
    cert = (detail.trigger_certificate_code or "").upper()
    return (
        cert.startswith("Y")
        or detail.requirement_type == "exemption"
        or detail.required_document.lower().startswith("exemption declaration")
        or bool(detail.exemption_text)
    )


def _negative_trigger_applies(detail: "DetailedRequirement", product_facts: dict) -> tuple[bool, str]:
    negative_triggers = {
        "organic_claim": "organic claim is false",
        "animal_origin": "animal-origin trigger is false",
        "fishery_product": "fishery-product trigger is false",
        "gmo_present": "GMO trigger is false",
    }
    for fact, reason in negative_triggers.items():
        if fact in detail.required_facts:
            value = _fact_bool(product_facts, fact)
            if value is False:
                return True, reason
    return False, ""


def _decision_for_detail(detail: "DetailedRequirement", product_facts: dict) -> dict:
    required_facts = [f for f in detail.required_facts if f]
    blocking_facts = [f for f in detail.blocking_facts if f]
    satisfied = [f for f in required_facts if _fact_is_known(product_facts, f)]
    missing = [f for f in required_facts if f not in satisfied]
    missing_blocking = [f for f in blocking_facts if not _fact_is_known(product_facts, f)]
    is_exemption = _is_exemption_requirement(detail)

    negative, negative_reason = _negative_trigger_applies(detail, product_facts)
    if negative:
        return {
            "decision_status": "exempted",
            "decision_label": "면제",
            "decision_reason": negative_reason,
            "missing_facts": [],
            "satisfied_facts": satisfied,
        }

    if is_exemption:
        exemption_applies = _fact_bool(product_facts, "exemption_condition_applies")
        if exemption_applies is True:
            return {
                "decision_status": "exempted",
                "decision_label": "면제",
                "decision_reason": "exemption condition is explicitly true in product facts",
                "missing_facts": [],
                "satisfied_facts": satisfied,
            }
        if missing_blocking:
            return {
                "decision_status": "pending",
                "decision_label": "판단보류",
                "decision_reason": "exemption route needs unresolved blocking facts",
                "missing_facts": missing_blocking,
                "satisfied_facts": satisfied,
            }
        return {
            "decision_status": "conditional",
            "decision_label": "조건부",
            "decision_reason": "exemption declaration route is available if its condition applies",
            "missing_facts": missing,
            "satisfied_facts": satisfied,
        }

    if missing_blocking:
        return {
            "decision_status": "pending",
            "decision_label": "판단보류",
            "decision_reason": "blocking facts are missing",
            "missing_facts": missing_blocking,
            "satisfied_facts": satisfied,
        }
    if detail.external_lookup_required in {"true", "conditional"}:
        return {
            "decision_status": "conditional",
            "decision_label": "조건부",
            "decision_reason": f"external lookup is {detail.external_lookup_required}",
            "missing_facts": missing,
            "satisfied_facts": satisfied,
        }
    if detail.required_level in {"mandatory", "mandatory_check"}:
        return {
            "decision_status": "required",
            "decision_label": "필요",
            "decision_reason": f"required_level={detail.required_level}",
            "missing_facts": missing,
            "satisfied_facts": satisfied,
        }
    if missing:
        return {
            "decision_status": "conditional",
            "decision_label": "조건부",
            "decision_reason": "non-blocking facts are still needed for final confirmation",
            "missing_facts": missing,
            "satisfied_facts": satisfied,
        }
    return {
        "decision_status": "required",
        "decision_label": "필요",
        "decision_reason": "all declared requirement facts are available",
        "missing_facts": [],
        "satisfied_facts": satisfied,
    }


def _checklist_summary(requirements: list["Requirement"]) -> dict:
    counts = {"required": 0, "conditional": 0, "exempted": 0, "pending": 0}
    missing: set[str] = set()
    documents: dict[str, list[str]] = {k: [] for k in counts}
    groups: dict[str, dict] = {}
    for req in requirements:
        if not req.applies_to_korea:
            continue
        for detail in req.detailed_requirements:
            status = detail.decision_status or "pending"
            if status not in counts:
                status = "pending"
            counts[status] += 1
            if detail.required_document:
                documents[status].append(detail.required_document)
            missing.update(detail.missing_facts or [])
            group_name = _document_group_name(detail)
            group = groups.setdefault(group_name, {
                "group_name": group_name,
                "status": "exempted",
                "documents": [],
                "declarations": [],
                "domains": set(),
                "missing_facts": set(),
                "external_dataset_ids": set(),
                "reasons": set(),
            })
            group["status"] = _merge_status(group["status"], status)
            target = "declarations" if _is_exemption_requirement(detail) else "documents"
            if detail.required_document:
                group[target].append(detail.required_document)
            group["domains"].add(detail.domain_route or detail.domain)
            group["missing_facts"].update(detail.missing_facts or [])
            group["external_dataset_ids"].update(detail.external_dataset_ids or [])
            if detail.decision_reason:
                group["reasons"].add(detail.decision_reason)
    group_rows = []
    for group in groups.values():
        group_rows.append({
            "group_name": group["group_name"],
            "status": group["status"],
            "label": {
                "required": "필요",
                "conditional": "조건부",
                "pending": "판단보류",
                "exempted": "면제",
            }.get(group["status"], "판단보류"),
            "documents": sorted(set(group["documents"])),
            "declarations": sorted(set(group["declarations"])),
            "domains": sorted(x for x in group["domains"] if x),
            "missing_facts": sorted(group["missing_facts"]),
            "external_dataset_ids": sorted(group["external_dataset_ids"]),
            "reasons": sorted(group["reasons"])[:4],
        })
    status_rank = {"required": 0, "conditional": 1, "pending": 2, "exempted": 3}
    return {
        "counts": counts,
        "missing_facts": sorted(missing),
        "documents": {k: sorted(set(v)) for k, v in documents.items()},
        "document_groups": sorted(group_rows, key=lambda g: (status_rank.get(g["status"], 9), g["group_name"])),
    }


def _binding_source_layer(detail: "DetailedRequirement") -> str:
    if detail.source_layer == "baseline_document":
        return "baseline"
    if detail.source_layer == "pre_taric_gate":
        return "pre_taric"
    return "post_taric"


def _normalize_binding_level(value: str) -> str:
    level = str(value or "").strip().lower()
    if level in {"required", "mandatory", "mandatory_check"}:
        return "required"
    if level in {"conditional", "condition"}:
        return "conditional"
    if level in {"support", "supporting"}:
        return "support"
    if level in {"optional", "exempted"}:
        return "optional"
    if level in {"pending", "needs_review"}:
        return "pending"
    return "conditional"


def _merge_binding_level(current: str, incoming: str) -> str:
    rank = {"required": 0, "conditional": 1, "pending": 2, "support": 3, "optional": 4}
    current = _normalize_binding_level(current)
    incoming = _normalize_binding_level(incoming)
    return current if rank.get(current, 9) <= rank.get(incoming, 9) else incoming


def _binding_field_status(missing_facts: list[str], required_level: str) -> str:
    if missing_facts:
        return "pending" if _normalize_binding_level(required_level) == "required" else "conditional"
    return "satisfied"


def _fetch_document_binding_cards(
    cur,
    requirements: list["Requirement"],
    product_facts: dict,
) -> list[dict]:
    """Group applicable baseline/pre/post rows into UI document cards.

    document_binding is intentionally compact, so human-readable document names
    are resolved from baseline_document_master and source provenance stays on
    each binding row.
    """
    if not _table_exists(cur, "document_binding"):
        return []
    if not (
        _table_exists(cur, "baseline_document_master")
        and _column_exists(cur, "baseline_document_master", "document_id")
    ):
        return []

    source_ids: dict[str, set[str]] = defaultdict(set)
    details_by_ref: dict[tuple[str, str], list[DetailedRequirement]] = defaultdict(list)
    for req in requirements:
        if not req.applies_to_korea:
            continue
        for detail in req.detailed_requirements:
            source_id = detail.requirement_master_id
            if not source_id:
                continue
            source_layer = _binding_source_layer(detail)
            source_ids[source_layer].add(source_id)
            details_by_ref[(source_layer, source_id)].append(detail)

    clauses: list[str] = []
    params: list[object] = []
    for source_layer in ("baseline", "pre_taric", "post_taric"):
        ids = sorted(source_ids.get(source_layer) or [])
        if not ids:
            continue
        clauses.append("(source_layer = %s AND source_id = ANY(%s))")
        params.extend([source_layer, ids])
    if not clauses:
        return []

    cur.execute(
        f"""
        SELECT binding_id, document_id, source_layer, source_id, binding_action,
               required_level, field_key, required_fact_key, sort_order
        FROM document_binding
        WHERE {" OR ".join(clauses)}
        ORDER BY
          CASE
            WHEN sort_order::text ~ '^[0-9]+$' THEN sort_order::int
            ELSE 999999
          END,
          document_id,
          binding_id
        """,
        params,
    )
    cols = [d[0] for d in cur.description]
    binding_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    if not binding_rows:
        return []

    document_ids = sorted({
        str(row.get("document_id") or "").strip()
        for row in binding_rows
        if str(row.get("document_id") or "").strip()
    })
    if not document_ids:
        return []

    cur.execute(
        """
        SELECT *
        FROM baseline_document_master
        WHERE document_id = ANY(%s)
        """,
        (document_ids,),
    )
    baseline_cols = [d[0] for d in cur.description]
    baseline_rows = [dict(zip(baseline_cols, row)) for row in cur.fetchall()]
    baseline_by_id = {
        str(row.get("document_id") or "").strip(): row
        for row in baseline_rows
        if str(row.get("document_id") or "").strip()
    }

    cards: dict[str, dict] = {}
    field_maps: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in binding_rows:
        document_id = str(row.get("document_id") or "").strip()
        if not document_id:
            continue
        baseline = baseline_by_id.get(document_id) or {}
        card = cards.setdefault(document_id, {
            "document_id": document_id,
            "document_code": baseline.get("document_code") or document_id.upper(),
            "document_name": baseline.get("document_name") or document_id.replace("_", " ").title(),
            "document_name_ko": baseline.get("document_name_ko") or baseline.get("document_name") or document_id,
            "document_family": baseline.get("document_family") or "",
            "required_level": "optional",
            "decision_status": "optional",
            "prepared_by": baseline.get("prepared_by") or "",
            "submitted_to": baseline.get("submitted_to") or "",
            "fields": [],
            "pre_checks": [],
            "post_requirements": [],
            "pre_taric_links": [],
            "post_taric_links": [],
            "taric_certificates": [],
            "missing_facts": [],
            "source_bindings": [],
        })

        source_layer = str(row.get("source_layer") or "")
        source_id = str(row.get("source_id") or "")
        binding_level = _normalize_binding_level(str(row.get("required_level") or ""))
        card["required_level"] = _merge_binding_level(card.get("required_level") or "optional", binding_level)

        required_fact = str(row.get("required_fact_key") or "").strip()
        binding_missing = [required_fact] if required_fact and not _fact_is_known(product_facts, required_fact) else []
        for fact in binding_missing:
            if fact not in card["missing_facts"]:
                card["missing_facts"].append(fact)

        source_binding = {
            "binding_id": row.get("binding_id") or "",
            "source_layer": source_layer,
            "source_id": source_id,
            "binding_action": row.get("binding_action") or "",
            "required_level": binding_level,
            "field_key": row.get("field_key") or "",
            "required_fact_key": required_fact,
            "missing_facts": binding_missing,
        }
        card["source_bindings"].append(source_binding)

        field_key = str(row.get("field_key") or "").strip()
        if (row.get("binding_action") or "") == "add_field" and field_key:
            field = field_maps[document_id].setdefault(field_key, {
                "field_key": field_key,
                "label": field_key.replace("_", " "),
                "required_by": [],
                "missing_facts": [],
                "status": "satisfied",
            })
            if source_layer not in field["required_by"]:
                field["required_by"].append(source_layer)
            for fact in binding_missing:
                if fact not in field["missing_facts"]:
                    field["missing_facts"].append(fact)
            field["status"] = _binding_field_status(field["missing_facts"], card["required_level"])

        source_details = details_by_ref.get((source_layer, source_id)) or []
        for detail in source_details:
            for fact in detail.missing_facts or []:
                if fact not in card["missing_facts"]:
                    card["missing_facts"].append(fact)
            if detail.trigger_certificate_code and detail.trigger_certificate_code not in card["taric_certificates"]:
                card["taric_certificates"].append(detail.trigger_certificate_code)
            item = {
                "source_id": source_id,
                "requirement_type": detail.requirement_type,
                "required_action": detail.required_action,
                "required_level": detail.required_level,
                "decision_status": detail.decision_status,
                "missing_facts": list(detail.missing_facts or []),
                "required_document": detail.required_document,
                "domain": detail.domain_route or detail.domain,
            }
            if source_layer == "pre_taric":
                card["pre_checks"].append(item)
                if source_id not in card["pre_taric_links"]:
                    card["pre_taric_links"].append(source_id)
            elif source_layer == "post_taric":
                card["post_requirements"].append(item)
                if source_id not in card["post_taric_links"]:
                    card["post_taric_links"].append(source_id)

        card["decision_status"] = card["required_level"]

    def sort_key(card: dict) -> tuple[int, str]:
        raw = baseline_by_id.get(card.get("document_id") or "", {}).get("runtime_sort_order") or ""
        try:
            order = int(str(raw))
        except ValueError:
            order = 9999
        return order, str(card.get("document_id") or "")

    for document_id, card in cards.items():
        fields = list(field_maps.get(document_id, {}).values())
        fields.sort(key=lambda f: f.get("field_key") or "")
        card["fields"] = fields
        card["pre_checks"] = list({x["source_id"]: x for x in card["pre_checks"]}.values())
        card["post_requirements"] = list({x["source_id"]: x for x in card["post_requirements"]}.values())
        card["taric_certificates"] = sorted(card["taric_certificates"])
        card["missing_facts"] = sorted(card["missing_facts"])
        card["source_bindings"] = sorted(
            card["source_bindings"],
            key=lambda b: (b.get("source_layer") or "", b.get("source_id") or "", b.get("binding_id") or ""),
        )

    return sorted(cards.values(), key=sort_key)


def _merge_status(current: str, incoming: str) -> str:
    rank = {"required": 0, "conditional": 1, "pending": 2, "exempted": 3}
    return current if rank.get(current, 9) <= rank.get(incoming, 9) else incoming


def _document_group_name(detail: "DetailedRequirement") -> str:
    text = f"{detail.required_document} {detail.domain_route} {detail.domain} {detail.requirement_type}".lower()
    is_cosmetic = (detail.domain_route == "cosmetics" or detail.domain == "cosmetics")
    if is_cosmetic and "cpnp" in text:
        return "CPNP notification"
    if is_cosmetic and ("product information file" in text or " pif" in text):
        return "Cosmetic PIF / product file"
    if is_cosmetic and ("safety report" in text or "cpsr" in text):
        return "Cosmetic CPSR / safety assessment"
    if is_cosmetic and "responsible person" in text:
        return "EU responsible person"
    if is_cosmetic and ("cosmetic label" in text or "ingredient list" in text or "language label" in text):
        return "Cosmetic label review"
    if is_cosmetic and ("annex ii" in text or "annex iii" in text or "annex iv" in text or "annex v" in text or "annex vi" in text):
        return "Cosmetic ingredient annex screen"
    if is_cosmetic and ("nanomaterial" in text or "cmr" in text):
        return "Cosmetic ingredient risk screen"
    if is_cosmetic and "gmp" in text:
        return "Cosmetic GMP evidence"
    if is_cosmetic and ("claims" in text or "animal testing" in text):
        return "Cosmetic substantiation statements"
    if "ched-a" in text or "common health entry document for animals" in text:
        return "CHED-A / Veterinary control"
    if "ched-p" in text:
        return "CHED-P / Animal-origin products"
    if "ched-d" in text:
        return "CHED-D / High-risk food-feed"
    if "cites" in text:
        return "CITES permit / species check"
    if "organic" in text or "certificate of inspection" in text:
        return "Organic COI"
    if "zootechnical" in text or "equidae" in text or "breeding book" in text:
        return "Zootechnical / equidae documents"
    if "iuu" in text or "catch certificate" in text:
        return "IUU catch certificate"
    if "preferential origin" in text or "movement certificate" in text or "eur.1" in text:
        return "Preferential origin proof"
    if "import licence" in text or "import license" in text:
        return "Import licence"
    if "exemption declaration" in text:
        return "Exemption declarations"
    return detail.domain_route or detail.domain or detail.required_document or "Other document requirements"


def _detail_from_post_req_row(row: dict, reason: str, product_facts: dict) -> DetailedRequirement:
    detail = DetailedRequirement(
        requirement_master_id=row.get("requirement_master_id") or "",
        match_reason=reason,
        trigger_certificate_code=(row.get("trigger_certificate_code") or "").upper(),
        source_layer=row.get("source_layer") or "",
        domain_route=row.get("domain_route") or "",
        domain=row.get("domain") or "",
        requirement_type=row.get("requirement_type") or "",
        required_document=row.get("required_document") or "",
        required_action=row.get("required_action") or "",
        required_level=row.get("required_level") or "",
        condition_text=row.get("condition_text") or "",
        exemption_text=row.get("exemption_text") or "",
        required_facts=_split_semicolon(row.get("required_facts")),
        blocking_facts=_split_semicolon(row.get("blocking_facts")),
        external_lookup_required=row.get("external_lookup_required") or "",
        external_dataset_ids=_split_semicolon(row.get("external_dataset_ids")),
        external_lookup_mode=row.get("external_lookup_mode") or "",
        user_fallback_evidence=_split_semicolon(row.get("user_fallback_evidence")),
        data_gap_status=row.get("data_gap_status") or "",
        needs_review=_truthy(row.get("needs_review")),
        decision_status="pending",
        decision_label="판단보류",
        decision_reason="not_evaluated",
        missing_facts=[],
        satisfied_facts=[],
    )
    decision = _decision_for_detail(detail, product_facts)
    detail.decision_status = decision["decision_status"]
    detail.decision_label = decision["decision_label"]
    detail.decision_reason = decision["decision_reason"]
    detail.missing_facts = decision["missing_facts"]
    detail.satisfied_facts = decision["satisfied_facts"]
    return detail


def _detail_from_pre_req_row(row: dict, reason: str, product_facts: dict, source_count: int = 1) -> DetailedRequirement:
    domain = row.get("domain_scope") or row.get("domain") or ""
    action = row.get("required_action") or row.get("requirement_type") or "pre_taric_screening"
    title = row.get("trigger_title") or ""
    required_document = {
        "sanctions": "Pre-TARIC sanctions screening",
        "cites": "Pre-TARIC CITES/species screening",
    }.get(domain, f"Pre-TARIC {domain or 'domain'} screening")
    condition = row.get("condition_text") or ""
    if source_count > 1:
        condition = (condition + " " if condition else "") + f"{source_count} source legal acts matched; sample: {title[:220]}"
    elif title:
        condition = (condition + " " if condition else "") + f"Source: {title[:260]}"

    detail = DetailedRequirement(
        requirement_master_id=row.get("pre_requirement_master_id") or "",
        match_reason=reason,
        trigger_certificate_code="",
        source_layer="pre_taric_gate",
        domain_route=domain,
        domain=domain,
        requirement_type=row.get("requirement_type") or "",
        required_document=required_document,
        required_action=action,
        required_level=row.get("required_level") or "conditional",
        condition_text=condition,
        exemption_text=row.get("prohibition_or_restriction_text") or "",
        required_facts=_split_semicolon(row.get("required_facts")),
        blocking_facts=_split_semicolon(row.get("blocking_facts")),
        external_lookup_required="true" if any(_truthy(row.get(k)) for k in (
            "needs_sanctions_party_lookup",
            "needs_origin_country_lookup",
            "needs_end_use_lookup",
        )) else "",
        external_dataset_ids=_split_semicolon(row.get("trigger_celex_id")),
        external_lookup_mode=row.get("runtime_gate") or "",
        user_fallback_evidence=_split_semicolon(row.get("user_fallback_evidence")),
        data_gap_status="pre_gate",
        needs_review=_truthy(row.get("needs_review")),
        decision_status="pending",
        decision_label="판단보류",
        decision_reason="pre_taric_gate_requires_screening",
        missing_facts=[],
        satisfied_facts=[],
    )
    decision = _decision_for_detail(detail, product_facts)
    detail.decision_status = decision["decision_status"]
    detail.decision_label = decision["decision_label"]
    detail.decision_reason = decision["decision_reason"]
    detail.missing_facts = decision["missing_facts"]
    detail.satisfied_facts = decision["satisfied_facts"]
    return detail


def _ancestor_goods_codes(goods_code_10: str) -> list[str]:
    """Return exact + broader zero-padded TARIC/CN ancestors.

    A2M/TARIC can attach measures above the exact leaf. For example:
    2103901000 should inherit measures attached to 2100000000.
    """
    code = goods_code_10
    candidates = [
        code,
        code[:8] + "00",
        code[:6] + "0000",
        code[:4] + "000000",
        code[:2] + "00000000",
    ]
    out: list[str] = []
    for c in candidates:
        if c not in out:
            out.append(c)
    return out


def _table_exists(cur, table_name: str) -> bool:
    cached = _TABLE_EXISTS_CACHE.get(table_name)
    if cached is not None:
        return cached
    cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
    row = cur.fetchone()
    exists = bool(row and row[0])
    _TABLE_EXISTS_CACHE[table_name] = exists
    return exists


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
        """,
        (table_name, column_name),
    )
    return bool(cur.fetchone())


def _fetch_certificate_guidance(cur, certificate_codes: list[str]) -> dict[str, dict]:
    if not certificate_codes or not _table_exists(cur, "taric_certificate_declaration_guidance"):
        return {}
    cur.execute(
        """
        SELECT *
        FROM taric_certificate_declaration_guidance
        WHERE certificate_code = ANY(%s)
        """,
        (certificate_codes,),
    )
    cols = [d[0] for d in cur.description]
    return {
        row_dict.get("certificate_code") or "": row_dict
        for row_dict in (dict(zip(cols, row)) for row in cur.fetchall())
        if row_dict.get("certificate_code")
    }


POST_REQ_FIELDS = [
    "requirement_master_id",
    "source_layer",
    "domain_route",
    "domain",
    "requirement_type",
    "required_document",
    "required_action",
    "required_level",
    "condition_text",
    "exemption_text",
    "required_facts",
    "blocking_facts",
    "external_lookup_required",
    "external_dataset_ids",
    "external_lookup_mode",
    "user_fallback_evidence",
    "data_gap_status",
    "needs_review",
    "trigger_certificate_code",
    "trigger_measure_type_code",
    "trigger_legal_base",
    "trigger_celex_id",
]

PRE_REQ_FIELDS = [
    "pre_requirement_master_id",
    "pre_gate_family",
    "domain",
    "domain_scope",
    "chapter_scope",
    "requirement_type",
    "required_action",
    "required_level",
    "trigger_celex_id",
    "trigger_title",
    "runtime_gate",
    "pre_gate_priority",
    "applies_to_product_scope",
    "applies_to_origin_scope",
    "applies_to_destination_scope",
    "condition_text",
    "prohibition_or_restriction_text",
    "required_facts",
    "blocking_facts",
    "user_question_hint",
    "user_fallback_evidence",
    "needs_sanctions_party_lookup",
    "needs_origin_country_lookup",
    "needs_end_use_lookup",
    "needs_review",
]

BASELINE_DOC_FIELDS = [
    "document_code",
    "document_name",
    "document_name_ko",
    "document_family",
    "stage",
    "default_required_level",
    "applies_to_domain_scope",
    "applies_to_chapter_scope",
    "applies_to_transaction_scope",
    "required_facts",
    "field_keys",
    "linked_pre_gate_families",
    "linked_pre_requirement_types",
    "linked_post_requirement_types",
    "linked_certificate_prefixes",
    "linked_certificate_codes",
    "linked_required_document_keywords",
    "prepared_by",
    "submitted_to",
    "source_basis",
    "runtime_sort_order",
    "needs_review",
]


def _split_scope_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_split_scope_values(item))
        return out
    return [x.strip() for x in re.split(r"[;,]", str(value)) if x.strip()]


def _baseline_domain_scopes(product_facts: dict) -> set[str]:
    scopes: set[str] = set()

    def add(value) -> None:
        for token in _split_scope_values(value):
            norm = token.strip().lower()
            if not norm or norm in UNKNOWN_FACT_VALUES:
                continue
            scopes.add(norm)
            if norm in {"food_feed_non_animal", "beverage", "noodle", "pasta", "sauce"}:
                scopes.add("food")
            if norm in {"animal_origin_food", "fishery", "seafood", "meat", "dairy", "egg"}:
                scopes.add("animal_origin")
            if norm in {"chemical", "chemicals"}:
                scopes.add("hazardous")

    for key in (
        "regulatory_domains",
        "domain_scopes",
        "domain_scope",
        "domains",
        "domain",
        "product_domain",
        "product_category",
        "pre_gate_domains",
    ):
        if key in product_facts:
            add(product_facts.get(key))
    return scopes


def _domain_scope_matches(scope_text: str, routed_domains: set[str]) -> bool:
    scopes = [s.lower() for s in _split_scope_values(scope_text)]
    if not scopes:
        return False
    if "all" in scopes:
        return True
    return bool(routed_domains.intersection(scopes))


def _chapter_scope_matches(scope_text: str, chapter: str) -> bool:
    chapter = chapter.zfill(2)
    scopes = _split_scope_values(scope_text)
    if not scopes:
        return False
    for raw in scopes:
        token = raw.strip().upper()
        if token == "ALL":
            return True
        if re.fullmatch(r"\d{2}", token) and token == chapter:
            return True
        match = re.fullmatch(r"(\d{2})-(\d{2})", token)
        if match and match.group(1) <= chapter <= match.group(2):
            return True
    return False


def _detail_from_baseline_doc_row(row: dict, reason: str, product_facts: dict) -> DetailedRequirement:
    default_level = (row.get("default_required_level") or "").strip().lower()
    required_level = "mandatory" if default_level == "required" else default_level or "conditional"
    document_name = row.get("document_name") or row.get("document_code") or "Baseline document"
    document_code = row.get("document_code") or ""
    field_keys = _split_semicolon(row.get("field_keys"))
    linked_pre = _split_semicolon(row.get("linked_pre_requirement_types"))
    linked_post = _split_semicolon(row.get("linked_post_requirement_types"))
    linked_certs = _split_semicolon(row.get("linked_certificate_codes"))

    condition_parts = [
        f"document_code={document_code}" if document_code else "",
        f"prepared_by={row.get('prepared_by')}" if row.get("prepared_by") else "",
        f"submitted_to={row.get('submitted_to')}" if row.get("submitted_to") else "",
        f"linked_pre={';'.join(linked_pre)}" if linked_pre else "",
        f"linked_post={';'.join(linked_post)}" if linked_post else "",
        f"linked_certificates={';'.join(linked_certs)}" if linked_certs else "",
    ]
    condition = " | ".join(part for part in condition_parts if part)

    detail = DetailedRequirement(
        requirement_master_id=document_code,
        match_reason=reason,
        trigger_certificate_code="",
        source_layer="baseline_document",
        domain_route=row.get("document_family") or "baseline",
        domain=row.get("applies_to_domain_scope") or "all",
        requirement_type=row.get("document_family") or "baseline_document",
        required_document=document_name,
        required_action=f"prepare_{document_code.lower()}" if document_code else "prepare_baseline_document",
        required_level=required_level,
        condition_text=condition,
        exemption_text="",
        required_facts=_split_semicolon(row.get("required_facts")),
        blocking_facts=[],
        external_lookup_required="",
        external_dataset_ids=linked_certs,
        external_lookup_mode="",
        user_fallback_evidence=field_keys,
        data_gap_status="baseline_document",
        needs_review=_truthy(row.get("needs_review")),
        decision_status="pending",
        decision_label="판단보류",
        decision_reason="not_evaluated",
        missing_facts=[],
        satisfied_facts=[],
    )
    decision = _decision_for_detail(detail, product_facts)
    detail.decision_status = decision["decision_status"]
    detail.decision_label = decision["decision_label"]
    detail.decision_reason = decision["decision_reason"]
    detail.missing_facts = decision["missing_facts"]
    detail.satisfied_facts = decision["satisfied_facts"]
    return detail


def _fetch_baseline_documents(
    cur,
    goods_code_10: str,
    product_facts: dict,
) -> list[DetailedRequirement]:
    """Attach common baseline documents by routed domain + CN chapter."""
    if not _table_exists(cur, "baseline_document_master"):
        return []

    chapter = (product_facts.get("chapter") or goods_code_10[:2] or "").zfill(2)
    routed_domains = _baseline_domain_scopes(product_facts)

    cur.execute(
        f"""
        SELECT {", ".join(BASELINE_DOC_FIELDS)}
        FROM baseline_document_master
        WHERE lower(coalesce(stage, '')) = 'baseline'
        ORDER BY
          CASE
            WHEN runtime_sort_order::text ~ '^[0-9]+$' THEN runtime_sort_order::int
            ELSE 9999
          END,
          document_code
        """
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    detailed: list[DetailedRequirement] = []
    for row in rows:
        if not _domain_scope_matches(row.get("applies_to_domain_scope") or "", routed_domains):
            continue
        if not _chapter_scope_matches(row.get("applies_to_chapter_scope") or "", chapter):
            continue
        reason = (
            "baseline_document_master:"
            f"chapter:{chapter};domains:{','.join(sorted(routed_domains)) or 'unknown'}"
        )
        detailed.append(_detail_from_baseline_doc_row(row, reason, product_facts))
    return detailed


def _fetch_detailed_requirements(
    cur,
    group_rows: list[dict],
    celex_map: dict[str, dict],
    product_facts: dict,
) -> list[DetailedRequirement]:
    """Attach reviewed post-TARIC requirement rows to a TARIC measure group."""
    if not _table_exists(cur, "post_taric_requirement_master"):
        return []

    cert_codes = sorted({
        c.strip().upper()
        for row in group_rows
        for c in (row.get("certificate_codes") or "").split(";")
        if c.strip()
    })
    measure_codes = sorted({
        str(row.get("measure_type_code") or "").strip()
        for row in group_rows
        if str(row.get("measure_type_code") or "").strip()
    })
    legal_bases = sorted({
        str(row.get("legal_base") or "").strip()
        for row in group_rows
        if str(row.get("legal_base") or "").strip()
    })
    celex_ids = sorted({
        celex_map.get(lb, {}).get("celex_id") or ""
        for lb in legal_bases
        if celex_map.get(lb, {}).get("celex_id")
    })

    cur.execute(
        f"""
        SELECT {", ".join(POST_REQ_FIELDS)}
        FROM post_taric_requirement_master
        WHERE source_layer = 'taric_triggered'
          AND (
            NULLIF(trigger_certificate_code, '') = ANY(%s)
            OR (
              NULLIF(trigger_certificate_code, '') IS NULL
              AND NULLIF(trigger_measure_type_code, '') = ANY(%s)
            )
          )
        """,
        (cert_codes or ["__none__"], measure_codes or ["__none__"]),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    scored: dict[str, tuple[int, str, dict]] = {}
    for row in rows:
        reasons: list[str] = []
        rank = 99
        row_cert = (row.get("trigger_certificate_code") or "").upper()
        row_measure = row.get("trigger_measure_type_code") or ""
        row_legal = row.get("trigger_legal_base") or ""
        row_celex = row.get("trigger_celex_id") or ""

        cert_match = bool(row_cert and row_cert in cert_codes)
        measure_match = bool(row_measure and row_measure in measure_codes)
        legal_match = bool(row_legal and row_legal in legal_bases)
        celex_match = bool(row_celex and row_celex in celex_ids)

        if row_cert:
            if not cert_match:
                continue
            if row_measure and not measure_match:
                continue
            if row_legal and not legal_match:
                continue
            if row_celex and not celex_match:
                continue
            rank = min(rank, 0)
            reasons.append(f"certificate:{row_cert}")
        elif not measure_match:
            continue

        if celex_match:
            rank = min(rank, 1)
            reasons.append(f"celex:{row_celex}")
        if legal_match:
            rank = min(rank, 2)
            reasons.append(f"legal_base:{row_legal}")
        if measure_match:
            rank = min(rank, 3)
            reasons.append(f"measure_type:{row_measure}")

        req_id = row.get("requirement_master_id") or ""
        if not req_id:
            continue
        current = scored.get(req_id)
        reason = "; ".join(reasons) or "post_taric_requirement_master_match"
        if current is None or rank < current[0]:
            scored[req_id] = (rank, reason, row)

    detailed: list[DetailedRequirement] = []
    for _, reason, row in sorted(scored.values(), key=lambda item: (item[0], item[2].get("required_document") or "")):
        detailed.append(_detail_from_post_req_row(row, reason, product_facts))
    return detailed


def _fetch_product_domain_requirements(
    cur,
    goods_code_10: str,
    product_facts: dict,
) -> list[DetailedRequirement]:
    """Attach post-TARIC chapter route checks and confirmed product-domain seeds."""
    if not _table_exists(cur, "post_taric_requirement_master"):
        return []
    chapter = goods_code_10[:2]
    heading = goods_code_10[:4]
    cn8 = goods_code_10[:8]
    allowed_domains = _allowed_product_domains(product_facts)
    cur.execute(
        f"""
        SELECT {", ".join(POST_REQ_FIELDS)}
        FROM post_taric_requirement_master
        WHERE source_layer IN ('chapter_route_seed', 'product_domain_seed')
          AND (
            %s = ANY(string_to_array(replace(coalesce(chapter_scope, ''), ' ', ''), ';'))
            OR %s = ANY(string_to_array(replace(coalesce(applies_to_cn_scope, ''), ' ', ''), ';'))
            OR %s = ANY(string_to_array(replace(coalesce(applies_to_cn_scope, ''), ' ', ''), ';'))
          )
        ORDER BY
          CASE source_layer WHEN 'chapter_route_seed' THEN 0 ELSE 1 END,
          CASE required_level WHEN 'mandatory' THEN 0 WHEN 'mandatory_check' THEN 1 ELSE 2 END,
          required_document
        """,
        (chapter, heading, cn8),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    detailed: list[DetailedRequirement] = []
    seen: set[str] = set()
    for row in rows:
        req_id = row.get("requirement_master_id") or ""
        if not req_id or req_id in seen:
            continue
        if row.get("source_layer") == "product_domain_seed":
            domain = (row.get("domain_route") or row.get("domain") or "").strip()
            if domain not in allowed_domains:
                continue
        seen.add(req_id)
        reason = f"{row.get('source_layer')}:chapter:{chapter}"
        detailed.append(_detail_from_post_req_row(row, reason, product_facts))
    return detailed


def _fetch_pre_taric_requirements(
    cur,
    goods_code_10: str,
    product_facts: dict,
) -> list[DetailedRequirement]:
    """Attach Supabase pre-TARIC screening gates by chapter + routed pre domains."""
    if not _table_exists(cur, "pre_taric_requirement_master"):
        return []
    if not (
        _column_exists(cur, "pre_taric_requirement_master", "chapter_scope")
        and _column_exists(cur, "pre_taric_requirement_master", "domain_scope")
    ):
        return []

    chapter = (product_facts.get("chapter") or goods_code_10[:2] or "").zfill(2)
    pre_gate_domains = _split_semicolon(product_facts.get("pre_gate_domains"))
    if not pre_gate_domains:
        pre_gate_domains = ["sanctions"]

    cur.execute(
        f"""
        SELECT {", ".join(PRE_REQ_FIELDS)}
        FROM pre_taric_requirement_master
        WHERE lower(coalesce(runtime_gate::text, '')) = 'true'
          AND (
            upper(coalesce(chapter_scope, '')) = 'ALL'
            OR %s = ANY(string_to_array(replace(coalesce(chapter_scope, ''), ' ', ''), ';'))
          )
          AND (
            NULLIF(domain_scope, '') = ANY(%s)
            OR NULLIF(domain, '') = ANY(%s)
          )
        ORDER BY domain_scope, pre_gate_priority, trigger_title
        """,
        (chapter, pre_gate_domains, pre_gate_domains),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    grouped: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (
            row.get("domain_scope") or row.get("domain") or "",
            row.get("pre_gate_family") or "",
            row.get("requirement_type") or "",
            row.get("required_action") or "",
        )
        grouped[key].append(row)

    detailed: list[DetailedRequirement] = []
    for key in sorted(grouped):
        group = grouped[key]
        first = group[0]
        reason = f"pre_taric_requirement_master:chapter:{chapter};domain:{key[0]}"
        detailed.append(_detail_from_pre_req_row(first, reason, product_facts, source_count=len(group)))
    return detailed


def _allowed_product_domains(product_facts: dict) -> set[str]:
    tokens = _product_fact_tokens(product_facts)
    allowed: set[str] = set()

    explicit_map = {
        "cosmetic": "cosmetics",
        "cosmetics": "cosmetics",
        "beauty": "cosmetics",
        "food": "food_feed_non_animal",
        "feed": "food_feed_non_animal",
        "beverage": "food_feed_non_animal",
        "noodle": "food_feed_non_animal",
        "pasta": "food_feed_non_animal",
        "sauce": "food_feed_non_animal",
        "animal_origin_food": "animal_origin_food",
        "animal_origin": "animal_origin_food",
        "live_animal": "animal_origin_food",
        "equine": "animal_origin_food",
        "horse": "animal_origin_food",
        "meat": "animal_origin_food",
        "dairy": "animal_origin_food",
        "egg": "animal_origin_food",
        "fishery": "fishery",
        "fish": "fishery",
        "seafood": "fishery",
        "shrimp": "fishery",
        "plant_health": "plant_health",
        "plant": "plant_health",
        "wood": "plant_health",
    }
    for token in tokens:
        if token in explicit_map:
            allowed.add(explicit_map[token])

    if _fact_bool(product_facts, "animal_origin") is True:
        allowed.add("animal_origin_food")
    if _fact_bool(product_facts, "fishery_product") is True:
        allowed.add("fishery")
    if _fact_bool(product_facts, "organic_claim") is True:
        allowed.add("food_feed_non_animal")
    if _fact_is_known(product_facts, "plant_product_type"):
        allowed.add("plant_health")
    if _fact_is_known(product_facts, "full_ingredient_list") and "cosmetics" in tokens:
        allowed.add("cosmetics")
    return allowed


def _product_fact_tokens(product_facts: dict) -> set[str]:
    values = []
    for key in (
        "product_category",
        "product_domain",
        "domain",
        "domain_route",
        "domain_scopes",
        "regulatory_domains",
        "chapter_routes",
        "intended_use",
        "product_type",
    ):
        if key in product_facts:
            values.append(product_facts.get(key))
    tokens: set[str] = set()

    def add(value) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                add(k)
                add(v)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        text = str(value).lower()
        if text in UNKNOWN_FACT_VALUES:
            return
        for token in re.split(r"[^a-z0-9_]+", text):
            if token:
                tokens.add(token)
        if "animal origin" in text:
            tokens.add("animal_origin")
        if "live animal" in text:
            tokens.add("live_animal")
        if "plant health" in text:
            tokens.add("plant_health")

    for value in values:
        add(value)
    return tokens


def _lookup_nomenclature_row(cur, goods_code_10: str) -> Optional[dict]:
    cur.execute(
        """
        SELECT
            master_id, row_kind, goods_code_10, line_id, parent_line_id,
            leaf_description_en, nomenclature_path_text
        FROM taric_master_table
        WHERE goods_code_10 = %s
        ORDER BY CASE WHEN row_kind = 'nomenclature_only' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (goods_code_10,),
    )
    if not cur.description:
        return None
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def _candidate_goods_codes(cur, goods_code_10: str, notes: list[str]) -> list[str]:
    codes = _ancestor_goods_codes(goods_code_10)
    nom_row = _lookup_nomenclature_row(cur, goods_code_10)
    if nom_row:
        leaf_desc = (nom_row.get("leaf_description_en") or "").strip()
        row_kind = nom_row.get("row_kind") or ""
        if row_kind == "nomenclature_only":
            notes.append(
                f"Code {goods_code_10} is row_kind='nomenclature_only' "
                f"(leaf_description: {leaf_desc!r}). Parent/ancestor measures are also checked."
            )
        parent_code = _resolve_parent_goods_code(nom_row)
        if parent_code and parent_code not in codes:
            codes.insert(1, parent_code)
            notes.append(
                f"Parent goods_code_10 {parent_code!r} added from TARIC nomenclature parent_line_id."
            )
    return codes


# ---------------------------------------------------------------------------
# 핵심 함수
# ---------------------------------------------------------------------------
def get_document_package(
    taric10: str,
    include_celex_excerpt: bool = False,
    celex_excerpt_chars: int = 600,
    product_facts: Optional[dict] = None,
) -> DocumentPackage:
    product_facts = product_facts or {}
    raw_input = taric10
    code = re.sub(r"\D", "", taric10)
    truncated_from = None
    if len(code) == 8:
        code = code + "00"
    elif len(code) in (11, 12):
        # 12-digit input (TARIC10 + productline suffix, e.g., '2102201110' = '2102201100':'10').
        # master_selection_table only stores 10-digit goods_code_10, so we truncate
        # and note the productline suffix in the result for downstream awareness.
        truncated_from = code
        code = code[:10]
    if len(code) != 10:
        raise ValueError(
            f"TARIC code must be 8, 10, or 11–12 digits, got {len(code)}: {raw_input!r}"
        )
    cn8 = code[:8]
    notes: list[str] = []
    if truncated_from:
        suffix = truncated_from[10:]
        notes.append(
            f"Input was {len(truncated_from)}-digit code {truncated_from!r}; "
            f"productline suffix {suffix!r} truncated. "
            f"taric_master_table stores official 10-digit goods_code_10; "
            f"measures shown apply to the 10-digit parent/ancestor where applicable."
        )

    conn = _connect_db()
    cur = conn.cursor()

    # 1. measure rows: exact + ancestor measures.
    # A2M inherits measures attached to broader TARIC/CN nodes, e.g. R1227/25
    # attached to 2100000000 appears when querying 2103901000.
    candidate_codes = _candidate_goods_codes(cur, code, notes)
    cur.execute(
        """
        SELECT
            master_id, goods_code_10, cn8, line_id,
            measure_type_code, measure_type_description,
            duty_text, condition_summary,
            certificate_codes, certificate_descriptions,
            footnote_codes, footnote_descriptions,
            origin_code, origin_description_en, applies_to_korea,
            legal_base, official_journal, publication_date,
            needs_review
        FROM taric_master_table
        WHERE goods_code_10 = ANY(%s)
          AND row_kind = 'measure_line'
          AND is_current = 'true'
        ORDER BY
            CASE WHEN goods_code_10 = %s THEN 0 ELSE 1 END,
            length(regexp_replace(goods_code_10, '0+$', '')) DESC,
            CASE WHEN applies_to_korea = 'true' THEN 0 ELSE 1 END,
            measure_type_code, legal_base, origin_description_en
        """,
        (candidate_codes, code),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    inherited_codes = sorted({r["goods_code_10"] for r in rows if r.get("goods_code_10") != code})
    if inherited_codes:
        notes.append(
            "Included broader TARIC/CN ancestor measures, A2M-style inheritance: "
            + ", ".join(inherited_codes)
        )

    if not rows:
        notes.append("No current measure rows found for this TARIC10.")

    # 2. (measure_type, legal_base) 그룹핑
    grouped = defaultdict(list)
    for r in rows:
        key = (
            r["measure_type_description"] or "Unknown measure",
            r["legal_base"] or "",
            r.get("goods_code_10") or "",
        )
        grouped[key].append(r)

    # 3. legal_base → CELEX 조회
    legal_bases = sorted({r["legal_base"] for r in rows if r["legal_base"]})
    celex_map: dict[str, dict] = {}
    if legal_bases:
        cur.execute(
            """SELECT legal_base, celex_id, full_description, celex_match_status
               FROM taric_celex_table WHERE legal_base = ANY(%s)""",
            (legal_bases,),
        )
        for lb, cid, fd, ms in cur.fetchall():
            celex_map[lb] = {"celex_id": cid, "title": fd, "status": ms}

    # 4. (선택) CELEX 본문 발췌
    celex_excerpts: dict[str, str] = {}
    if include_celex_excerpt and _table_exists(cur, "taric_celex_source_chunks"):
        celex_ids = [c["celex_id"] for c in celex_map.values() if c.get("celex_id")]
        if celex_ids:
            cur.execute(
                """SELECT celex_id, string_agg(chunk_text, ' ' ORDER BY chunk_index) AS body
                   FROM taric_celex_source_chunks
                   WHERE celex_id = ANY(%s)
                   GROUP BY celex_id""",
                (celex_ids,),
            )
            for cid, body in cur.fetchall():
                if body:
                    celex_excerpts[cid] = (body or "")[:celex_excerpt_chars]

    all_certificate_codes = sorted({
        c.strip().upper()
        for r in rows
        for c in (r["certificate_codes"] or "").split(";")
        if c.strip()
    })
    certificate_guidance = _fetch_certificate_guidance(cur, all_certificate_codes)

    # 5. Requirement 객체 구성
    requirements: list[Requirement] = []
    for (mt_desc, lb, source_code), group_rows in grouped.items():
        cert_set: dict[str, str] = {}  # code → description
        for r in group_rows:
            codes = (r["certificate_codes"] or "").split(";")
            descs = (r["certificate_descriptions"] or "").split(";")
            for i, c in enumerate(codes):
                c = c.strip()
                if not c:
                    continue
                d = descs[i].strip() if i < len(descs) else ""
                if c not in cert_set or len(d) > len(cert_set.get(c, "")):
                    cert_set[c] = d

        certs = [
            Certificate(code=c, description=d, category=cert_category(c), guidance=certificate_guidance.get(c, {}))
            for c, d in sorted(cert_set.items())
        ]

        footnote_codes = sorted({
            f.strip() for r in group_rows
            for f in (r["footnote_codes"] or "").split(";") if f.strip()
        })

        kr_applicable = any(_truthy(r["applies_to_korea"]) for r in group_rows)
        origins = sorted({r["origin_description_en"] for r in group_rows if r["origin_description_en"]})
        source_goods_codes = sorted({r["goods_code_10"] for r in group_rows if r.get("goods_code_10")})

        # 첫 measure 의 duty_text 를 대표로 (같은 measure_type+legal_base 그룹은 보통 같은 duty)
        duty_raw = next((r["duty_text"] for r in group_rows if r["duty_text"]), "")
        duty_parsed = parse_duty_text(duty_raw)

        celex_info = celex_map.get(lb)
        celex_ref = None
        if celex_info and celex_info.get("celex_id"):
            celex_ref = CelexRef(
                celex_id=celex_info["celex_id"],
                title=(celex_info.get("title") or "")[:300],
                match_status=celex_info.get("status"),
                excerpt=celex_excerpts.get(celex_info["celex_id"]),
            )

        detailed_requirements = _fetch_detailed_requirements(cur, group_rows, celex_map, product_facts)

        requirements.append(Requirement(
            measure_type=mt_desc,
            applies_to_korea=kr_applicable,
            origins=origins[:8],
            source_goods_codes=source_goods_codes,
            duty=duty_parsed,
            certificates=certs,
            conditions_count=len(group_rows),
            footnotes=footnote_codes[:10],
            legal_base=lb or None,
            celex=celex_ref,
            needs_review=any(_truthy(r.get("needs_review")) for r in group_rows),
            detailed_requirements=detailed_requirements,
        ))

    baseline_details = _fetch_baseline_documents(cur, code, product_facts)
    if baseline_details:
        requirements.append(Requirement(
            measure_type="Baseline document requirements",
            applies_to_korea=True,
            origins=["All third countries"],
            source_goods_codes=[f"CN chapter {code[:2]}"],
            duty={"raw": "", "rate": None, "conditions": []},
            certificates=[],
            conditions_count=len(baseline_details),
            footnotes=[],
            legal_base=None,
            celex=None,
            needs_review=any(d.needs_review for d in baseline_details),
            detailed_requirements=baseline_details,
        ))
        notes.append(
            "Included baseline document requirements from baseline_document_master: "
            + str(len(baseline_details))
            + " documents"
        )

    pre_taric_details = _fetch_pre_taric_requirements(cur, code, product_facts)
    if pre_taric_details:
        pre_domains = sorted({
            d.domain_route or d.domain for d in pre_taric_details
            if d.domain_route or d.domain
        })
        requirements.append(Requirement(
            measure_type="Pre-TARIC screening requirements",
            applies_to_korea=True,
            origins=["All third countries"],
            source_goods_codes=[f"CN chapter {code[:2]}"],
            duty={"raw": "", "rate": None, "conditions": []},
            certificates=[],
            conditions_count=len(pre_taric_details),
            footnotes=[],
            legal_base=None,
            celex=None,
            needs_review=any(d.needs_review for d in pre_taric_details),
            detailed_requirements=pre_taric_details,
        ))
        notes.append(
            "Included pre-TARIC screening requirements from pre_taric_requirement_master: "
            + ", ".join(pre_domains)
        )

    product_domain_details = _fetch_product_domain_requirements(cur, code, product_facts)
    if product_domain_details:
        domain_names = sorted({
            d.domain_route or d.domain for d in product_domain_details
            if d.domain_route or d.domain
        })
        requirements.append(Requirement(
            measure_type="Product regulatory requirements",
            applies_to_korea=True,
            origins=["All third countries"],
            source_goods_codes=[f"CN chapter {code[:2]}"],
            duty={"raw": "", "rate": None, "conditions": []},
            certificates=[],
            conditions_count=len(product_domain_details),
            footnotes=[],
            legal_base=None,
            celex=None,
            needs_review=any(d.needs_review for d in product_domain_details),
            detailed_requirements=product_domain_details,
        ))
        notes.append(
            "Included product-domain requirements from post_taric_requirement_master: "
            + ", ".join(domain_names)
        )

    # KR 적용 measure 먼저 정렬
    requirements.sort(key=lambda r: (not r.applies_to_korea, r.measure_type))
    checklist_summary = _checklist_summary(requirements)
    binding_cards = _fetch_document_binding_cards(cur, requirements, product_facts)
    if binding_cards:
        checklist_summary["document_binding_cards"] = binding_cards
        checklist_summary["document_binding_count"] = len(binding_cards)

    cur.close()
    _release_db(conn)

    return DocumentPackage(
        taric10=code, cn8=cn8, total_measure_rows=len(rows),
        has_data=bool(rows or requirements), requirements=requirements,
        verification_urls=_verification_urls(code), data_source=DB_SOURCE_NAME,
        notes=notes, product_facts=product_facts,
        checklist_summary=checklist_summary,
    )


def _verification_urls(taric10: str) -> dict:
    return {
        "access2markets": (
            f"https://trade.ec.europa.eu/access-to-markets/en/results"
            f"?product={taric10}&origin=KR&destination=EU"
        ),
        "taric_consultation": (
            "https://taxation-customs.ec.europa.eu/dds2/taric/"
            "taric_consultation.jsp?Lang=en"
            f"&Taric={taric10}"
        ),
    }


# ---------------------------------------------------------------------------
# 출력 포맷터
# ---------------------------------------------------------------------------
def render_text(pkg: DocumentPackage) -> str:
    lines: list[str] = []
    push = lines.append

    push("=" * 78)
    push(f"  TARIC10: {pkg.taric10}    CN8: {pkg.cn8}    measure rows: {pkg.total_measure_rows}")
    push("=" * 78)

    if pkg.notes:
        for n in pkg.notes:
            push(f"  ⚠️  {n}")
        push("")

    if not pkg.has_data:
        push("  (no current measures)")
        return "\n".join(lines)

    if pkg.checklist_summary:
        counts = pkg.checklist_summary.get("counts", {})
        push(
            "  checklist: "
            f"필요 {counts.get('required', 0)} · "
            f"조건부 {counts.get('conditional', 0)} · "
            f"면제 {counts.get('exempted', 0)} · "
            f"판단보류 {counts.get('pending', 0)}"
        )
        missing = pkg.checklist_summary.get("missing_facts") or []
        if missing:
            push(f"  missing facts: {', '.join(missing[:12])}")
        push("")

    for i, req in enumerate(pkg.requirements, 1):
        kr = "✓ KR" if req.applies_to_korea else "✗ KR"
        push("")
        push(f"  [{i}] {req.measure_type}     [{kr}]")
        if req.source_goods_codes and req.source_goods_codes != [pkg.taric10]:
            push(f"      source goods_code(s): {', '.join(req.source_goods_codes)}")
        push(f"      origins: {', '.join(req.origins[:4])}{' ...' if len(req.origins) > 4 else ''}")
        if req.duty.get("rate"):
            push(f"      duty rate: {req.duty['rate']}")
        if req.duty.get("conditions"):
            for c in req.duty["conditions"][:3]:
                push(f"      condition: {c['condition_code']} → cert {c['certificate']} action {c['action_code']}")

        mand = [c for c in req.certificates if c.category == "mandatory_certificate"]
        exemp = [c for c in req.certificates if c.category == "exemption_declaration"]
        other = [c for c in req.certificates if c.category not in ("mandatory_certificate", "exemption_declaration")]

        if mand:
            push(f"      📋 필수 서류 ({len(mand)}):")
            for c in mand[:6]:
                push(f"            {c.code} — {c.description[:90]}")
            if len(mand) > 6:
                push(f"            ... +{len(mand)-6} more")
        if exemp:
            push(f"      📝 면제 사유 선언 가능 ({len(exemp)}):")
            for c in exemp[:4]:
                push(f"            {c.code} — {c.description[:90]}")
            if len(exemp) > 4:
                push(f"            ... +{len(exemp)-4} more")
        if other:
            push(f"      🔗 기타 ({len(other)}): {', '.join(c.code for c in other)}")
        if req.detailed_requirements:
            push(f"      detailed requirements ({len(req.detailed_requirements)}):")
            for detail in req.detailed_requirements[:5]:
                lookup = ""
                if detail.external_lookup_required in {"true", "conditional"}:
                    lookup = f" | external: {detail.external_lookup_required}"
                missing = f" | missing: {', '.join(detail.missing_facts[:3])}" if detail.missing_facts else ""
                push(
                    f"            [{detail.decision_label}] {detail.required_document[:80]}"
                    f" [{detail.domain_route or detail.domain}{lookup}{missing}]"
                )
            if len(req.detailed_requirements) > 5:
                push(f"            ... +{len(req.detailed_requirements)-5} more")
        if req.footnotes:
            push(f"      footnotes: {', '.join(req.footnotes[:5])}")
        if req.legal_base:
            push(f"      📜 법규: {req.legal_base}")
            if req.celex:
                push(f"            CELEX {req.celex.celex_id} [{req.celex.match_status}]")
                if req.celex.title:
                    push(f"            \"{req.celex.title[:110]}\"")
                if req.celex.excerpt:
                    push(f"            본문 발췌: {req.celex.excerpt[:200]}...")
        if req.needs_review:
            push(f"      ⚠️  needs_review")

    push("")
    push("-" * 78)
    push("  검증 URL:")
    push(f"    Access2Markets: {pkg.verification_urls['access2markets']}")
    push(f"    TARIC consult:  {pkg.verification_urls['taric_consultation']}")
    push("=" * 78)
    return "\n".join(lines)


def _dc_to_dict(obj) -> dict:
    if isinstance(obj, (Certificate, CelexRef, Requirement, DocumentPackage)):
        return {k: _dc_to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_dc_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dc_to_dict(v) for k, v in obj.items()}
    return obj


def _load_facts_json(value: str) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid facts JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Facts JSON must be an object/dict.")
    return data


def _load_facts_file(path: str) -> dict:
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return _load_facts_json(f.read())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("taric10", help="10-digit TARIC code (or 8-digit CN8 — auto-padded)")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--include-celex-excerpt", action="store_true",
                   help="Include first ~600 chars of CELEX body for each legal base")
    p.add_argument("--facts-json", default="", help="Product facts JSON object.")
    p.add_argument("--facts-file", default="", help="Path to product facts JSON file.")
    args = p.parse_args()

    try:
        product_facts = _load_facts_file(args.facts_file) if args.facts_file else _load_facts_json(args.facts_json)
        pkg = get_document_package(
            args.taric10,
            include_celex_excerpt=args.include_celex_excerpt,
            product_facts=product_facts,
        )
    except ValueError as e:
        sys.exit(f"[fatal] {e}")

    if args.format == "json":
        print(json.dumps(_dc_to_dict(pkg), ensure_ascii=False, indent=2))
    else:
        print(render_text(pkg))


if __name__ == "__main__":
    main()
