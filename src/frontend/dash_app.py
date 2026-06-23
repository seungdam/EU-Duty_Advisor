"""Dash frontend composition for the ASAP UI."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dash import ALL, MATCH, Dash, Input, Output, State, dcc, html, no_update

from agents.document_pipeline import run_document_pipeline
from backend import PipelineApi, PipelineRunService, RunRegistry
from frontend.ui import document_package_dash, admin_dash, classification_dash

PipelineCallable = Callable[..., dict[str, Any]]


def CreateDashApp(
    *,
    pipelineCallable: PipelineCallable = run_document_pipeline,
) -> Dash:
    registry = RunRegistry()
    service = PipelineRunService(registry=registry, pipelineCallable=pipelineCallable)
    pipelineApi = PipelineApi(registry=registry, service=service)

    app = Dash(
        __name__,
        title="ASAP - 수출 분류·서류 추천",
        suppress_callback_exceptions=True,
    )
    app.index_string = document_package_dash.app.index_string
    pipelineApi.RegisterRoutes(app.server)

    app.layout = html.Div(
        [
            dcc.Location(id="url", refresh=False),
            dcc.Store(id="store-run-id"),
            dcc.Store(id="store-result"),
            dcc.Store(id="document-panel-store", data="overview"),
            html.Div(id="sse-bridge", style={"display": "none"}),
            html.Div(id="page-root"),
        ],
        style={
            "maxWidth": "1440px",
            "margin": "0 auto",
            "padding": "24px",
            "fontFamily": "-apple-system, BlinkMacSystemFont, 'SF Pro', 'Apple SD Gothic Neo', sans-serif",
        },
    )

    _RegisterClientsideCallbacks(app)
    _RegisterServerCallbacks(app)
    return app


def _RegisterClientsideCallbacks(app: Dash) -> None:
    app.clientside_callback(
        """
        async function(nClicks, productName, description, kurlyUrl, currentRunId, resultData) {
            const dc = window.dash_clientside || dash_clientside;
            if (!nClicks) {
                return [dc.no_update, dc.no_update];
            }
            if (
                currentRunId &&
                resultData &&
                resultData.job_id === currentRunId &&
                ["queued", "running"].includes(resultData.job_status)
            ) {
                return [currentRunId, "/classification"];
            }

            const clean = function(value) {
                return (value || "").toString().trim();
            };
            const nextProductName = clean(productName);
            const nextDescription = clean(description);
            const nextKurlyUrl = clean(kurlyUrl);
            const query = nextProductName || nextDescription || nextKurlyUrl;
            const facts = {
                product_name: nextProductName,
                description: nextDescription,
                url: nextKurlyUrl,
                source_urls: nextKurlyUrl ? [nextKurlyUrl] : [],
                origin_country: "KR",
                intended_use: "human consumption"
            };
            const setStore = function(data) {
                if (dc.set_props) {
                    dc.set_props("store-result", {data: data});
                }
            };

            if (!query) {
                setStore({
                    job_id: null,
                    job_status: "failed",
                    request: {query: query, facts: facts},
                    error: "제품명, 설명, URL 중 하나는 입력해야 합니다.",
                    events: [{
                        stage: "Input",
                        status: "failed",
                        message: "제품명, 설명, URL 중 하나는 입력해야 합니다."
                    }]
                });
                return [dc.no_update, "/classification"];
            }

            try {
                const response = await fetch("/api/runs", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        query: query,
                        product_name: nextProductName,
                        description: nextDescription,
                        url: nextKurlyUrl,
                        facts: facts
                    })
                });
                const payload = await response.json();
                if (!response.ok) {
                    throw new Error(payload.message || payload.error || "run_create_failed");
                }

                const queuedResult = {
                    job_id: payload.job_id,
                    job_status: payload.status || "queued",
                    request: {query: query, facts: facts},
                    events: [{
                        stage: "Pipeline",
                        status: payload.status || "queued",
                        message: payload.reused ? "기존 실행 중인 작업에 연결했습니다." : "작업이 등록되었습니다."
                    }]
                };
                setStore(queuedResult);

                try {
                    const snapshotResponse = await fetch(payload.result_url);
                    if (snapshotResponse.ok) {
                        setStore(await snapshotResponse.json());
                    }
                } catch (snapshotError) {
                    // queuedResult is already enough to render initial progress.
                }
                return [payload.job_id, "/classification"];
            } catch (error) {
                setStore({
                    job_id: null,
                    job_status: "failed",
                    request: {query: query, facts: facts},
                    error: String(error && error.message ? error.message : error),
                    events: [{
                        stage: "Pipeline",
                        status: "failed",
                        message: String(error && error.message ? error.message : error)
                    }]
                });
                return [dc.no_update, "/classification"];
            }
        }
        """,
        Output("store-run-id", "data"),
        Output("url", "pathname"),
        Input("btn-run", "n_clicks"),
        State("ipt-product-name", "value"),
        State("ipt-description", "value"),
        State("ipt-kurly-url", "value"),
        State("store-run-id", "data"),
        State("store-result", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(jobId) {
            const dc = window.dash_clientside || dash_clientside;
            const noUpdate = dc.no_update;
            const setStore = function(data) {
                if (dc.set_props) {
                    dc.set_props("store-result", {data: data});
                }
            };
            const closeCurrent = function() {
                if (window.asapPipelineSse && window.asapPipelineSse.source) {
                    window.asapPipelineSse.source.close();
                }
                window.asapPipelineSse = null;
            };
            const isTerminal = function(status) {
                return ["completed", "failed"].includes(status);
            };

            closeCurrent();
            if (!jobId) {
                return noUpdate;
            }

            fetch(`/api/runs/${jobId}`)
                .then(function(response) {
                    if (!response.ok) {
                        throw new Error("run_not_found");
                    }
                    return response.json();
                })
                .then(function(snapshot) {
                    let state = Object.assign({}, snapshot);
                    setStore(state);
                    if (isTerminal(state.job_status)) {
                        return;
                    }

                    const eventStart = Array.isArray(state.events) ? state.events.length : 0;
                    const source = new EventSource(`/api/runs/${jobId}/events?start=${eventStart}`);
                    window.asapPipelineSse = {jobId: jobId, source: source};

                    const pushState = function(nextState) {
                        state = Object.assign({}, nextState, {job_id: jobId});
                        setStore(state);
                    };
                    const applyPipelineEvent = function(eventPayload) {
                        const events = Array.isArray(state.events) ? state.events.slice() : [];
                        events.push(eventPayload);
                        const partial = (
                            eventPayload.partial_result &&
                            typeof eventPayload.partial_result === "object"
                        ) ? eventPayload.partial_result : {};
                        const nextStatus = (
                            eventPayload.stage === "Pipeline" &&
                            isTerminal(eventPayload.status)
                        ) ? eventPayload.status : "running";
                        pushState(Object.assign({}, state, partial, {
                            events: events,
                            job_status: isTerminal(state.job_status) ? state.job_status : nextStatus
                        }));
                    };

                    source.addEventListener("pipeline_event", function(event) {
                        try {
                            applyPipelineEvent(JSON.parse(event.data));
                        } catch (parseError) {
                            pushState(Object.assign({}, state, {
                                job_status: "failed",
                                error: String(parseError)
                            }));
                        }
                    });

                    source.addEventListener("run_complete", function(event) {
                        let payload = {};
                        try {
                            payload = JSON.parse(event.data || "{}");
                        } catch (parseError) {
                            payload = {};
                        }
                        source.close();
                        fetch(`/api/runs/${jobId}`)
                            .then(function(response) {
                                return response.ok ? response.json() : null;
                            })
                            .then(function(finalSnapshot) {
                                pushState(finalSnapshot || Object.assign({}, state, {
                                    job_status: payload.status || state.job_status || "completed"
                                }));
                            })
                            .catch(function() {
                                pushState(Object.assign({}, state, {
                                    job_status: payload.status || state.job_status || "completed"
                                }));
                            });
                    });

                    source.addEventListener("error", function(event) {
                        if (event && event.data) {
                            try {
                                const payload = JSON.parse(event.data);
                                pushState(Object.assign({}, state, {
                                    job_status: "failed",
                                    error: payload.message || "sse_error"
                                }));
                            } catch (parseError) {
                                pushState(Object.assign({}, state, {
                                    job_status: "failed",
                                    error: String(parseError)
                                }));
                            }
                            source.close();
                        }
                    });
                })
                .catch(function(error) {
                    setStore({
                        job_id: jobId,
                        job_status: "failed",
                        error: String(error && error.message ? error.message : error),
                        events: [{
                            stage: "Pipeline",
                            status: "failed",
                            message: String(error && error.message ? error.message : error)
                        }]
                    });
                });

            return noUpdate;
        }
        """,
        Output("sse-bridge", "children"),
        Input("store-run-id", "data"),
        prevent_initial_call=True,
    )


def _RegisterServerCallbacks(app: Dash) -> None:
    @app.callback(
        Output("btn-run", "disabled"),
        Input("store-run-id", "data"),
        Input("store-result", "data"),
    )
    def toggle_run_button(job_id, result_data):
        if not job_id or not isinstance(result_data, dict):
            return False
        if result_data.get("job_id") != job_id:
            return False
        return result_data.get("job_status") in {"queued", "running"}

    @app.callback(
        Output("document-panel-store", "data"),
        Input({"type": "panel-btn", "panel": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def select_document_panel(_clicks):
        from dash import ctx

        triggered = ctx.triggered_id
        if isinstance(triggered, dict) and triggered.get("type") == "panel-btn":
            return triggered.get("panel") or "overview"
        return no_update

    @app.callback(
        Output({"type": "scenario-result", "taric": MATCH}, "children"),
        Input({"type": "scenario-checks", "taric": MATCH}, "value"),
        State("package-store", "data"),
        prevent_initial_call=True,
    )
    def update_document_scenario(selected_values, package_data):
        if not package_data:
            return no_update
        cx = document_package_dash.package_context(package_data)
        if cx.get("source") == "unresolved":
            return no_update
        return document_package_dash.render_scenario_decision(
            package_data,
            cx,
            selected_values or [],
        )

    @app.callback(
        Output("page-root", "children"),
        Input("url", "pathname"),
        Input("store-result", "data"),
        Input("document-panel-store", "data"),
    )
    def render_page(pathname, result_data, document_panel):
        parts = _SplitPath(pathname)
        if not parts:
            return classification_dash.render_page(result_data)

        page = parts[0]
        if page == "document":
            runId = parts[1] if len(parts) > 1 else None
            taric10 = parts[2] if len(parts) > 2 else ""
            return document_package_dash.render_detail_page(
                runId,
                taric10,
                document_panel or "overview",
            )

        if page == "admin":
            runId = parts[1] if len(parts) > 1 else None
            live = (
                result_data
                if result_data and (not runId or result_data.get("run_id") == runId)
                else None
            )
            return admin_dash.render_page(run_id=runId, live_result=live)

        return classification_dash.render_page(result_data)


def _SplitPath(pathname: str | None) -> list[str]:
    return [part for part in (pathname or "/classification").split("/") if part]
