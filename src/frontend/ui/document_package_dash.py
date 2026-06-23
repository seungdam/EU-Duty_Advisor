"""
Dash UI - EU import document package workbench.

Run:
    PYTHONPATH=src python -m ui.document_package_dash

Open:
    http://127.0.0.1:8050

Optional:
    ASAP_DASH_PORT=8051 PYTHONPATH=src python -m ui.document_package_dash
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from errno import EADDRINUSE
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()
for _path in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from dash import ALL, MATCH, Dash, Input, Output, State, ctx, dcc, html, no_update

from agents.document_package import _dc_to_dict, get_document_package
from bussiness_logic.app_config import LoadAppConfig
from frontend.ui.classification_dash import display_stage_name


APP_CONFIG = LoadAppConfig(PROJECT_ROOT)
RUNS_ROOT = APP_CONFIG.paths.ResolvePath(
    PROJECT_ROOT,
    APP_CONFIG.paths.blackboard_runs_root,
)


EXAMPLES: list[tuple[str, str, dict[str, Any]]] = [
    ("0101210000", "말, 순종번식용", {"product_category": "live_animal", "animal_origin": True, "species": "horse"}),
    ("1605290000", "새우 prepared", {"product_category": "fishery", "fishery_product": True, "species": "shrimp"}),
    ("1902301000", "Pasta/Noodle", {"product_category": "food", "ingredient_list": ["wheat flour"], "intended_use": "human food"}),
    ("2103909000", "Sauces", {"product_category": "food", "intended_use": "human food"}),
    ("3304990000", "스킨케어", {"product_category": "cosmetic", "intended_use": "skin care", "full_ingredient_list": ["water"]}),
    ("0202203083", "Bovine forequarter", {"product_category": "animal_origin_food", "animal_origin": True, "species": "bovine"}),
]

DEFAULT_FACTS: dict[str, Any] = {
    "origin_country": "KR",
    "destination_market": "EU",
    "product_category": "unknown",
    "organic_claim": "unknown",
    "animal_origin": "unknown",
    "species": "unknown",
    "species_scientific_name": "unknown",
    "gmo_present": "unknown",
}

EXAMPLE_FACTS = {
    code: {**DEFAULT_FACTS, **facts, "origin_country": "KR", "destination_market": "EU"}
    for code, _, facts in EXAMPLES
}

STATUS = {
    "required": ("필요", "#b91c1c", "#fef2f2"),
    "conditional": ("조건부", "#9a3412", "#fff7ed"),
    "pending": ("판단보류", "#475569", "#f8fafc"),
    "exempted": ("면제", "#166534", "#f0fdf4"),
}

CERT_LABELS = {
    "mandatory_certificate": "필수 서류",
    "national_document": "국가/국제 문서",
    "exemption_declaration": "신고 선언문",
    "preferential_origin": "우대 원산지 증빙",
    "import_license": "수입 라이선스",
    "other": "기타 코드",
    "unknown": "분류 미상",
}


app = Dash(__name__, suppress_callback_exceptions=True)
server = app.server


def _preferred_port(default: int = 8050) -> int:
    raw = os.getenv("ASAP_DASH_PORT") or os.getenv("PORT")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid ASAP_DASH_PORT/PORT value {raw!r}; falling back to {default}.")
        return default


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno != EADDRINUSE:
                raise
            return False
    return True


def _select_port(host: str, preferred: int) -> int:
    if _port_available(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        fallback = int(sock.getsockname()[1])
    print(f"Port {preferred} is already in use; starting Dash on {host}:{fallback} instead.")
    return fallback


app.index_string = """
<!DOCTYPE html>
<html>
  <head>
    {%metas%}
    <title>ASAP EU Document Package</title>
    {%favicon%}
    {%css%}
    <style>
      :root {
        --bg: #f5f7fb;
        --panel: #ffffff;
        --line: #e5e7eb;
        --muted: #64748b;
        --text: #111827;
        --blue: #2563eb;
        --red: #b91c1c;
        --green: #166534;
        --amber: #9a3412;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        background: var(--bg);
        color: var(--text);
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .shell { display: flex; min-height: 100vh; }
      .sidebar {
        width: 314px;
        flex: 0 0 314px;
        background: #ffffff;
        border-right: 1px solid var(--line);
        padding: 28px 22px;
        overflow-y: auto;
      }
      .main {
        flex: 1;
        min-width: 0;
        padding: 28px 34px 42px;
        overflow-y: auto;
      }
      .brand { font-size: 22px; font-weight: 900; letter-spacing: -0.01em; margin: 0 0 6px; }
      .subtle { color: var(--muted); font-size: 12px; line-height: 1.45; }
      .divider { height: 1px; background: var(--line); margin: 22px 0; }
      .label { font-size: 11px; font-weight: 850; color: #475569; margin: 12px 0 7px; }
      .input {
        width: 100%;
        border: 1px solid #cbd5e1;
        border-radius: 9px;
        padding: 12px 13px;
        font-size: 14px;
        background: #ffffff;
        color: var(--text);
      }
      .textarea {
        width: 100%;
        min-height: 160px;
        border: 1px solid #cbd5e1;
        border-radius: 9px;
        padding: 10px 11px;
        font-size: 12px;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        background: #ffffff;
        color: var(--text);
      }
      button {
        font-family: inherit;
      }
      .primary-btn {
        width: 100%;
        border: 1px solid var(--blue);
        border-radius: 9px;
        background: var(--blue);
        color: #ffffff;
        font-weight: 900;
        padding: 12px 13px;
        cursor: pointer;
      }
      .example-btn {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 9px;
        background: #ffffff;
        color: var(--text);
        padding: 12px 13px;
        margin-bottom: 9px;
        text-align: left;
        cursor: pointer;
      }
      .example-btn:hover { border-color: var(--blue); box-shadow: 0 0 0 2px rgba(37,99,235,0.08); }
      .example-code { font-size: 14px; font-weight: 900; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
      .example-label { font-size: 12px; color: #475569; margin-top: 3px; }
      .topbar {
        display: grid;
        grid-template-columns: minmax(280px, 1fr) 132px;
        gap: 10px;
        align-items: center;
        margin-bottom: 14px;
      }
      .title { font-size: 25px; font-weight: 950; letter-spacing: -0.02em; margin: 0; }
      .caption { color: var(--muted); font-size: 12px; margin-top: 5px; }
      .metric-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 9px;
        margin: 12px 0 10px;
      }
      .metric {
        background: #ffffff;
        border: 1px solid var(--line);
        border-radius: 9px;
        padding: 11px 12px;
        min-height: 70px;
      }
      .metric-label { font-size: 10px; color: var(--muted); font-weight: 900; }
      .metric-value { font-size: 19px; color: var(--text); font-weight: 950; margin-top: 4px; overflow-wrap: anywhere; }
      .flow {
        background: #ffffff;
        border: 1px solid var(--line);
        border-radius: 12px;
        padding: 16px;
        margin: 10px 0 12px;
        box-shadow: 0 1px 2px rgba(15,23,42,0.04);
      }
      .flow-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        gap: 10px;
      }
      .node {
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 11px 12px;
        background: #ffffff;
        min-height: 76px;
      }
      .node-kicker { font-size: 10px; color: var(--muted); font-weight: 900; }
      .node-main { font-size: 15px; color: var(--text); font-weight: 950; margin-top: 5px; overflow-wrap: anywhere; }
      .node-blue { border-left: 4px solid var(--blue); }
      .node-red { border-left: 4px solid var(--red); }
      .node-amber { border-left: 4px solid var(--amber); background: #fff7ed; }
      .node-green { border-left: 4px solid var(--green); background: #f0fdf4; }
      .panel-buttons {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(142px, 1fr));
        gap: 8px;
        margin: 12px 0;
      }
      .panel-btn {
        min-height: 58px;
        border: 1px solid #cbd5e1;
        background: #ffffff;
        color: var(--text);
        border-radius: 9px;
        padding: 10px 11px;
        text-align: left;
        font-weight: 900;
        line-height: 1.24;
        cursor: pointer;
      }
      .panel-btn:hover { border-color: var(--blue); color: #1d4ed8; }
      .panel-btn.active { background: var(--blue); border-color: var(--blue); color: #ffffff; }
      .panel {
        background: #ffffff;
        border: 1px solid var(--line);
        border-radius: 12px;
        padding: 16px;
        margin-top: 10px;
      }
      .section-title { font-size: 15px; color: var(--text); font-weight: 950; margin-bottom: 10px; }
      .card {
        background: #ffffff;
        border: 1px solid var(--line);
        border-radius: 9px;
        padding: 11px 12px;
        margin-bottom: 9px;
      }
      .card-title { font-size: 13px; font-weight: 950; color: var(--text); }
      .card-meta { font-size: 11px; color: var(--muted); margin-top: 4px; line-height: 1.4; }
      .two-col { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
      .three-col { display: grid; grid-template-columns: 1fr 1.25fr 1.7fr; gap: 10px; align-items: start; }
      .scenario-shell {
        border: 1px solid var(--line);
        border-radius: 12px;
        background: #ffffff;
        padding: 14px;
        margin-bottom: 14px;
      }
      .scenario-head {
        display: grid;
        grid-template-columns: minmax(150px, 0.75fr) minmax(260px, 1.25fr);
        gap: 12px;
        align-items: stretch;
      }
      .scenario-code {
        border: 1px solid var(--line);
        border-left: 4px solid var(--blue);
        border-radius: 10px;
        padding: 12px;
        background: #f8fafc;
      }
      .scenario-checks {
        border: 1px solid var(--line);
        border-radius: 10px;
        padding: 10px 12px;
        background: #ffffff;
      }
      .scenario-checks label {
        display: block;
        margin: 7px 0;
        font-size: 12px;
        font-weight: 850;
        color: #334155;
      }
      .scenario-checks input { margin-right: 8px; }
      .scenario-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 10px;
        margin-top: 12px;
      }
      .scenario-card {
        border: 1px solid var(--line);
        border-left: 4px solid #475569;
        border-radius: 10px;
        padding: 12px;
        background: #ffffff;
        min-height: 164px;
      }
      .scenario-card.green { border-left-color: var(--green); background: #f0fdf4; }
      .scenario-card.amber { border-left-color: var(--amber); background: #fff7ed; }
      .scenario-card.red { border-left-color: var(--red); background: #fef2f2; }
      .scenario-duty {
        font-size: 28px;
        line-height: 1.05;
        letter-spacing: 0;
        font-weight: 950;
        margin-top: 7px;
      }
      .scenario-actions {
        margin: 9px 0 0;
        padding-left: 18px;
        color: #374151;
        font-size: 11px;
        line-height: 1.4;
      }
      .scenario-window {
        border: 1px solid #dbe3ef;
        border-radius: 10px;
        background: rgba(255,255,255,0.78);
        margin-top: 12px;
        overflow: hidden;
      }
      .scenario-window-head {
        display: flex;
        justify-content: space-between;
        gap: 10px;
        align-items: center;
        border-bottom: 1px solid #e5e7eb;
        padding: 9px 10px;
        background: #f8fafc;
      }
      .scenario-window-title { font-size: 12px; font-weight: 950; color: #111827; }
      .scenario-window-count { font-size: 10px; font-weight: 900; color: #64748b; }
      .scenario-window-body { padding: 9px 10px 10px; }
      .scenario-doc-row {
        display: grid;
        grid-template-columns: minmax(140px, 1fr) auto;
        gap: 8px;
        align-items: start;
        padding: 7px 0;
        border-bottom: 1px solid #edf2f7;
      }
      .scenario-doc-row:last-child { border-bottom: 0; }
      .scenario-doc-name { font-size: 12px; font-weight: 900; color: #111827; }
      .scenario-doc-code {
        font-size: 10px;
        color: #64748b;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        margin-top: 2px;
      }
      .scenario-doc-meta { font-size: 10px; color: #64748b; line-height: 1.35; margin-top: 2px; }
      .scenario-detail summary {
        color: #1d4ed8;
        font-size: 11px;
        font-weight: 900;
      }
      .scenario-doc-fields {
        margin-top: 7px;
        padding: 7px;
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        background: #ffffff;
      }
      .scenario-field-row {
        padding: 5px 0;
        border-bottom: 1px solid #edf2f7;
        font-size: 10px;
        color: #475569;
      }
      .scenario-field-row:last-child { border-bottom: 0; }
      .badge {
        display: inline-block;
        padding: 2px 7px;
        border-radius: 999px;
        font-size: 10px;
        font-weight: 900;
        margin-left: 6px;
      }
      .chip {
        display: inline-block;
        margin: 0 6px 6px 0;
        padding: 5px 8px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: #ffffff;
        color: #334155;
        font-size: 11px;
      }
      .cert {
        background: #ffffff;
        border: 1px solid var(--line);
        border-left: 4px solid #475569;
        border-radius: 9px;
        padding: 9px 10px;
        margin-bottom: 8px;
      }
      .cert-code { font-size: 13px; font-weight: 950; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
      .cert-desc { font-size: 11px; color: #334155; margin-top: 3px; line-height: 1.38; }
      .cert-help {
        background: #f8fafc;
        border: 1px solid var(--line);
        border-radius: 7px;
        padding: 7px;
        font-size: 10px;
        color: #334155;
        margin-top: 7px;
        line-height: 1.35;
      }
      .cert-detail summary {
        cursor: pointer;
        color: #1d4ed8;
        font-size: 11px;
        font-weight: 850;
        margin-top: 8px;
      }
      .cert-detail[open] summary { margin-bottom: 7px; }
      .guidance-row { margin-top: 6px; }
      .guidance-label { font-weight: 900; color: #111827; }
      .detail-card {
        border: 1px solid var(--line);
        border-left: 4px solid #475569;
        border-radius: 9px;
        padding: 10px 11px;
        margin-bottom: 9px;
        background: #ffffff;
      }
      .lookup {
        background: #fff7ed;
        border: 1px solid #fed7aa;
        color: #7c2d12;
        border-radius: 7px;
        padding: 7px;
        margin-top: 7px;
        font-size: 10px;
        line-height: 1.35;
      }
      details {
        margin-top: 12px;
      }
      summary {
        cursor: pointer;
        color: #334155;
        font-size: 12px;
        font-weight: 900;
      }
      .error {
        background: #fef2f2;
        border: 1px solid #fecaca;
        color: #7f1d1d;
        border-radius: 9px;
        padding: 10px 12px;
        margin-top: 10px;
        font-size: 12px;
        font-weight: 800;
      }
      .empty {
        background: #ffffff;
        border: 1px dashed #cbd5e1;
        border-radius: 12px;
        padding: 24px;
        color: #475569;
        font-size: 13px;
      }
      a { color: #1d4ed8; text-decoration: none; font-weight: 850; }
      a:hover { text-decoration: underline; }
      @media (max-width: 980px) {
        .shell { display: block; }
        .sidebar { width: auto; border-right: 0; border-bottom: 1px solid var(--line); }
        .main { padding: 22px 18px 32px; }
        .topbar { grid-template-columns: 1fr; }
        .two-col, .three-col { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    {%app_entry%}
    <footer>{%config%}{%scripts%}{%renderer%}</footer>
  </body>
</html>
"""


def clean_code(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def is_taric_like(value: str) -> bool:
    text = (value or "").strip()
    if re.match(r"^(https?://|www\.)", text, flags=re.IGNORECASE):
        return False
    return bool(re.fullmatch(r"[\d\s.\-]+", text)) and len(clean_code(text)) >= 8


def fmt_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def parse_facts_json(facts_text: str) -> dict[str, Any]:
    text = facts_text or "{}"
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("facts JSON must be an object")
    return parsed


def status_badge(status: str) -> html.Span:
    label, color, bg = STATUS.get(status or "pending", ("검토", "#475569", "#f8fafc"))
    return html.Span(label, className="badge", style={"color": color, "backgroundColor": bg, "border": f"1px solid {color}33"})


def duty_rate(req: dict[str, Any] | None) -> str:
    if not req:
        return "없음"
    return (req.get("duty") or {}).get("rate") or "조건부"


def cert_color(category: str) -> str:
    if category in {"mandatory_certificate", "national_document", "import_license"}:
        return "#b91c1c"
    if category == "preferential_origin":
        return "#166534"
    if category == "exemption_declaration":
        return "#1d4ed8"
    return "#475569"


def cert_help(cert: dict[str, Any]) -> str:
    guidance = cert.get("guidance") or {}
    if guidance.get("certificate_description") or guidance.get("when_required"):
        return guidance.get("certificate_description") or guidance.get("when_required") or ""
    category = cert.get("category") or "unknown"
    role = {
        "mandatory_certificate": "C-code: TARIC 조건에서 요구되는 certificate/document 코드입니다.",
        "national_document": "N-code: 국가/국제 표준 문서 또는 관련 증빙 코드입니다.",
        "exemption_declaration": "Y-code: 통관 신고에 입력하는 declaration/waiver 코드입니다.",
        "preferential_origin": "U-code: 우대관세 또는 원산지 증빙 관련 코드입니다.",
        "import_license": "L-code: licence/authorisation 관련 코드입니다.",
    }.get(category, "TARIC certificate/declaration 코드입니다.")
    return f"{role} 코드별 세부 의미는 TARIC description 기준입니다: {cert.get('description') or 'description 없음'}"


def guidance_row(label: str, value: Any) -> html.Div | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return html.Div([html.Span(label + ": ", className="guidance-label"), html.Span(text)], className="guidance-row")


def _compact_text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(*values: Any) -> str:
    for value in values:
        text = _compact_text(value)
        if text:
            return text
    return ""


def _cert_kind_label(category: str) -> str:
    if category == "exemption_declaration":
        return "선언문"
    if category == "preferential_origin":
        return "우대 증빙"
    if category in {"mandatory_certificate", "national_document"}:
        return "증명서/서류"
    if category == "import_license":
        return "수입 라이선스"
    return "certificate/declaration 코드"


def _readable_evidence(guidance: dict[str, Any]) -> str:
    evidence = _compact_text(guidance.get("required_evidence"))
    if not evidence:
        return ""
    return ", ".join(part.strip() for part in re.split(r"[;,]", evidence) if part.strip())


def _cert_topic(cert: dict[str, Any], guidance: dict[str, Any]) -> str:
    title = _first_text(guidance.get("certificate_description"), cert.get("description"), guidance.get("guidance_title"))
    code = _compact_text(cert.get("code"))
    if title.lower().startswith((code or "").lower()):
        return title
    category = cert.get("category") or "unknown"
    topic = f"{code}는 {title}에 관한 {_cert_kind_label(category)}입니다." if title else cert_help(cert)
    evidence = _readable_evidence(guidance)
    if not evidence:
        return topic
    if category == "exemption_declaration":
        return f"{topic} {evidence} 확인 후 수입신고서의 declaration/supporting document code로 선언합니다."
    if category == "preferential_origin":
        return f"{topic} {evidence} 확인 후 우대관세 신청 시 원산지 증빙 코드와 문서번호를 신고서에 기재합니다."
    return f"{topic} {evidence} 확인 후 해당 증명서/문서번호를 수입신고 supporting document로 제출 또는 기재합니다."


def _certificate_content_label(category: str) -> str:
    return "선언문 내용" if category == "exemption_declaration" else "문서 내용"


def _certificate_content(cert: dict[str, Any], guidance: dict[str, Any]) -> str:
    category = cert.get("category") or "unknown"
    if category == "exemption_declaration":
        return _first_text(guidance.get("declaration_wording"), guidance.get("certificate_description"), cert.get("description"))
    return _first_text(guidance.get("certificate_description"), cert.get("description"), guidance.get("guidance_title"))


def _certificate_condition(cert: dict[str, Any], guidance: dict[str, Any]) -> str:
    category = cert.get("category") or "unknown"
    if category == "exemption_declaration":
        return _first_text(guidance.get("not_applicable_condition"), guidance.get("when_required"))
    return _first_text(guidance.get("when_required"), guidance.get("not_applicable_condition"))


def cert_guidance_detail(cert: dict[str, Any]) -> html.Details:
    guidance = cert.get("guidance") or {}
    rows = [
        guidance_row("설명", _cert_topic(cert, guidance)),
        guidance_row(_certificate_content_label(cert.get("category") or "unknown"), _certificate_content(cert, guidance)),
        guidance_row("해당 조건", _certificate_condition(cert, guidance)),
        guidance_row("근거", guidance.get("source_basis") or guidance.get("source_legal_bases") or cert.get("description")),
        guidance_row("CELEX", guidance.get("source_celex_ids")),
    ]
    rows = [row for row in rows if row is not None]
    if not rows:
        rows = [html.Div(cert_help(cert), className="cert-help")]
    return html.Details(
        [html.Summary("상세 설명"), html.Div(rows, className="cert-help")],
        className="cert-detail",
    )


def cert_card(cert: dict[str, Any]) -> html.Div:
    category = cert.get("category") or "unknown"
    color = cert_color(category)
    guidance = cert.get("guidance") or {}
    return html.Div(
        [
            html.Div(cert.get("code") or "", className="cert-code", style={"color": color}),
            html.Div(guidance.get("guidance_title") or cert.get("description") or "description 없음", className="cert-desc"),
            cert_guidance_detail(cert),
        ],
        className="cert",
        style={"borderLeftColor": color},
    )


def detail_card(detail: dict[str, Any], label: str, related_declarations: list[str] | None = None) -> html.Div:
    status = detail.get("decision_status") or "pending"
    _, color, _ = STATUS.get(status, ("검토", "#475569", "#f8fafc"))
    missing = ", ".join((detail.get("missing_facts") or [])[:5]) or "없음"
    facts = ", ".join((detail.get("required_facts") or [])[:5]) or "없음"
    declarations = ", ".join((related_declarations or [])[:5]) or ""
    lookups = detail.get("external_dataset_ids") or []
    lookup = None
    if lookups:
        lookup_label = "외부참조 필요" if detail.get("external_lookup_required") == "true" else "외부참조 조건부"
        lookup = html.Div(
            [
                html.B(lookup_label),
                html.Span(": " + ", ".join(lookups[:5])),
                html.Br(),
                html.Span(detail.get("external_lookup_mode") or detail.get("data_gap_status") or ""),
            ],
            className="lookup",
        )
    return html.Div(
        [
            html.Div([html.Span(f"{label} · {detail.get('required_level') or ''}", style={"color": color, "fontWeight": 950}), status_badge(status)]),
            html.Div(detail.get("required_document") or "문서명 없음", className="card-title", style={"marginTop": "5px"}),
            html.Div(f"{detail.get('domain_route') or detail.get('domain') or '-'} · {detail.get('requirement_type') or '-'}", className="card-meta"),
            html.Div([html.B("필요 facts: "), html.Span(facts)], className="card-meta", style={"color": "#334155"}),
            html.Div([html.B("관련 선언/면제: "), html.Span(declarations)], className="card-meta", style={"color": "#334155"}) if declarations else None,
            html.Div([html.B("누락: "), html.Span(missing)], className="card-meta", style={"color": "#9a3412"}),
            lookup,
        ],
        className="detail-card",
        style={"borderLeftColor": color},
    )


def metric(label: str, value: Any, color: str = "#111827", mono: bool = False) -> html.Div:
    return html.Div(
        [
            html.Div(label, className="metric-label"),
            html.Div(str(value), className="metric-value", style={"color": color, "fontFamily": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" if mono else "inherit"}),
        ],
        className="metric",
    )


def node(kicker: str, value: Any, cls: str = "node-blue") -> html.Div:
    return html.Div([html.Div(kicker, className="node-kicker"), html.Div(str(value), className="node-main")], className=f"node {cls}")


def package_context(pkg: dict[str, Any]) -> dict[str, Any]:
    view_context = document_view_context(pkg)
    if view_context:
        return view_context
    return _unresolved_context(pkg)


def _unresolved_context(pkg: dict[str, Any]) -> dict[str, Any]:
    return {
        "kr": [],
        "non_kr": [],
        "controls": [],
        "duties": [],
        "base_duty_measures": [],
        "preferential_measures": [],
        "third_country": None,
        "fta_pref": None,
        "additional_duty": None,
        "groups": [],
        "counts": {},
        "missing": [],
        "product_reqs": [],
        "product_pre": [],
        "product_post": [],
        "related_declarations": {},
        "source": "unresolved",
        "_raw_package": pkg,
    }


def document_view_context(pkg: dict[str, Any]) -> dict[str, Any] | None:
    """Use DocumentAgent's view model when a pipeline package provides it.

    Direct TARIC lookup still falls back to raw package splitting. Pipeline
    detail pages should be display-only consumers of Document_Agent output.
    """
    view = pkg.get("_document_view") or pkg.get("document_view")
    if not isinstance(view, dict):
        return None
    sections = view.get("sections") or {}
    if not isinstance(sections, dict):
        return None

    overview = sections.get("overview") or {}
    customs = sections.get("customs_check_items") or {}
    basic = sections.get("basic_duty") or {}
    preferential = sections.get("preferential_evidence") or {}
    required_docs = sections.get("required_documents") or {}
    product = sections.get("product_regulations") or {}
    checklist = sections.get("document_checklist") or {}
    pre_taric_checks = sections.get("pre_taric_checks") or {}

    reqs = pkg.get("requirements") or []
    kr = [r for r in reqs if r.get("applies_to_korea")]
    non_kr = [r for r in reqs if not r.get("applies_to_korea")]
    controls = customs.get("render_bucket") or customs.get("agent_bucket") or []
    base_duty_measures = basic.get("render_bucket") or basic.get("agent_bucket") or []
    preferential_measures = preferential.get("render_bucket") or preferential.get("agent_bucket") or []
    groups = required_docs.get("document_groups") or []
    product_reqs = product.get("requirements") or []

    return {
        "kr": kr,
        "non_kr": non_kr,
        "controls": controls,
        "duties": list(base_duty_measures) + list(preferential_measures),
        "base_duty_measures": base_duty_measures,
        "preferential_measures": preferential_measures,
        "third_country": overview.get("third_country_duty"),
        "fta_pref": overview.get("fta_preference"),
        "additional_duty": overview.get("additional_duty"),
        "groups": groups,
        "document_checklist": checklist,
        "baseline_documents": checklist.get("documents") or [],
        "pre_taric_checks": pre_taric_checks.get("checks") or checklist.get("pre_taric_checks") or [],
        "counts": overview.get("counts") or {},
        "missing": overview.get("missing_facts") or [],
        "product_reqs": product_reqs,
        "product_pre": product.get("pre") or [],
        "product_post": product.get("post") or [],
        "related_declarations": product.get("related_declarations") or {},
        "document_view": view,
        "source": "document_view",
    }


def render_shell() -> html.Div:
    return html.Div(
        [
            dcc.Store(id="package-store"),
            dcc.Store(id="panel-store", data="overview"),
            html.Aside(
                [
                    html.H1("ASAP 서류 패키지", className="brand"),
                    html.Div("TARIC10 기준 EU 수입 관세/서류/제품규제 확인", className="subtle"),
                    html.Div(className="divider"),
                    html.Div("예제", className="label"),
                    html.Div(
                        [
                            html.Button(
                                [html.Div(code, className="example-code"), html.Div(label, className="example-label")],
                                id={"type": "example-btn", "code": code},
                                className="example-btn",
                            )
                            for code, label, _ in EXAMPLES
                        ]
                    ),
                    html.Div(className="divider"),
                    html.Details(
                        [
                            html.Summary("Product facts JSON"),
                            dcc.Textarea(id="facts-input", value=fmt_json(DEFAULT_FACTS), className="textarea"),
                        ]
                    ),
                    html.Div(className="divider"),
                    dcc.Checklist(
                        id="options",
                        options=[
                            {"label": " CELEX 본문 발췌 포함", "value": "celex"},
                            {"label": " 한국 무관 measure 표시", "value": "nonkr"},
                            {"label": " Raw JSON 표시", "value": "raw"},
                            {"label": " Blackboard log 표시", "value": "blackboard"},
                        ],
                        value=[],
                        className="subtle",
                    ),
                ],
                className="sidebar",
            ),
            html.Main(
                [
                    html.Div([html.H2("EU 수출 서류 패키지", className="title"), html.Div("기본 화면은 결론만, 세부 설명은 카드 클릭으로 확인합니다.", className="caption")]),
                    html.Div(
                        [
                            dcc.Input(id="code-input", value="", placeholder="TARIC 코드 입력", className="input"),
                            html.Button("조회", id="search-btn", className="primary-btn"),
                        ],
                        className="topbar",
                    ),
                    html.Div(id="error-box"),
                    html.Div(id="result-root", className="empty", children="TARIC 코드를 입력하거나 좌측 예제를 선택하세요."),
                ],
                className="main",
            ),
        ],
        className="shell",
    )


app.layout = render_shell


@app.callback(
    Output("package-store", "data"),
    Output("error-box", "children"),
    Output("code-input", "value"),
    Output("facts-input", "value"),
    Output("panel-store", "data"),
    Input("search-btn", "n_clicks"),
    Input({"type": "example-btn", "code": ALL}, "n_clicks"),
    State("code-input", "value"),
    State("facts-input", "value"),
    State("options", "value"),
    prevent_initial_call=True,
)
def load_package(_search_clicks, _example_clicks, code_value, facts_text, options):
    triggered = ctx.triggered_id
    if isinstance(triggered, dict) and triggered.get("type") == "example-btn":
        code = triggered["code"]
        facts = EXAMPLE_FACTS.get(code, DEFAULT_FACTS)
        facts_text = fmt_json(facts)
    else:
        code = code_value or ""
        try:
            facts = parse_facts_json(facts_text or "{}")
        except Exception as exc:
            return no_update, html.Div(f"Product facts JSON 오류: {exc}", className="error"), no_update, no_update, no_update

    try:
        if not is_taric_like(code):
            return no_update, html.Div("TARIC 코드를 입력하세요.", className="error"), code, facts_text, no_update
        package = get_document_package(
            code,
            include_celex_excerpt="celex" in (options or []),
            product_facts=facts,
        )
        package_data = _dc_to_dict(package)
        display_code = package.taric10
    except Exception as exc:
        return no_update, html.Div(f"조회 오류: {exc}", className="error"), code, facts_text, no_update

    return package_data, None, display_code, facts_text, "overview"


@app.callback(
    Output("panel-store", "data", allow_duplicate=True),
    Input({"type": "panel-btn", "panel": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def select_panel(_clicks):
    triggered = ctx.triggered_id
    if isinstance(triggered, dict) and triggered.get("type") == "panel-btn":
        return triggered.get("panel") or "overview"
    return no_update


@app.callback(
    Output("result-root", "children"),
    Input("package-store", "data"),
    Input("panel-store", "data"),
    Input("options", "value"),
)
def render_result(pkg, panel, options):
    if not pkg:
        return "TARIC 코드를 입력하거나 좌측 예제를 선택하세요."
    if not pkg.get("has_data"):
        return html.Div("이 코드에 대한 현재 적용 measure가 없습니다.", className="empty")

    cx = package_context(pkg)
    if cx.get("source") == "unresolved":
        return render_unresolved(pkg, options or [])
    third_country = cx["third_country"]
    fta_pref = cx["fta_pref"]
    final_duty = fta_pref or third_country
    counts = cx["counts"]
    groups = cx["groups"]
    baseline_documents = cx.get("baseline_documents") or []
    additional_documents = _additional_detail_documents(baseline_documents)
    controls = cx["controls"]
    duties = cx["duties"]
    product_reqs = cx["product_reqs"]
    selected = panel or "overview"
    product_count = (
        len(cx.get("product_pre") or [])
        + len(cx.get("product_post") or [])
        or sum(len(r.get("detailed_requirements") or []) for r in product_reqs)
    )

    panel_defs = [
        ("overview", "전체 결론", "요약"),
        ("scenario", "시나리오", "기본/우대"),
        ("bundles", "추가 상세서류", f"{len(additional_documents) or len(groups)}개"),
    ]

    children = [
        html.Div(
            [
                metric("TARIC10", pkg.get("taric10"), "#1d4ed8", True),
                metric("CN8", pkg.get("cn8"), "#1d4ed8", True),
                metric("최종 관세율 후보", duty_rate(final_duty), "#166534"),
                metric("KR measure", len(cx["kr"])),
                metric("필요 / 조건부", f"{counts.get('required', 0)} / {counts.get('conditional', 0)}", "#9a3412"),
                metric("판단보류", counts.get("pending", 0), "#475569"),
            ],
            className="metric-grid",
        ),
        html.Div(
            [
                html.Div(
                    [
                        node("CODE", pkg.get("taric10"), "node-blue"),
                        node("통관 조건", f"{len(controls)} control / {len(duties)} duty", "node-red"),
                        node("기본 관세", duty_rate(third_country), "node-amber"),
                        node("FTA 우대 가능 시", duty_rate(fta_pref), "node-green"),
                        node("추가 상세서류", f"{len(additional_documents) or len(groups)} docs", "node-blue"),
                    ],
                    className="flow-grid",
                )
            ],
            className="flow",
        ),
        html.Div(
            [
                html.Button([html.Div(title), html.Div(sub, style={"fontSize": "11px", "fontWeight": 700, "marginTop": "3px"})], id={"type": "panel-btn", "panel": pid}, className=f"panel-btn {'active' if selected == pid else ''}")
                for pid, title, sub in panel_defs
            ],
            className="panel-buttons",
        ),
        html.Div(render_panel(pkg, selected, cx, options or []), className="panel"),
    ]
    return children


def render_unresolved(pkg: dict[str, Any], options: list[str]):
    taric10 = pkg.get("taric10") or "-"
    children = [
        html.Div(
            [
                html.Div("⚠ document_view missing", className="metric-label", style={"color": "#b91c1c"}),
                html.Div(
                    f"TARIC10 {taric10} 의 분류 결과를 받지 못했습니다.",
                    style={"fontSize": "15px", "fontWeight": 600, "marginTop": "6px"},
                ),
                html.Div(
                    "DocumentAgent 가 sections 을 채우지 못했거나, direct TARIC 조회로 pipeline 을 거치지 않았습니다. 관리자에게 pipeline 재실행을 요청하세요.",
                    style={"fontSize": "13px", "color": "#475569", "marginTop": "4px"},
                ),
            ],
            className="empty",
            style={"borderLeft": "4px solid #b91c1c", "padding": "16px"},
        ),
    ]
    if "admin" in (options or []) or "debug" in (options or []):
        children.append(
            html.Details(
                [
                    html.Summary("raw_document_package (admin debug)"),
                    html.Pre(
                        str(pkg)[:4000],
                        style={"fontSize": "11px", "color": "#334155", "whiteSpace": "pre-wrap"},
                    ),
                ],
                style={"marginTop": "16px"},
            )
        )
    return children


def render_panel(pkg: dict[str, Any], panel: str, cx: dict[str, Any], options: list[str]):
    if panel == "scenario":
        return render_trade_scenario(pkg, cx)
    if panel == "customs":
        return render_customs(pkg, cx["controls"])
    if panel == "base_duty":
        return render_base_duty(cx["base_duty_measures"])
    if panel == "preferential":
        return render_preferential(cx["preferential_measures"])
    if panel == "bundles":
        if cx.get("baseline_documents"):
            return render_additional_documents(
                _additional_detail_documents(cx.get("baseline_documents") or []),
                cx.get("pre_taric_checks") or [],
                cx.get("groups") or [],
            )
        return render_bundles(cx["groups"])
    if panel == "product":
        return render_product_rules_from_view(
            cx.get("product_pre") or [],
            cx.get("product_post") or [],
            cx.get("related_declarations") or {},
        )
    return render_overview(cx, options, pkg)


def _scenario_cert_codes(reqs: list[dict[str, Any]], categories: set[str] | None = None) -> list[str]:
    codes: set[str] = set()
    for req in reqs:
        for cert in req.get("certificates") or []:
            category = cert.get("category") or "unknown"
            if categories is None or category in categories:
                code = cert.get("code")
                if code:
                    codes.add(str(code))
    return sorted(codes)


def _doc_name(doc: dict[str, Any]) -> str:
    return str(doc.get("document_name_ko") or doc.get("document_name") or doc.get("document_code") or "제출서류")


def _doc_code(doc: dict[str, Any]) -> str:
    return str(doc.get("document_code") or "")


def _doc_status(doc: dict[str, Any]) -> str:
    return str(doc.get("decision_status") or doc.get("required_level") or "conditional")


BASELINE_CORE_DOCUMENT_CODES = {
    "COMMERCIAL_INVOICE",
    "PACKING_LIST",
    "BL_AWB",
    "DELIVERY_NOTE",
}


def _additional_detail_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    detailed = []
    for doc in documents:
        code = _doc_code(doc)
        if code in BASELINE_CORE_DOCUMENT_CODES:
            continue
        if (
            doc.get("taric_certificates")
            or doc.get("pre_checks")
            or doc.get("post_requirements")
            or code in {"ORIGIN_PROOF", "PRODUCT_SPEC", "INGREDIENT_LIST", "COA", "SDS", "LABEL_ARTWORK", "HEALTH_CERT_SUPPORT", "ORGANIC_COI", "CITES_SPECIES_EVIDENCE"}
        ):
            detailed.append(doc)
    return detailed


def _scenario_field_rows(fields: list[dict[str, Any]]) -> list[html.Div]:
    rows = []
    for field in fields[:8]:
        rows.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(field.get("label") or field.get("field_key") or "작성항목", style={"fontWeight": 850, "color": "#111827"}),
                            html.Span(" · "),
                            status_badge(field.get("status") or "conditional"),
                        ]
                    ),
                    html.Div("required_by: " + (", ".join(field.get("required_by") or []) or "baseline")),
                    html.Div(
                        "추가 확인: " + (", ".join((field.get("missing_facts") or [])[:4]) or "없음"),
                        style={"color": "#9a3412"},
                    ),
                ],
                className="scenario-field-row",
            )
        )
    return rows


def _scenario_documents(cx: dict[str, Any], scenario: str) -> list[dict[str, Any]]:
    docs = cx.get("baseline_documents") or []
    required_docs = [doc for doc in docs if _doc_status(doc) == "required"]
    selected: list[dict[str, Any]] = list(required_docs)

    def include_by_code(*codes: str) -> None:
        code_set = set(codes)
        selected.extend([doc for doc in docs if _doc_code(doc) in code_set])

    if scenario == "fta":
        include_by_code("ORIGIN_PROOF", "PRODUCT_SPEC", "INGREDIENT_LIST")
    elif scenario == "basic":
        include_by_code("PRODUCT_SPEC", "INGREDIENT_LIST", "COA")
    elif scenario == "control":
        selected.extend(
            [
                doc
                for doc in docs
                if doc.get("taric_certificates")
                or doc.get("pre_checks")
                or doc.get("post_requirements")
            ]
        )

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for doc in selected:
        code = _doc_code(doc) or _doc_name(doc)
        if code in seen:
            continue
        seen.add(code)
        deduped.append(doc)
    return deduped


def _scenario_document_window(
    cx: dict[str, Any],
    scenario: str,
    cert_codes: list[str] | None,
    title: str = "이 시나리오 제출 창",
) -> html.Div:
    docs = _scenario_documents(cx, scenario)
    cert_codes = cert_codes or []
    rows = []
    for doc in docs[:8]:
        pre_count = len(doc.get("pre_checks") or [])
        post_count = len(doc.get("post_requirements") or [])
        fields = doc.get("fields") or []
        field_preview = ", ".join(
            str(field.get("label") or field.get("field_key") or "")
            for field in fields[:4]
            if field.get("label") or field.get("field_key")
        ) or "정의 없음"
        missing = ", ".join((doc.get("missing_facts") or [])[:3]) or "없음"
        rows.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(_doc_name(doc), className="scenario-doc-name"),
                            html.Div(_doc_code(doc), className="scenario-doc-code"),
                            html.Div(
                                f"사전 {pre_count} · 상세 {post_count} · 추가 확인: {missing}",
                                className="scenario-doc-meta",
                            ),
                            html.Div(
                                f"작성항목: {field_preview}",
                                className="scenario-doc-meta",
                                style={"color": "#334155"},
                            ),
                            html.Details(
                                [
                                    html.Summary(f"작성항목 {len(fields)}개"),
                                    html.Div(
                                        _scenario_field_rows(fields) or html.Div("작성항목 정의 없음", className="card-meta"),
                                        className="scenario-doc-fields",
                                    ),
                                ],
                                className="scenario-detail",
                            ),
                        ]
                    ),
                    status_badge(_doc_status(doc)),
                ],
                className="scenario-doc-row",
            )
        )
    cert_block = html.Details(
        [
            html.Summary(f"세부 서류/선언 코드 {len(cert_codes)}개"),
            html.Div(
                [html.Span(code, className="chip") for code in cert_codes[:12]]
                if cert_codes
                else html.Div("이 시나리오에 별도 TARIC certificate/declaration code가 없습니다.", className="card-meta"),
                style={"marginTop": "8px"},
            ),
        ],
        className="scenario-detail",
    )
    return html.Div(
        [
            html.Div(
                [
                    html.Div(title, className="scenario-window-title"),
                    html.Div(f"baseline {len(docs)} · 세부 코드 {len(cert_codes)}", className="scenario-window-count"),
                ],
                className="scenario-window-head",
            ),
            html.Div(
                [
                    html.Div(rows or html.Div("연결된 baseline 제출서류가 없습니다.", className="card-meta")),
                    cert_block,
                ],
                className="scenario-window-body",
            ),
        ],
        className="scenario-window",
    )


def _scenario_card(
    title: str,
    duty: str,
    basis: str,
    actions: list[str],
    color_class: str,
    cert_codes: list[str] | None = None,
    document_window: html.Div | None = None,
) -> html.Div:
    color = {
        "green": "#166534",
        "amber": "#9a3412",
        "red": "#b91c1c",
    }.get(color_class, "#111827")
    return html.Div(
        [
            html.Div(title, className="card-title"),
            html.Div(basis or "-", className="card-meta"),
            html.Div(duty or "-", className="scenario-duty", style={"color": color}),
            html.Ul([html.Li(action) for action in actions if action], className="scenario-actions"),
            html.Div(
                f"세부 서류/선언 코드 {len(cert_codes or [])}개",
                className="card-meta",
                style={"marginTop": "9px"},
            ),
            html.Details(
                [
                    html.Summary("서류 확인"),
                    document_window,
                ],
                className="scenario-detail",
            ) if document_window else None,
        ],
        className=f"scenario-card {color_class}",
    )


def _scenario_parts(cx: dict[str, Any]) -> dict[str, Any]:
    controls = cx.get("controls") or []
    third_country = cx.get("third_country")
    fta_pref = cx.get("fta_pref")
    mandatory_categories = {"mandatory_certificate", "national_document", "import_license"}
    control_cert_codes = _scenario_cert_codes(controls, mandatory_categories)
    all_control_codes = _scenario_cert_codes(controls)
    fta_codes = _scenario_cert_codes([fta_pref] if fta_pref else [], {"preferential_origin"}) or _scenario_cert_codes([fta_pref] if fta_pref else [])
    has_control_requirements = bool(control_cert_codes or all_control_codes or controls)
    return {
        "controls": controls,
        "third_country": third_country,
        "fta_pref": fta_pref,
        "control_cert_codes": control_cert_codes,
        "all_control_codes": all_control_codes,
        "fta_codes": fta_codes,
        "has_control_requirements": has_control_requirements,
    }


def _scenario_comparison_cards(cx: dict[str, Any]) -> list[html.Div]:
    parts = _scenario_parts(cx)
    third_country = parts["third_country"]
    fta_pref = parts["fta_pref"]
    all_control_codes = parts["all_control_codes"]
    fta_codes = parts["fta_codes"]
    has_control_requirements = parts["has_control_requirements"]

    scenarios: list[html.Div] = []
    if fta_pref:
        scenarios.append(
            _scenario_card(
                "FTA 우대세율 적용",
                duty_rate(fta_pref),
                fta_pref.get("measure_type") or "Tariff preference",
                [
                    "원산지가 한국이고 한-EU FTA 원산지 기준을 충족해야 합니다.",
                    "상업서류에 원산지 신고문안 또는 관련 원산지 증빙을 준비합니다.",
                    "Control 서류가 있으면 먼저 충족해야 합니다.",
                ],
                "green",
                fta_codes + all_control_codes,
                _scenario_document_window(cx, "fta", fta_codes + all_control_codes, "FTA 우대 시 제출 창"),
            )
        )
    else:
        scenarios.append(
            _scenario_card(
                "FTA 우대세율 미확인",
                "해당 없음",
                "현재 한국 기준 우대관세 measure를 찾지 못했습니다.",
                [
                    "우대세율이 필요하면 원산지/협정 기준을 별도로 확인합니다.",
                    "기본관세 시나리오와 서류 확인 창을 먼저 검토합니다.",
                ],
                "amber",
                all_control_codes,
                _scenario_document_window(cx, "basic", all_control_codes, "우대 미확인 시 제출 창"),
            )
        )

    scenarios.append(
        _scenario_card(
            "기본관세 적용",
            duty_rate(third_country),
            (third_country or {}).get("measure_type") or "Third country duty",
            [
                "FTA 우대세율을 쓰지 않을 때의 기본 세율 시나리오입니다.",
                "상업송장, 포장명세서, 운송서류 등 baseline 제출서류는 계속 필요합니다.",
                "Control 서류가 있으면 기본관세 납부와 별개로 준비해야 합니다.",
            ],
            "amber",
            all_control_codes,
            _scenario_document_window(cx, "basic", all_control_codes, "기본관세 시 제출 창"),
        )
    )

    scenarios.append(
        _scenario_card(
            "Control 서류 미준비",
            "통관 보류 가능",
            "필수 certificate/declaration 또는 비대상 근거가 준비되지 않은 경우",
            [
                "세율보다 control 서류 충족 여부가 먼저입니다.",
                "필수 코드가 있으면 관련 증명서 또는 비대상 선언 근거를 준비합니다.",
                "해당 없음으로 판단하려면 제품 성분/용도/원산지 근거가 필요합니다.",
            ],
            "red" if has_control_requirements else "amber",
            all_control_codes,
            _scenario_document_window(cx, "control", all_control_codes, "Control 확인용 제출 창"),
        )
    )

    return scenarios


def render_scenario_decision(pkg: dict[str, Any], cx: dict[str, Any], selected_values: list[str] | None) -> html.Div:
    parts = _scenario_parts(cx)
    third_country = parts["third_country"]
    fta_pref = parts["fta_pref"]
    all_control_codes = parts["all_control_codes"]
    fta_codes = parts["fta_codes"]
    has_control_requirements = parts["has_control_requirements"]
    selected = set(selected_values or [])
    origin_is_kr = "origin_kr" in selected
    controls_ready = "controls_ready" in selected or not has_control_requirements
    fta_requested = "fta_requested" in selected

    if not controls_ready:
        primary = _scenario_card(
            "현재 선택 결과: Control 서류 미준비",
            "통관 보류 가능",
            "필수 certificate/declaration 또는 비대상 근거가 준비되지 않은 상태입니다.",
            [
                "세율보다 control 서류 충족 여부가 먼저입니다.",
                "서류 확인을 열어 연결된 TARIC 코드와 준비 문서를 확인하세요.",
            ],
            "red",
            all_control_codes,
            _scenario_document_window(cx, "control", all_control_codes, "Control 서류 준비 창"),
        )
    elif origin_is_kr and fta_requested and fta_pref:
        primary = _scenario_card(
            "현재 선택 결과: FTA 우대세율 적용",
            duty_rate(fta_pref),
            fta_pref.get("measure_type") or "Tariff preference",
            [
                "원산지 기준 충족자료와 원산지 신고문안을 준비합니다.",
                "기본 제출서류에는 원산지/가격/수량/운송정보가 일관되게 들어가야 합니다.",
            ],
            "green",
            fta_codes + all_control_codes,
            _scenario_document_window(cx, "fta", fta_codes + all_control_codes, "FTA 우대 시 제출 창"),
        )
    elif origin_is_kr:
        primary = _scenario_card(
            "현재 선택 결과: 기본관세 적용",
            duty_rate(third_country),
            (third_country or {}).get("measure_type") or "Third country duty",
            [
                "FTA 우대세율을 쓰지 않거나 확인되지 않은 경우의 기본 시나리오입니다.",
                "기본 제출서류와 control 서류/비대상 근거는 별도로 준비합니다.",
            ],
            "amber",
            all_control_codes,
            _scenario_document_window(cx, "basic", all_control_codes, "기본관세 시 제출 창"),
        )
    else:
        primary = _scenario_card(
            "현재 선택 결과: 한국 원산지 아님",
            duty_rate(third_country),
            (third_country or {}).get("measure_type") or "원산지별 재조회 필요",
            [
                "한-EU FTA 한국 원산지 우대세율은 적용하지 않습니다.",
                "실제 원산지 국가 기준으로 TARIC/Access2Markets를 다시 확인해야 합니다.",
            ],
            "amber",
            all_control_codes,
            _scenario_document_window(cx, "basic", all_control_codes, "비한국 원산지 기본 제출 창"),
        )

    return html.Div(
        [
            primary,
            html.Details(
                [
                    html.Summary("가능 시나리오 비교"),
                    html.Div(_scenario_comparison_cards(cx), className="scenario-grid"),
                ],
                style={"marginTop": "12px"},
            ),
        ]
    )


def _default_scenario_values(cx: dict[str, Any]) -> list[str]:
    parts = _scenario_parts(cx)
    values = ["origin_kr"]
    if not parts["has_control_requirements"]:
        values.append("controls_ready")
    if parts["fta_pref"]:
        values.append("fta_requested")
    return values


def render_trade_scenario(pkg: dict[str, Any], cx: dict[str, Any]) -> html.Div:
    parts = _scenario_parts(cx)
    has_control_requirements = parts["has_control_requirements"]
    fta_pref = parts["fta_pref"]
    checklist_values = _default_scenario_values(cx)
    taric_key = clean_code(str(pkg.get("taric10") or "unknown")) or "unknown"

    return html.Div(
        [
            html.Div("통관 조건 체크", className="section-title"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("TARIC CODE", className="metric-label"),
                            html.Div(pkg.get("taric10") or "-", className="metric-value", style={"color": "#1d4ed8", "fontFamily": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"}),
                            html.Div(f"CN8: {pkg.get('cn8') or '-'}", className="card-meta"),
                        ],
                        className="scenario-code",
                    ),
                    html.Div(
                        [
                            html.Div("조건 체크", className="card-title"),
                            dcc.Checklist(
                                id={"type": "scenario-checks", "taric": taric_key},
                                options=[
                                    {"label": "원산지가 한국인가요?", "value": "origin_kr"},
                                    {"label": "Control 서류 또는 비대상 근거가 준비되었나요?", "value": "controls_ready", "disabled": not has_control_requirements},
                                    {"label": "FTA 우대세율을 신청하나요?", "value": "fta_requested", "disabled": not bool(fta_pref)},
                                ],
                                value=checklist_values,
                                className="scenario-checks",
                                inputStyle={"marginRight": "8px"},
                                labelStyle={"display": "block"},
                            ),
                            html.Div(
                                "체크 상태에 따라 아래 시나리오를 비교하고, 실제 제출물은 각 시나리오의 서류 확인에서 봅니다.",
                                className="card-meta",
                            ),
                        ]
                    ),
                ],
                className="scenario-head",
            ),
            html.Div(
                render_scenario_decision(pkg, cx, checklist_values),
                id={"type": "scenario-result", "taric": taric_key},
            ),
        ],
        className="scenario-shell",
    )


@app.callback(
    Output({"type": "scenario-result", "taric": MATCH}, "children"),
    Input({"type": "scenario-checks", "taric": MATCH}, "value"),
    State("package-store", "data"),
)
def update_scenario_decision(selected_values, pkg):
    if not pkg:
        return no_update
    cx = package_context(pkg)
    if cx.get("source") == "unresolved":
        return no_update
    return render_scenario_decision(pkg, cx, selected_values or [])


def render_overview(cx: dict[str, Any], options: list[str], pkg: dict[str, Any]):
    missing = cx["missing"]
    items = [
        ("TARIC 확인 코드", f"{len(cx['controls'])}개 control measure"),
        ("관세 시나리오", f"{len(cx['duties'])}개 duty/preference measure"),
        ("추가 상세서류", f"{len(_additional_detail_documents(cx.get('baseline_documents') or []) or cx['groups'])}개 chapter/domain document"),
    ]
    left = html.Div(
        [
            html.Div("오늘 봐야 할 것", className="section-title"),
            *[
                html.Div([html.Div(title, className="card-title"), html.Div(body, className="card-meta")], className="card")
                for title, body in items
            ],
        ]
    )
    right = html.Div(
        [
            html.Div("남은 판단 facts", className="section-title"),
            html.Div(
                [html.Span(fact, className="chip") for fact in missing[:22]]
                if missing
                else html.Div("현재 상세 row 기준 추가 missing facts가 없습니다.", className="card-meta"),
                className="card",
            ),
        ]
    )
    raw = None
    if "raw" in options:
        raw = html.Details([html.Summary("Raw JSON"), html.Pre(json.dumps(pkg, ensure_ascii=False, indent=2), className="textarea")])
    blackboard = render_blackboard_log(pkg) if "blackboard" in options else None
    return html.Div([render_trade_scenario(pkg, cx), html.Div([left, right], className="two-col"), blackboard, raw])


def render_blackboard_log(pkg: dict[str, Any]) -> html.Div | None:
    pipeline = pkg.get("_pipeline") or {}
    if not pipeline:
        return html.Details(
            [
                html.Summary("Blackboard log"),
                html.Div("TARIC 직접조회 모드입니다. Agent pipeline log가 없습니다.", className="card-meta"),
            ]
        )

    summary = {
        "run_id": pipeline.get("run_id"),
        "run_dir": pipeline.get("run_dir"),
        "agent_results": pipeline.get("agent_results") or [],
        "decision": pipeline.get("decision") or {},
    }
    agent_runs = pipeline.get("agent_runs") or []
    candidate_set = pipeline.get("candidate_code_set") or {}
    document_package = pipeline.get("document_package") or {}
    compact_document_package = {
        key: value
        for key, value in document_package.items()
        if key != "raw_document_package"
    } if isinstance(document_package, dict) else document_package

    payload = {
        "summary": summary,
        "candidate_code_set": candidate_set,
        "document_package": compact_document_package,
        "agent_runs": agent_runs,
    }
    cards = []
    for run in agent_runs:
        cards.append(
            html.Div(
                [
                    html.Div(
                        (
                            f"{display_stage_name(run.get('agent_name'))} · "
                            f"{display_stage_name(run.get('stage'))}"
                        ),
                        className="card-title",
                    ),
                    html.Div(
                        f"outputs: {', '.join(run.get('outputs_written') or []) or '-'}",
                        className="card-meta",
                    ),
                    html.Div(
                        run.get("reasoning_summary") or "reasoning 없음",
                        className="card-meta",
                    ),
                ],
                className="card",
            )
        )
    return html.Details(
        [
            html.Summary("Blackboard log"),
            html.Div(
                [
                    html.Div(f"run_id: {pipeline.get('run_id')}", className="card-meta"),
                    html.Div(f"run_dir: {pipeline.get('run_dir')}", className="card-meta"),
                ],
                className="card",
            ),
            html.Div(cards, className="two-col") if cards else html.Div("AgentRun log 없음", className="card-meta"),
            html.Details(
                [
                    html.Summary("Blackboard JSON"),
                    html.Pre(json.dumps(payload, ensure_ascii=False, indent=2), className="textarea"),
                ]
            ),
        ]
    )


def render_customs(pkg: dict[str, Any], controls: list[dict[str, Any]]):
    if not controls:
        return html.Div("별도 control measure가 없습니다.", className="card-meta")
    rows = []
    for req in controls:
        certs = req.get("certificates") or []
        rows.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(pkg.get("taric10"), className="card-title", style={"fontFamily": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", "color": "#1d4ed8"}),
                            html.Div(f"CN8 {pkg.get('cn8')}", className="card-meta"),
                        ],
                        className="card",
                    ),
                    html.Div(
                        [
                            html.Div(req.get("measure_type"), className="card-title"),
                            html.Div(f"legal: {req.get('legal_base') or 'N/A'}", className="card-meta"),
                            html.Div(f"source: {', '.join((req.get('source_goods_codes') or [])[:3])}", className="card-meta"),
                        ],
                        className="card",
                        style={"borderLeft": "4px solid #b91c1c"},
                    ),
                    html.Div([cert_card(c) for c in certs] or html.Div("별도 certificate/declaration code 없음", className="card-meta")),
                ],
                className="three-col",
            )
        )
    return html.Div([html.Div("세관 확인사항: measure -> certificate/declaration", className="section-title"), *rows])


def render_base_duty(reqs: list[dict[str, Any]]):
    if not reqs:
        return html.Div("현재 조회 결과에 기본 관세 measure가 없습니다.", className="card-meta")
    return html.Div(
        [
            html.Div("기본 관세 적용 및 관련 서류", className="section-title"),
            *[
                html.Div(
                    [
                        html.Div(req.get("measure_type"), className="card-title"),
                        html.Div(duty_rate(req), style={"fontSize": "24px", "fontWeight": 950, "color": "#9a3412", "marginTop": "3px"}),
                        html.Div(f"legal: {req.get('legal_base') or 'N/A'} · origin: {', '.join((req.get('origins') or [])[:4])}", className="card-meta"),
                        html.Div(
                            [cert_card(c) for c in (req.get("certificates") or [])]
                            or html.Div("이 관세 measure에는 별도 certificate/declaration code가 없습니다.", className="card-meta"),
                            style={"marginTop": "10px"},
                        ),
                    ],
                    className="card",
                    style={"background": "#fff7ed", "borderLeft": "4px solid #9a3412"},
                )
                for req in reqs
            ],
        ]
    )


def render_preferential(reqs: list[dict[str, Any]]):
    if not reqs:
        return html.Div("현재 조회 결과에 우대 관세 measure가 없습니다.", className="card-meta")
    return html.Div(
        [
            html.Div("우대 관세와 원산지 증빙", className="section-title"),
            *[
                html.Div(
                    [
                        html.Div(req.get("measure_type"), className="card-title"),
                        html.Div(duty_rate(req), style={"fontSize": "24px", "fontWeight": 950, "color": "#166534", "marginTop": "3px"}),
                        html.Div(f"legal: {req.get('legal_base') or 'N/A'} · origin: {', '.join((req.get('origins') or [])[:4])}", className="card-meta"),
                        html.Div(
                            [cert_card(c) for c in (([c for c in (req.get("certificates") or []) if c.get("category") == "preferential_origin"] or (req.get("certificates") or [])))]
                            or html.Div("이 우대 관세 measure에는 별도 certificate/declaration code가 없습니다.", className="card-meta"),
                            style={"marginTop": "10px"},
                        ),
                    ],
                    className="card",
                    style={"background": "#f0fdf4", "borderLeft": "4px solid #166534"},
                )
                for req in reqs
            ],
        ]
    )


def render_bundles(groups: list[dict[str, Any]]):
    if not groups:
        return html.Div("요구서류 묶음이 없습니다.", className="card-meta")
    cards = []
    for group in groups[:24]:
        doc_items = (group.get("documents") or [])[:6]
        declaration_items = (group.get("declarations") or [])[:6]
        docs = ", ".join(doc_items) or "없음"
        declarations = ", ".join(declaration_items) or "없음"
        needed_names = doc_items or declaration_items or [group.get("group_name") or "필요서류"]
        missing_facts = group.get("missing_facts") or []
        missing = (
            f"해당서류 없음({', '.join(needed_names[:3])})"
            + (f" · 확인 필요 facts: {', '.join(missing_facts[:4])}" if missing_facts else "")
            if missing_facts
            else "없음"
        )
        lookups = ", ".join((group.get("external_dataset_ids") or [])[:6]) or "없음"
        cards.append(
            html.Div(
                [
                    html.Div([html.Span(group.get("group_name") or "문서 묶음", className="card-title"), status_badge(group.get("status") or "pending")]),
                    html.Div([html.B("준비 서류: "), docs], className="card-meta", style={"color": "#334155"}),
                    html.Div([html.B("선언/면제: "), declarations], className="card-meta", style={"color": "#334155"}),
                    html.Div([html.B("missing: "), missing], className="card-meta", style={"color": "#9a3412"}),
                    html.Div([html.B("외부참조: "), lookups], className="card-meta", style={"color": "#7c2d12"}),
                ],
                className="card",
            )
        )
    return html.Div([html.Div("요구서류 묶음", className="section-title"), html.Div(cards, className="two-col")])


def render_additional_documents(
    documents: list[dict[str, Any]],
    pre_checks: list[dict[str, Any]],
    legacy_groups: list[dict[str, Any]],
):
    if not documents and legacy_groups:
        return render_bundles(legacy_groups)
    if not documents:
        return html.Div(
            [
                html.Div("추가 상세서류", className="section-title"),
                html.Div(
                    "이 코드에서 기본 상업서류 외에 별도로 표시할 챕터/도메인 상세서류가 없습니다.",
                    className="card-meta",
                ),
            ]
        )

    pre_summary = html.Details(
        [
            html.Summary(f"사전 확인사항 {len(pre_checks)}개"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(check.get("pre_gate_family") or "사전 확인", className="card-title"),
                            html.Div(check.get("required_action") or "", className="card-meta"),
                            html.Div(
                                "추가 확인: " + (", ".join((check.get("missing_facts") or [])[:6]) or "없음"),
                                className="card-meta",
                                style={"color": "#9a3412"},
                            ),
                        ],
                        className="card",
                    )
                    for check in pre_checks[:8]
                ]
                or html.Div("사전 확인사항 없음", className="card-meta"),
                className="two-col",
            ),
        ],
        style={"margin": "10px 0 14px"},
    )

    cards = []
    for doc in documents:
        fields = doc.get("fields") or []
        certs = ", ".join((doc.get("taric_certificates") or [])[:8]) or "-"
        cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(_doc_name(doc), className="card-title"),
                            status_badge(_doc_status(doc)),
                        ]
                    ),
                    html.Div(_doc_code(doc), className="card-meta", style={"fontFamily": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"}),
                    html.Div(f"연결 코드/선언: {certs}", className="card-meta", style={"color": "#1d4ed8"}),
                    html.Div(
                        "추가 확인: " + (", ".join((doc.get("missing_facts") or [])[:6]) or "없음"),
                        className="card-meta",
                        style={"color": "#9a3412"},
                    ),
                    html.Details(
                        [
                            html.Summary(f"작성항목 {len(fields)}개"),
                            html.Div(
                                _scenario_field_rows(fields) or html.Div("작성항목 정의 없음", className="card-meta"),
                                className="scenario-doc-fields",
                            ),
                        ],
                        className="scenario-detail",
                    ),
                ],
                className="card",
            )
        )

    legacy = None
    if legacy_groups:
        legacy = html.Details(
            [
                html.Summary(f"기존 TARIC document group {len(legacy_groups)}개"),
                render_bundles(legacy_groups),
            ],
            style={"marginTop": "16px"},
        )
    return html.Div(
        [
            html.Div("추가 상세서류", className="section-title"),
            html.Div(
                "공통 baseline 문서는 시나리오의 서류 확인 창에서 보고, 여기서는 챕터/도메인별로 추가되는 증명자료와 작성항목만 봅니다.",
                className="card-meta",
                style={"marginBottom": "10px"},
            ),
            pre_summary,
            html.Div(cards, className="two-col"),
            legacy,
        ]
    )


def render_document_checklist(
    documents: list[dict[str, Any]],
    pre_checks: list[dict[str, Any]],
    checklist: dict[str, Any],
    legacy_groups: list[dict[str, Any]],
):
    counts = checklist.get("counts") or {}
    intro = html.Div(
        [
            html.Div("제출서류", className="section-title"),
            html.Div(
                [
                    metric("전체 서류", counts.get("total", len(documents))),
                    metric("필수", counts.get("required", 0), "#b91c1c"),
                    metric("조건부", counts.get("conditional", 0), "#9a3412"),
                    metric("판단보류", counts.get("pending", 0), "#475569"),
                    metric("사전 연결", counts.get("with_pre_links", 0), "#1d4ed8"),
                    metric("상세 연결", counts.get("with_post_links", 0), "#166534"),
                ],
                className="metric-grid",
            ),
            html.Div(
                "상업송장, 포장명세서, 운송서류 같은 기본 제출서류를 먼저 보여주고, 각 문서에 연결된 사전 확인사항과 TARIC 상세 규제를 붙였습니다.",
                className="card-meta",
                style={"marginTop": "8px"},
            ),
        ]
    )

    pre_cards = []
    for check in pre_checks[:8]:
        pre_cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(check.get("pre_gate_family") or "pre gate", className="card-title"),
                            status_badge(check.get("decision_status") or check.get("required_level") or "conditional"),
                        ]
                    ),
                    html.Div(f"domain: {check.get('domain') or '-'} · type: {check.get('requirement_type') or '-'}", className="card-meta"),
                    html.Div(check.get("required_action") or "", className="card-meta", style={"color": "#334155"}),
                    html.Div(
                        "missing: " + (", ".join((check.get("missing_facts") or [])[:6]) or "없음"),
                        className="card-meta",
                        style={"color": "#9a3412"},
                    ),
                ],
                className="card",
            )
        )
    pre_block = html.Details(
        [
            html.Summary(f"사전 확인사항 {len(pre_checks)}개"),
            html.Div(pre_cards or html.Div("사전 확인사항 없음", className="card-meta"), className="two-col"),
        ],
        style={"margin": "14px 0"},
    )

    doc_cards = []
    for doc in documents:
        fields = doc.get("fields") or []
        pre_count = len(doc.get("pre_checks") or [])
        post_count = len(doc.get("post_requirements") or [])
        certs = ", ".join((doc.get("taric_certificates") or [])[:8]) or "-"
        field_rows = [
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(field.get("label") or field.get("field_key"), style={"fontWeight": 800}),
                            html.Span(" · "),
                            status_badge(field.get("status") or "conditional"),
                        ],
                        className="card-meta",
                    ),
                    html.Div("required_by: " + (", ".join(field.get("required_by") or []) or "baseline"), className="card-meta"),
                    html.Div(
                        "missing: " + (", ".join((field.get("missing_facts") or [])[:5]) or "없음"),
                        className="card-meta",
                        style={"color": "#9a3412"},
                    ),
                ],
                style={"borderTop": "1px solid #e5e7eb", "paddingTop": "7px", "marginTop": "7px"},
            )
            for field in fields[:8]
        ]
        doc_cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(doc.get("document_name_ko") or doc.get("document_name") or doc.get("document_code"), className="card-title"),
                            status_badge(doc.get("decision_status") or doc.get("required_level") or "conditional"),
                        ]
                    ),
                    html.Div(doc.get("document_code") or "", className="card-meta", style={"fontFamily": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"}),
                    html.Div(f"작성/제공: {doc.get('prepared_by') or '-'} → {doc.get('submitted_to') or '-'}", className="card-meta"),
                    html.Div(f"연결된 확인사항: 사전 {pre_count} · 상세 {post_count} · TARIC 코드 {certs}", className="card-meta", style={"color": "#1d4ed8"}),
                    html.Div("추가 확인 필요: " + (", ".join((doc.get("missing_facts") or [])[:6]) or "없음"), className="card-meta", style={"color": "#9a3412"}),
                    html.Details(
                        [
                            html.Summary(f"작성 항목 {len(fields)}개 보기"),
                            html.Div(field_rows or html.Div("필드 정의 없음", className="card-meta")),
                        ],
                        style={"marginTop": "9px"},
                    ),
                ],
                className="card",
            )
        )

    legacy = None
    if legacy_groups:
        legacy = html.Details(
            [
                html.Summary(f"기존 TARIC document group {len(legacy_groups)}개"),
                render_bundles(legacy_groups),
            ],
            style={"marginTop": "16px"},
        )
    return html.Div([intro, pre_block, html.Div(doc_cards, className="two-col"), legacy])


def render_product_rules_from_view(
    pre: list[dict[str, Any]],
    post: list[dict[str, Any]],
    related_declarations: dict[str, list[str]],
):
    pre_col = html.Div(
        [
            html.Div([html.Div("사전 규제 후보", className="card-title"), html.Div(f"CN chapter 기준으로 먼저 확인할 규제 후보 · {len(pre)}개", className="card-meta")], className="card", style={"borderLeft": "4px solid #475569"}),
            *[
                detail_card(d, "pre 후보", related_declarations.get(d.get("domain_route") or d.get("domain") or "", []))
                for d in pre[:16]
            ],
        ]
    )
    post_col = html.Div(
        [
            html.Div([html.Div("TARIC 상세 규제", className="card-title"), html.Div(f"선택된 TARIC 코드에서 실제 준비/누락/보류 판단 항목 · {len(post)}개", className="card-meta")], className="card", style={"borderLeft": "4px solid #166534", "background": "#f0fdf4"}),
            *[
                detail_card(d, "post 상세", related_declarations.get(d.get("domain_route") or d.get("domain") or "", []))
                for d in post[:22]
            ],
        ]
    )
    return html.Div([html.Div("상세 규제/선언 체크리스트", className="section-title"), html.Div([pre_col, post_col], className="two-col")])


def _load_pipeline_payload(run_id: str | None) -> dict[str, Any]:
    if not run_id:
        return {}
    run_dir = RUNS_ROOT / run_id
    blackboard_path = run_dir / "blackboard.json"
    agent_runs_path = run_dir / "agent_runs.jsonl"
    payload: dict[str, Any] = {"run_id": run_id, "run_dir": str(run_dir)}
    if blackboard_path.exists():
        payload["blackboard"] = json.loads(blackboard_path.read_text(encoding="utf-8"))
    if agent_runs_path.exists():
        agent_runs = []
        for line in agent_runs_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                agent_runs.append(json.loads(line))
            except json.JSONDecodeError:
                agent_runs.append({"raw": line})
        payload["agent_runs"] = agent_runs
    return payload


def load_package_for_detail(run_id: str | None, taric10: str, *, include_celex_excerpt: bool = False) -> dict[str, Any]:
    """Read the precomputed package from blackboard; resolve directly only as fallback."""
    code = clean_code(taric10)
    pipeline = _load_pipeline_payload(run_id)
    blackboard = pipeline.get("blackboard") or {}
    document_packages = blackboard.get("document_packages") or []
    for dp in document_packages:
        if clean_code(dp.get("taric10") or "") != code:
            continue
        raw = dp.get("raw_document_package")
        if isinstance(raw, dict):
            package = dict(raw)
            if isinstance(dp.get("document_view"), dict):
                package["_document_view"] = dp.get("document_view")
            package["_pipeline"] = {
                "run_id": pipeline.get("run_id"),
                "run_dir": pipeline.get("run_dir"),
                "agent_results": [],
                "agent_runs": pipeline.get("agent_runs") or [],
                "candidate_code_set": (blackboard.get("candidate_code_sets") or [None])[-1],
                "document_package": {k: v for k, v in dp.items() if k != "raw_document_package"},
                "decision": (blackboard.get("orchestrator_decisions") or [None])[-1],
            }
            return package

    package = get_document_package(code, include_celex_excerpt=include_celex_excerpt)
    package_data = _dc_to_dict(package)
    package_data["_pipeline"] = pipeline
    return package_data


def render_detail_page(run_id: str | None, taric10: str, panel: str = "overview", options: list[str] | None = None) -> html.Div:
    options = options or ["blackboard"]
    try:
        package = load_package_for_detail(run_id, taric10, include_celex_excerpt="celex" in options)
    except Exception as exc:  # noqa: BLE001
        return html.Div(
            [
                dcc.Link("← 분류 화면", href="/classification", className="subtle"),
                html.Div(f"서류 패키지 조회 오류: {exc}", className="error"),
            ],
            className="main",
        )

    return html.Div(
        [
            dcc.Store(id="package-store", data=package),
            html.Div(
                [
                    dcc.Link("← 분류 화면", href="/classification", className="subtle"),
                    html.Span(" · ", className="subtle"),
                    dcc.Link(
                        "Admin log",
                        href=f"/admin/{run_id}" if run_id else "/admin",
                        className="subtle",
                    ),
                ],
                style={"marginBottom": "12px"},
            ),
            html.Div(
                [
                    html.H2("EU 수출 서류 패키지", className="title"),
                    html.Div(
                        f"run_id {run_id or '-'} · TARIC10 {clean_code(taric10) or '-'}",
                        className="caption",
                    ),
                ],
                style={"marginBottom": "14px"},
            ),
            html.Div(render_result(package, panel or "overview", options), id="document-result-root"),
        ],
        className="main",
    )


if __name__ == "__main__":
    dash_host = os.getenv("ASAP_DASH_HOST", "127.0.0.1")
    dash_port = _select_port(dash_host, _preferred_port())
    app.run(host=dash_host, port=dash_port, debug=False)
