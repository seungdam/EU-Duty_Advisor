"""Pipeline backend Flask application composition."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import NotFound

from backend.pipeline_api import PipelineApi
from backend.pipeline_service import PipelineRunService, RunRegistry

PipelineCallable = Callable[..., dict[str, object]]


def CreateBackendApp(
    *,
    pipelineCallable: PipelineCallable | None = None,
    allowedFrontendOrigins: Sequence[str] = (),
    webappDistDir: Path | None = None,
) -> Flask:
    if pipelineCallable is None:
        from bussiness_logic.pipeline.pipeline_manager import (
            RunExportRequirementPipeline,
        )

        pipelineCallable = RunExportRequirementPipeline

    registry = RunRegistry()
    service = PipelineRunService(
        registry=registry,
        pipelineCallable=pipelineCallable,
    )
    pipelineApi = PipelineApi(
        registry=registry,
        service=service,
    )

    app = Flask(__name__)
    pipelineApi.RegisterRoutes(app)
    _RegisterCors(app, allowedFrontendOrigins)

    @app.get("/api/health")
    def read_health() -> Response:
        return jsonify({"status": "ok"})

    _RegisterWebappStatic(app, webappDistDir)
    return app


def _RegisterWebappStatic(app: Flask, distDir: Path | None) -> None:
    """React 빌드 산출물(webapp/dist)을 같은 origin에서 서빙 — 단일 프로세스 실행용.

    SPA 라우트(/classification, /admin, /document/...)는 index.html로 폴백한다.
    dist가 없으면(프론트 미빌드) API 전용으로 동작한다.
    """
    if distDir is None or not (distDir / "index.html").is_file():
        return

    @app.get("/", defaults={"spaPath": ""})
    @app.get("/<path:spaPath>")
    def serve_webapp(spaPath: str):
        if spaPath.startswith("api/"):
            return jsonify({
                "error": "not_found",
                "message": "Unknown API route.",
            }), 404
        if spaPath:
            try:
                return send_from_directory(distDir, spaPath)
            except NotFound:
                pass
        return send_from_directory(distDir, "index.html")


def _RegisterCors(app: Flask, allowedOrigins: Sequence[str]) -> None:
    allowed = frozenset(origin.rstrip("/") for origin in allowedOrigins if origin)

    @app.after_request
    def add_cors_headers(response: Response) -> Response:
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin not in allowed:
            return response
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Last-Event-ID"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
        return response
