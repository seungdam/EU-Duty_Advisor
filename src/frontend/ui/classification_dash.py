from __future__ import annotations

import json
from typing import Any

from dash import dcc, html


CARD = {
    "background": "white",
    "border": "1px solid #e5e7eb",
    "borderRadius": "10px",
    "padding": "16px 18px",
    "boxShadow": "0 1px 3px rgba(15,23,42,0.05)",
}
LABEL = {
    "fontSize": "11px",
    "color": "#64748b",
    "fontWeight": 850,
    "textTransform": "uppercase",
    "marginBottom": "8px",
}
PLACEHOLDER = {
    "color": "#64748b",
    "fontSize": "13px",
    "fontStyle": "italic",
    "padding": "20px",
    "textAlign": "center",
    "background": "#f8fafc",
    "borderRadius": "8px",
    "border": "1px dashed #cbd5e1",
}
INPUT = {
    "width": "100%",
    "height": "42px",
    "lineHeight": "20px",
    "padding": "10px 12px",
    "fontSize": "14px",
    "fontFamily": "inherit",
    "boxSizing": "border-box",
    "border": "1px solid #cbd5e1",
    "borderRadius": "8px",
    "marginBottom": "8px",
}
TEXTAREA = {
    **INPUT,
    "height": "auto",
    "minHeight": "94px",
    "lineHeight": "1.45",
    "resize": "vertical",
}
PILL = {
    "display": "inline-block",
    "padding": "3px 8px",
    "borderRadius": "999px",
    "fontSize": "11px",
    "fontWeight": 800,
    "background": "#eff6ff",
    "color": "#2563eb",
    "marginRight": "6px",
}
MONO = {
    "fontFamily": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    "fontSize": "12px",
}
CLASSIFIER_INPUT_EXCLUDED_FACT_MARKERS = {
    "알레르기",
    "혼입",
    "같은 제조시설",
    "같은 제조 시설",
    "판매처",
    "판매원",
    "판매자",
    "유통전문판매원",
    "판매업소",
    "제조원",
    "제조사",
    "제조업소",
    "제조업체",
    "allergen",
    "allergy",
    "may contain",
    "same facility",
    "seller",
    "vendor",
    "manufacturer",
}
STAGE_DISPLAY_NAMES = {
    "Input": "Input",
    "Pipeline": "Pipeline",
    "Input_Intake": "Input Processing",
    "Evidence_Intake_Agent": "Product Evidence Builder",
    "Classification_Agent": "Classification",
    "Document_Agent": "Document Recommendation",
    "Orchestrator_Agent": "Result Orchestration",
    "Product_Intake": "Product Evidence",
    "Classification": "Classification",
    "Document_Recommendation": "Document Recommendation",
    "Orchestration": "Result Orchestration",
}


def display_stage_name(stage: Any) -> str:
    stageText = str(stage or "").strip()
    return STAGE_DISPLAY_NAMES.get(stageText, stageText or "-")


def display_stage_message(message: Any) -> str:
    messageText = str(message or "")
    for rawStageName, displayName in STAGE_DISPLAY_NAMES.items():
        messageText = messageText.replace(rawStageName, displayName)
    return messageText


def _small(label: str, value: Any) -> html.Div:
    return html.Div(
        [
            html.Div(label, style={"fontSize": "11px", "color": "#64748b", "marginBottom": "4px"}),
            html.Div(str(value or "-"), style={"fontWeight": 850, "fontSize": "14px", "overflowWrap": "anywhere"}),
        ],
        style={
            "border": "1px solid #e5e7eb",
            "borderRadius": "8px",
            "padding": "10px 12px",
            "background": "#fbfcfe",
            "minWidth": "120px",
        },
    )


def json_pre(data: Any, *, max_height: int = 420) -> html.Pre:
    return html.Pre(
        json.dumps(data, ensure_ascii=False, indent=2),
        style={
            **MONO,
            "whiteSpace": "pre-wrap",
            "overflow": "auto",
            "maxHeight": f"{max_height}px",
            "background": "#0f172a",
            "color": "#e5e7eb",
            "borderRadius": "8px",
            "padding": "14px",
            "border": "1px solid #111827",
        },
    )


def detail_block(title: str, data: Any, *, max_height: int = 320, open_: bool = False) -> html.Details:
    return html.Details(
        [
            html.Summary(title, style={"cursor": "pointer", "fontSize": "12px", "fontWeight": 850, "color": "#334155"}),
            json_pre(data, max_height=max_height),
        ],
        open=open_,
        style={"marginTop": "8px"},
    )


def _short_text(value: Any, *, max_length: int = 160) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text or "-"
    return text[: max_length - 1].rstrip() + "..."


def _fact_display_value(fact: dict[str, Any]) -> str:
    return str(
        fact.get("normalized_value")
        or fact.get("raw_value")
        or ""
    ).strip()


def _fact_source_text(
    fact: dict[str, Any],
    source_labels: dict[str, Any] | None = None,
) -> str:
    refs = fact.get("source_refs") or []
    if isinstance(refs, list):
        labels = source_labels or {}
        return ", ".join(
            str(labels.get(str(ref), ref))
            for ref in refs[:3]
            if str(ref).strip()
        )
    return str(refs or "")


def _filter_static_classifier_input_facts(
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fieldName = str(fact.get("field_name") or "")
        factText = "{0} {1}".format(fieldName, _fact_display_value(fact)).lower()
        if any(marker in factText for marker in CLASSIFIER_INPUT_EXCLUDED_FACT_MARKERS):
            continue
        filtered.append(fact)
    return filtered


def _reconstruction_fact_table(
    title: str,
    facts: list[dict[str, Any]],
    *,
    source_labels: dict[str, Any] | None = None,
    max_rows: int = 12,
) -> html.Div | None:
    if not facts:
        return None

    gridStyle = {
        "display": "grid",
        "gridTemplateColumns": "minmax(110px, 0.8fr) minmax(180px, 1.6fr) minmax(90px, 0.7fr) minmax(90px, 0.7fr) minmax(130px, 1fr)",
        "gap": "0",
        "minWidth": "720px",
    }
    headerCell = {
        "padding": "8px 10px",
        "fontSize": "11px",
        "fontWeight": 900,
        "color": "#475569",
        "background": "#f8fafc",
        "borderBottom": "1px solid #e5e7eb",
    }
    cell = {
        "padding": "9px 10px",
        "fontSize": "12px",
        "color": "#334155",
        "borderBottom": "1px solid #edf2f7",
        "overflowWrap": "anywhere",
    }
    rows: list[Any] = [
        html.Div("Field", style=headerCell),
        html.Div("Value", style=headerCell),
        html.Div("Status", style=headerCell),
        html.Div("Correction", style=headerCell),
        html.Div("Source", style=headerCell),
    ]
    for fact in facts[:max_rows]:
        fieldName = fact.get("field_name") or "-"
        rows.extend(
            [
                html.Div(_short_text(fieldName, max_length=72), style={**cell, "fontWeight": 850}),
                html.Div(_short_text(_fact_display_value(fact), max_length=220), style=cell),
                html.Div(_short_text(fact.get("validation_status") or "-", max_length=48), style=cell),
                html.Div(_short_text(fact.get("correction_type") or "-", max_length=48), style=cell),
                html.Div(
                    _short_text(
                        _fact_source_text(fact, source_labels),
                        max_length=120,
                    ),
                    style=cell,
                ),
            ]
        )

    more = None
    if len(facts) > max_rows:
        more = html.Div(
            f"+ {len(facts) - max_rows} more reconstructed facts",
            style={"fontSize": "12px", "color": "#64748b", "marginTop": "8px"},
        )

    return html.Div(
        [
            html.Div(title, style={"fontSize": "12px", "fontWeight": 900, "color": "#0f172a", "marginBottom": "8px"}),
            html.Div(html.Div(rows, style=gridStyle), style={"overflowX": "auto", "border": "1px solid #e5e7eb", "borderRadius": "8px"}),
            more,
        ],
        style={"marginTop": "10px"},
    )


def _classification_fact_text_table(
    fact_texts: list[Any],
    *,
    title: str = "Classification input text lines",
    max_rows: int = 12,
) -> html.Div | None:
    cleaned = [str(text).strip() for text in fact_texts if str(text).strip()]
    if not cleaned:
        return None

    rows = []
    for index, text in enumerate(cleaned[:max_rows], start=1):
        rows.append(
            html.Div(
                [
                    html.Div(str(index), style={**MONO, "color": "#64748b", "fontWeight": 850}),
                    html.Div(_short_text(text, max_length=260), style={"overflowWrap": "anywhere"}),
                ],
                style={
                    "display": "grid",
                    "gridTemplateColumns": "42px minmax(0, 1fr)",
                    "gap": "10px",
                    "padding": "8px 10px",
                    "borderBottom": "1px solid #edf2f7",
                    "fontSize": "12px",
                    "color": "#334155",
                },
            )
        )
    if len(cleaned) > max_rows:
        rows.append(
            html.Div(
                f"+ {len(cleaned) - max_rows} more classification fact lines",
                style={"fontSize": "12px", "color": "#64748b", "padding": "8px 10px"},
            )
        )

    return html.Div(
        [
            html.Div(title, style={"fontSize": "12px", "fontWeight": 900, "color": "#0f172a", "marginBottom": "8px"}) if title else None,
            html.Div(rows, style={"border": "1px solid #e5e7eb", "borderRadius": "8px", "overflow": "hidden"}),
        ],
        style={"marginTop": "10px"},
    )


def _product_page_basic_card(raw: dict[str, Any]) -> html.Div | None:
    productName = raw.get("product_name") or ""
    description = raw.get("description") or ""
    sourceUrls = raw.get("source_urls") or []
    if isinstance(sourceUrls, str):
        sourceUrls = [sourceUrls] if sourceUrls.strip() else []
    url = sourceUrls[0] if isinstance(sourceUrls, list) and sourceUrls else raw.get("url") or ""
    if not any(str(value).strip() for value in [productName, description, url]):
        return None

    rowStyle = {
        "display": "grid",
        "gridTemplateColumns": "120px minmax(0, 1fr)",
        "gap": "10px",
        "padding": "8px 0",
        "borderBottom": "1px solid #edf2f7",
        "fontSize": "12px",
    }
    labelStyle = {"fontWeight": 900, "color": "#475569"}
    valueStyle = {"color": "#334155", "overflowWrap": "anywhere"}
    rows = [
        ("상품명", productName),
        ("설명", _short_text(description, max_length=260)),
        ("URL", url),
    ]
    return html.Div(
        [
            html.Div("Product Facts", style={"fontSize": "13px", "fontWeight": 950, "color": "#0f172a"}),
            html.Div(
                "웹페이지 상단 영역에서 수집한 상품 기본 정보",
                style={"fontSize": "12px", "color": "#64748b", "marginTop": "2px", "marginBottom": "8px"},
            ),
            *[
                html.Div(
                    [
                        html.Div(label, style=labelStyle),
                        html.Div(str(value or "-"), style=valueStyle),
                    ],
                    style=rowStyle,
                )
                for label, value in rows
            ],
        ],
        style={
            "marginTop": "10px",
            "padding": "12px",
            "border": "1px solid #e5e7eb",
            "borderRadius": "8px",
            "background": "#ffffff",
        },
    )


def _source_evidence_table(
    title: str,
    records: list[Any],
    *,
    max_rows: int = 12,
) -> html.Div | None:
    cleaned = [record for record in records if isinstance(record, dict)]
    if not cleaned:
        return None

    headerCell = {
        "padding": "8px 10px",
        "fontSize": "11px",
        "fontWeight": 900,
        "color": "#475569",
        "background": "#f8fafc",
        "borderBottom": "1px solid #e5e7eb",
    }
    cell = {
        "padding": "9px 10px",
        "fontSize": "12px",
        "color": "#334155",
        "borderBottom": "1px solid #edf2f7",
        "overflowWrap": "anywhere",
    }
    rows: list[Any] = [
        html.Div("Source", style=headerCell),
        html.Div("Raw detail text before reconstruction", style=headerCell),
    ]
    for record in cleaned[:max_rows]:
        sourceLabel = record.get("source_label") or record.get("source_type") or "-"
        sourceType = record.get("source_type") or ""
        rows.extend(
            [
                html.Div(
                    [
                        html.Div(_short_text(sourceLabel, max_length=80), style={"fontWeight": 850}),
                        html.Div(_short_text(sourceType, max_length=40), style={**MONO, "color": "#64748b", "marginTop": "3px"}),
                    ],
                    style=cell,
                ),
                html.Div(_short_text(record.get("text"), max_length=360), style=cell),
            ]
        )

    more = None
    if len(cleaned) > max_rows:
        more = html.Div(
            f"+ {len(cleaned) - max_rows} more source evidence rows",
            style={"fontSize": "12px", "color": "#64748b", "marginTop": "8px"},
        )

    return html.Div(
        [
            html.Div(title, style={"fontSize": "12px", "fontWeight": 900, "color": "#0f172a", "marginBottom": "8px"}),
            html.Div(
                html.Div(
                    rows,
                    style={
                        "display": "grid",
                        "gridTemplateColumns": "minmax(130px, 0.65fr) minmax(260px, 1.35fr)",
                        "gap": "0",
                        "minWidth": "520px",
                    },
                ),
                style={"overflowX": "auto", "border": "1px solid #e5e7eb", "borderRadius": "8px"},
            ),
            more,
        ],
        style={"marginTop": "10px"},
    )


def _reconstructed_tables_widget(
    title: str,
    tables: list[Any],
    *,
    source_labels: dict[str, Any] | None = None,
    max_rows_per_table: int = 18,
) -> html.Div | None:
    cleanedTables = [table for table in tables if isinstance(table, dict)]
    if not cleanedTables:
        return None

    headerCell = {
        "padding": "8px 10px",
        "fontSize": "11px",
        "fontWeight": 900,
        "color": "#475569",
        "background": "#f8fafc",
        "borderBottom": "1px solid #e5e7eb",
    }
    cell = {
        "padding": "9px 10px",
        "fontSize": "12px",
        "color": "#334155",
        "borderBottom": "1px solid #edf2f7",
        "overflowWrap": "anywhere",
    }
    labels = source_labels or {}
    tableBlocks: list[Any] = []
    for tableIndex, table in enumerate(cleanedTables, start=1):
        rows = table.get("rows") or []
        if not isinstance(rows, list) or not rows:
            continue
        tableName = table.get("table_name") or f"Table {tableIndex}"
        gridRows: list[Any] = [
            html.Div("Field", style=headerCell),
            html.Div("Value", style=headerCell),
            html.Div("Unit", style=headerCell),
            html.Div("Daily %", style=headerCell),
            html.Div("Source", style=headerCell),
        ]
        for row in [item for item in rows if isinstance(item, dict)][:max_rows_per_table]:
            sourceRefs = row.get("source_refs") or []
            if isinstance(sourceRefs, list):
                sourceText = ", ".join(
                    str(labels.get(str(ref), ref))
                    for ref in sourceRefs[:3]
                    if str(ref).strip()
                )
            else:
                sourceText = str(sourceRefs or "")
            gridRows.extend(
                [
                    html.Div(_short_text(row.get("field_name"), max_length=90), style={**cell, "fontWeight": 850}),
                    html.Div(_short_text(row.get("normalized_value") or row.get("raw_value"), max_length=220), style=cell),
                    html.Div(_short_text(row.get("unit"), max_length=32), style=cell),
                    html.Div(_short_text(row.get("daily_value_percent"), max_length=32), style=cell),
                    html.Div(_short_text(sourceText, max_length=110), style=cell),
                ]
            )
        more = None
        if len(rows) > max_rows_per_table:
            more = html.Div(
                f"+ {len(rows) - max_rows_per_table} more rows",
                style={"fontSize": "12px", "color": "#64748b", "marginTop": "8px"},
            )
        tableBlocks.append(
            html.Div(
                [
                    html.Div(_short_text(tableName, max_length=120), style={"fontSize": "12px", "fontWeight": 900, "color": "#0f172a", "marginBottom": "8px"}),
                    html.Div(
                        html.Div(
                            gridRows,
                            style={
                                "display": "grid",
                                "gridTemplateColumns": "minmax(110px, 0.8fr) minmax(180px, 1.4fr) minmax(58px, 0.45fr) minmax(70px, 0.5fr) minmax(120px, 0.9fr)",
                                "gap": "0",
                                "minWidth": "680px",
                            },
                        ),
                        style={"overflowX": "auto", "border": "1px solid #e5e7eb", "borderRadius": "8px"},
                    ),
                    more,
                ],
                style={"marginTop": "10px"},
            )
        )

    if not tableBlocks:
        return None
    return html.Div(
        [
            html.Div(title, style={"fontSize": "12px", "fontWeight": 900, "color": "#0f172a", "marginBottom": "8px"}),
            *tableBlocks,
        ],
        style={"marginTop": "10px"},
    )


def input_processing_detail_card(
    input_processing_view: dict[str, Any],
) -> html.Div | None:
    if not input_processing_view:
        return None

    status = input_processing_view.get("reconstruction_status") or {}
    if not isinstance(status, dict):
        status = {}
    productFacts = input_processing_view.get("classification_input_facts") or []
    unresolvedFacts = input_processing_view.get("unresolved_input_facts") or []
    conflicts = input_processing_view.get("input_fact_conflicts") or []
    factTexts = input_processing_view.get("classification_input_text_lines") or []
    if not isinstance(productFacts, list):
        productFacts = []
    if not isinstance(unresolvedFacts, list):
        unresolvedFacts = []
    if not isinstance(conflicts, list):
        conflicts = [str(conflicts)] if str(conflicts).strip() else []
    if not isinstance(factTexts, list):
        factTexts = []
    reconstructedTables = input_processing_view.get("reconstructed_detail_tables") or []
    if not isinstance(reconstructedTables, list):
        reconstructedTables = []
    sourceLabels = input_processing_view.get("evidence_source_labels") or {}
    if not isinstance(sourceLabels, dict):
        sourceLabels = {}
    sourceEvidencePreview = input_processing_view.get("detail_evidence_rows") or []
    if not isinstance(sourceEvidencePreview, list):
        sourceEvidencePreview = []
    reconstructionMode = status.get("mode") or "unknown"
    staticClassifierFacts = _filter_static_classifier_input_facts(productFacts)
    reconstructionError = status.get("error")
    fallbackReason = status.get("fallback_reason")
    beforePanel = _source_evidence_table(
        "Before reconstruction: product detail OCR evidence",
        sourceEvidencePreview,
    )
    afterPanel = (
        _reconstructed_tables_widget(
            "After reconstruction: reconstructed detail tables",
            reconstructedTables,
            source_labels=sourceLabels,
        )
        or _reconstruction_fact_table(
            "After reconstruction: reconstructed detail facts",
            productFacts,
            source_labels=sourceLabels,
        )
    )
    primaryFactPanels = [
        panel
        for panel in [
            beforePanel,
            afterPanel,
        ]
        if panel is not None
    ]
    primaryFactLayout = (
        html.Div(
            primaryFactPanels,
            style={
                "display": "grid",
                "gridTemplateColumns": "repeat(auto-fit, minmax(min(100%, 460px), 1fr))",
                "gap": "12px",
                "alignItems": "start",
                "marginTop": "10px",
            },
        )
        if len(primaryFactPanels) > 1
        else primaryFactPanels[0]
        if primaryFactPanels
        else None
    )

    return html.Div(
        [
            html.Div(
                [
                    html.Div("Product Detail Reconstruction", style={"fontSize": "13px", "fontWeight": 950, "color": "#0f172a"}),
                    html.Div(
                        "웹페이지 하단 상세 이미지/PP table/OCR에서 복원한 정보",
                        style={"fontSize": "12px", "color": "#64748b", "marginTop": "2px"},
                    ),
                ],
                style={"marginBottom": "10px"},
            ),
            html.Div(
                [
                    _small("LLM", "on" if status.get("used_llm_reconstruction") else "off"),
                    _small("mode", reconstructionMode),
                    _small("detail tables", status.get("detail_table_count") or len(reconstructedTables)),
                    _small("classification facts", status.get("classification_fact_count") or len(productFacts)),
                    _small("classification text lines", status.get("classification_text_line_count") or len(factTexts)),
                ],
                style={"display": "flex", "gap": "8px", "flexWrap": "wrap"},
            ),
            html.Div(
                [
                    html.Div("Input reconstruction issue", style={"fontSize": "12px", "fontWeight": 900, "color": "#92400e", "marginBottom": "4px"}),
                    html.Div(
                        reconstructionError or fallbackReason or "fallback reconstruction is being used",
                        style={"fontSize": "12px", "color": "#78350f"},
                    ),
                ],
                style={"marginTop": "10px", "padding": "10px", "border": "1px solid #fcd34d", "borderRadius": "8px", "background": "#fffbeb"},
            ) if reconstructionError or (
                fallbackReason
                and not status.get("used_llm_reconstruction")
            ) else None,
            primaryFactLayout,
            _reconstruction_fact_table(
                "Static classifier input facts",
                staticClassifierFacts,
                source_labels=sourceLabels,
                max_rows=16,
            ) if staticClassifierFacts else None,
            html.Details(
                [
                    html.Summary(
                        "Classification input text lines",
                        style={"cursor": "pointer", "fontSize": "12px", "fontWeight": 850, "color": "#334155"},
                    ),
                    _classification_fact_text_table(factTexts, title=""),
                ],
                open=False,
                style={"marginTop": "10px"},
            ) if factTexts else None,
            _reconstruction_fact_table(
                "Unresolved facts",
                unresolvedFacts,
                source_labels=sourceLabels,
                max_rows=6,
            ),
            html.Div(
                [
                    html.Div("Conflicts", style={"fontSize": "12px", "fontWeight": 900, "color": "#991b1b", "marginBottom": "6px"}),
                    html.Ul(
                        [html.Li(_short_text(conflict, max_length=220)) for conflict in conflicts[:6]],
                        style={"margin": "0 0 0 18px", "padding": 0, "fontSize": "12px", "color": "#7f1d1d"},
                    ),
                ],
                style={"marginTop": "10px", "padding": "10px", "border": "1px solid #fecaca", "borderRadius": "8px", "background": "#fef2f2"},
            ) if conflicts else None,
        ],
        style={
            "marginTop": "10px",
            "padding": "12px",
            "border": "1px solid #dbeafe",
            "borderRadius": "8px",
            "background": "#fbfdff",
        },
    )


def input_processing_view_card(
    input_processing_view: dict[str, Any] | None,
) -> html.Div | None:
    if not isinstance(input_processing_view, dict) or not input_processing_view:
        return None

    basicInfo = input_processing_view.get("page_product_facts") or {}
    if not isinstance(basicInfo, dict):
        basicInfo = {}
    basicPanel = _product_page_basic_card(basicInfo)
    reconstructionPanel = input_processing_detail_card(input_processing_view)
    panels = [panel for panel in [basicPanel, reconstructionPanel] if panel is not None]
    if not panels:
        return None
    return html.Div(
        panels,
        style={
            "marginBottom": "20px",
        },
    )


def render_input_form(facts: dict | None = None) -> html.Div:
    facts = facts or {}
    return html.Div(
        [
            html.Div("입력", style=LABEL),
            html.Div(
                [
                    dcc.Input(
                        id="ipt-product-name",
                        type="text",
                        placeholder="제품명",
                        value=facts.get("product_name") or "",
                        style=INPUT,
                    ),
                    dcc.Textarea(
                        id="ipt-description",
                        placeholder="제품 설명 / 원재료 / OCR text",
                        value=facts.get("description") or "",
                        style=TEXTAREA,
                    ),
                    dcc.Input(
                        id="ipt-kurly-url",
                        type="text",
                        placeholder="URL",
                        value=facts.get("url") or "",
                        style={**INPUT, "marginBottom": "12px"},
                    ),
                    html.Div(
                        [
                            html.Button(
                                "Run pipeline",
                                id="btn-run",
                                n_clicks=0,
                                style={
                                    "padding": "10px 24px",
                                    "fontSize": "14px",
                                    "background": "#2563eb",
                                    "color": "white",
                                    "border": "none",
                                    "borderRadius": "8px",
                                    "cursor": "pointer",
                                    "fontWeight": 850,
                                },
                            ),
                        ],
                        style={"display": "flex", "alignItems": "center"},
                    ),
                ],
                style=CARD,
            ),
        ],
        style={"marginBottom": "20px"},
    )


def _event_summary(event: dict[str, Any]) -> html.Div | None:
    partial = event.get("partial_result") or {}
    stage = event.get("stage") or ""
    status = event.get("status") or ""

    if stage == "Input_Intake":
        raw = event.get("collected_input_summary") or {}
        if not raw:
            return None
        url_intake = raw.get("url_intake") or {}
        ocr_summary = url_intake.get("ocr") or {}
        return html.Div(
            [
                _product_page_basic_card(raw),
                html.Div(
                    "ocr_text chunks: {0} / composition: {1}".format(
                        raw.get("ocr_text_count")
                        or len(raw.get("ocr_text") or []),
                        raw.get("composition_count")
                        or len(raw.get("composition") or []),
                    )
                ),
                html.Div(
                    (
                        "URL collection: ocr_images={0}, combined_ocr_chars={1}"
                    ).format(
                        ocr_summary.get("image_result_count")
                        or url_intake.get("ocr_image_count", "-"),
                        ocr_summary.get("combined_text_length")
                        or url_intake.get("combined_ocr_text_length", "-"),
                    )
                ) if url_intake else None,
                html.Div(
                    (
                        "Input reconstruction: mode={0}, llm={1}, "
                        "classification_text_lines={2}"
                    ).format(
                        raw.get("input_reconstruction_mode") or "-",
                        "yes" if raw.get("input_reconstruction_available") else "no",
                        raw.get("classification_input_fact_texts_count"),
                    )
                ) if raw.get("input_reconstruction_available") else None,
                detail_block(
                    "URL collection pipeline steps",
                    url_intake.get("pipeline_steps") or [],
                    max_height=260,
                ) if url_intake.get("pipeline_steps") else None,
            ],
            style={"fontSize": "12px", "color": "#334155", "marginTop": "6px"},
        )

    if stage == "Evidence_Intake_Agent":
        if status == "running":
            return html.Div(
                "Product Evidence Builder 실행 전입니다. 완료 이벤트에서 실제 OCR/composition count를 표시합니다.",
                style={"fontSize": "12px", "color": "#64748b", "marginTop": "6px"},
            )
        pes = partial.get("input_processing_summary") or {}
        if not pes:
            return html.Div(
                "Product evidence 생성 완료. 상세 audit은 Admin log에서 확인할 수 있습니다.",
                style={"fontSize": "12px", "color": "#64748b", "marginTop": "6px"},
            )
        return html.Div(
            [
                html.Div(f"product_id: {pes.get('product_id') or '-'}"),
                html.Div(f"observed: {pes.get('product_name') or '-'} / OCR {pes.get('ocr_text_count') or 0} chunks"),
                html.Div(f"composition facts: {pes.get('composition_count') or 0}"),
                html.Div(f"inferred facts: {pes.get('inferred_fact_count') or 0}"),
                html.Div(f"unknowns: {', '.join(pes.get('unknowns') or []) or '-'}"),
            ],
            style={"fontSize": "12px", "color": "#334155", "marginTop": "6px"},
        )

    if stage == "Classification_Agent":
        if status == "running":
            return html.Div(
                "Classification retriever/LLM/TARIC branch resolver 실행 중입니다. 완료 이벤트에서 후보 수를 표시합니다.",
                style={"fontSize": "12px", "color": "#64748b", "marginTop": "6px"},
            )
        ccs = partial.get("candidate_code_set") or {}
        candidates = ccs.get("candidates") or []
        return html.Div(
            [
                html.Div(f"candidate_set: {ccs.get('candidate_set_id') or '-'} / {len(candidates)} candidates"),
                html.Ul(
                    [
                        html.Li(
                            f"#{c.get('rank')} CN8 {c.get('cn8')} -> TARIC10 {c.get('taric10') or '-'} "
                            f"({c.get('candidate_source') or 'classifier'}, conf={c.get('confidence')})"
                        )
                        for c in candidates[:6]
                    ],
                    style={"margin": "4px 0 0 18px", "padding": 0},
                ),
                detail_block("CandidateCodeSet JSON", ccs, max_height=420) if ccs else None,
            ],
            style={"fontSize": "12px", "color": "#334155", "marginTop": "6px"},
        )

    if stage == "Document_Agent":
        dp = partial.get("document_package") or {}
        if not dp:
            return None
        return html.Div(
            [
                html.Div(f"document_package: {dp.get('document_package_id') or '-'} / TARIC10 {dp.get('taric10') or '-'}"),
                html.Div(
                    "counts: customs {0}, required_docs {1}, regulations {2}, missing {3}".format(
                        len(dp.get("customs_check_items") or []),
                        len(dp.get("required_documents") or []),
                        len(dp.get("product_regulations") or []),
                        len(dp.get("missing_facts") or []),
                    )
                ),
                detail_block(
                    "DocumentPackage compact JSON",
                    {k: v for k, v in dp.items() if k != "raw_document_package"},
                    max_height=360,
                ),
            ],
            style={"fontSize": "12px", "color": "#334155", "marginTop": "6px"},
        )

    if stage == "Orchestrator_Agent":
        dec = partial.get("decision") or {}
        if not dec:
            return None
        return html.Div(
            [
                html.Div(f"decision: {dec.get('decision_status') or '-'}"),
                html.Div(f"selected: {', '.join(dec.get('selected_candidate_ids') or []) or '-'}"),
                html.Div(f"user_questions: {', '.join(dec.get('user_questions') or []) or '-'}"),
                detail_block("OrchestratorDecision JSON", dec, max_height=300),
            ],
            style={"fontSize": "12px", "color": "#334155", "marginTop": "6px"},
        )

    return None


def render_progress(result: dict[str, Any]) -> html.Div:
    status = result.get("job_status") or result.get("status") or "idle"
    events = result.get("events") or []
    rows = []
    for event in events[-18:]:
        st = event.get("status") or "running"
        color = "#166534" if st == "completed" else "#b91c1c" if st == "failed" else "#2563eb"
        bg = "#f0fdf4" if st == "completed" else "#fef2f2" if st == "failed" else "#eff6ff"
        rows.append(
            html.Div(
                [
                    html.Span(event.get("ts") or "", style={**MONO, "color": "#64748b", "marginRight": "8px"}),
                    html.Span(st.upper(), style={**PILL, "background": bg, "color": color}),
                    html.Span(display_stage_name(event.get("stage")), style={"fontWeight": 850}),
                    html.Div(display_stage_message(event.get("message")), style={"fontSize": "12px", "color": "#334155", "marginTop": "4px"}),
                    _event_summary(event),
                    detail_block(
                        "stage event JSON",
                        {k: v for k, v in event.items() if k != "partial_result"},
                        max_height=260,
                    ),
                ],
                style={"padding": "8px 0", "borderBottom": "1px solid #eef2f7"},
            )
        )

    return html.Div(
        [
            html.Div(
                [
                    _small("job", status),
                    _small("run_id", result.get("run_id")),
                    _small("run_dir", result.get("run_dir")),
                    _small("agents", len(result.get("agent_results") or [])),
                ],
                style={"display": "flex", "gap": "10px", "flexWrap": "wrap", "marginBottom": "10px"},
            ),
            html.Div(rows or html.Div("아직 stage event가 없습니다.", style=PLACEHOLDER), style=CARD),
        ]
    )


def render_candidate_cards(result: dict[str, Any]) -> html.Div:
    ccs = result.get("candidate_code_set") or {}
    candidates = ccs.get("candidates") or []
    run_id = result.get("run_id") or "current"
    if not candidates:
        return html.Div("분류 후보가 없습니다.", style=PLACEHOLDER)

    cards = []
    for cand in candidates:
        taric10 = cand.get("taric10") or ""
        branches = cand.get("taric10_branch_candidates") or []
        href = f"/document/{run_id}/{taric10}" if taric10 else "#"
        cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(f"rank {cand.get('rank')}", style=PILL),
                            html.Span(cand.get("candidate_source") or "classifier", style={**PILL, "background": "#f8fafc", "color": "#334155"}),
                            html.Span(cand.get("status") or "-", style={**PILL, "background": "#f0fdf4", "color": "#166534"}),
                        ],
                        style={"marginBottom": "8px"},
                    ),
                    html.Div(
                        [
                            _small("CN8", cand.get("cn8")),
                            _small("TARIC10", cand.get("taric10")),
                            _small("HS6", cand.get("hs6")),
                            _small("confidence", cand.get("confidence")),
                            _small("branches", len(branches)),
                        ],
                        style={"display": "flex", "gap": "10px", "flexWrap": "wrap"},
                    ),
                    html.Div(cand.get("selected_taric10_reason") or "", style={"fontSize": "12px", "color": "#64748b", "marginTop": "10px"}),
                    dcc.Link(
                        "TARIC10 서류 상세 보기",
                        href=href,
                        style={
                            "display": "inline-block",
                            "marginTop": "12px",
                            "padding": "8px 12px",
                            "borderRadius": "8px",
                            "background": "#2563eb",
                            "color": "white",
                            "fontWeight": 850,
                            "textDecoration": "none",
                            "fontSize": "12px",
                        },
                    ) if taric10 else None,
                ],
                style={**CARD, "marginBottom": "10px", "borderLeft": "4px solid #2563eb"},
            )
        )
    return html.Div(cards)


def render_decision(result: dict[str, Any]) -> html.Div:
    dec = result.get("decision") or {}
    if not dec:
        return html.Div("최종 결정이 아직 없습니다.", style=PLACEHOLDER)

    user_questions = result.get("user_questions") or []

    return html.Div(
        [
            html.Div(
                [
                    _small("decision", dec.get("decision_status")),
                    _small("selected", ", ".join(dec.get("selected_candidate_ids") or [])),
                    _small("packages", ", ".join(dec.get("document_package_ids") or [])),
                ],
                style={"display": "flex", "gap": "10px", "flexWrap": "wrap", "marginBottom": "10px"},
            ),
            html.Div(
                [
                    html.Div("추가 확인 필요", style={"fontSize": "13px", "fontWeight": 700, "marginBottom": "6px"}),
                    html.Ul(
                        [html.Li(q.get("question") or q.get("fact_key") or "") for q in user_questions],
                        style={"fontSize": "13px", "color": "#334155", "marginTop": 0},
                    ),
                ]
            ) if user_questions else html.Div("추가 확인 필요 없음", style={"fontSize": "13px", "color": "#166534"}),
        ],
        style=CARD,
    )


def render_page(result: dict[str, Any] | None = None) -> html.Div:
    result = result or {}
    run_id = result.get("run_id")
    inputProcessingView = input_processing_view_card(
        result.get("input_processing_view"),
    )
    requestFacts = (
        (result.get("request") or {}).get("facts")
        or {}
    )
    return html.Div(
        [
            html.Div(
                [
                    html.H1("ASAP 수출 분류", style={"fontSize": "24px", "margin": 0}),
                    html.Div("URL / text / OCR evidence -> CN8/TARIC10 후보 -> 서류 상세 연결", style={"fontSize": "12px", "color": "#64748b", "marginTop": "4px"}),
                    html.Div(
                        [
                            dcc.Link(
                                "Admin log",
                                href=f"/admin/{run_id}" if run_id else "/admin",
                                style={"fontSize": "12px", "fontWeight": 850},
                            ),
                        ],
                        style={"marginTop": "8px"},
                    ),
                ],
                style={"borderBottom": "2px solid #2563eb", "paddingBottom": "12px", "marginBottom": "22px"},
            ),
            render_input_form(requestFacts),
            html.Div("입력 수집/복원 결과", style=LABEL) if inputProcessingView else None,
            inputProcessingView,
            html.Div("진행 상태", style=LABEL),
            html.Div(render_progress(result) if result else html.Div("[Run] 버튼을 누르면 단계별 진행 상태가 표시됩니다.", style=PLACEHOLDER), id="out-progress"),
            html.Div("분류 결과", style={**LABEL, "marginTop": "22px"}),
            html.Div(render_candidate_cards(result) if result else html.Div("분류 결과가 여기에 표시됩니다.", style=PLACEHOLDER), id="out-classification"),
            html.Div("최종 결정", style={**LABEL, "marginTop": "22px"}),
            html.Div(render_decision(result) if result else html.Div("Orchestrator 결정이 여기에 표시됩니다.", style=PLACEHOLDER), id="out-decision"),
        ]
    )
