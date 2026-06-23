"""HTTP API boundary for pipeline runs."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from collections.abc import Mapping
from math import ceil
from threading import Lock
from typing import Any

from flask import Response, jsonify, request as flask_request
from flask.typing import ResponseReturnValue

from backend.pipeline_service import PipelineRunRequest, PipelineRunService, RunRegistry


class PipelineApi:
    """Flask route adapter for the pipeline run service."""

    def __init__(
        self,
        registry: RunRegistry,
        service: PipelineRunService,
        *,
        maxRunCreatesPerMinute: int = 12,
    ) -> None:
        self._registry = registry
        self._service = service
        self._maxRunCreatesPerMinute = max(1, maxRunCreatesPerMinute)
        self._rateLimitWindowSeconds = 60.0
        self._rateLimitLock = Lock()
        self._runCreateTimestamps: defaultdict[str, deque[float]] = defaultdict(deque)

    def RegisterRoutes(self, server: Any) -> None:
        @server.route("/api/runs", methods=["POST"])
        def create_run() -> ResponseReturnValue:
            isAllowed, retryAfterSeconds = self._AllowRunCreate(
                flask_request.remote_addr or "unknown",
            )
            if not isAllowed:
                return jsonify({
                    "error": "rate_limited",
                    "message": "Too many pipeline run create requests.",
                    "hint": "Wait before creating another run.",
                    "retry_after_seconds": retryAfterSeconds,
                }), 429, {"Retry-After": str(retryAfterSeconds)}

            payload = flask_request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                return jsonify({
                    "error": "invalid_json_payload",
                    "message": "Request body must be a JSON object.",
                    "hint": "Send Content-Type: application/json with an object payload.",
                }), 400
            responsePayload, statusCode = self.StartRunFromPayload(payload)
            return jsonify(responsePayload), statusCode

        @server.route("/api/runs/<job_id>")
        def read_run_snapshot(job_id: str) -> ResponseReturnValue:
            snapshot = self._registry.BuildUiResult(job_id)
            if not snapshot:
                return jsonify({
                    "error": "run_not_found",
                    "message": "No run exists for the requested job_id.",
                    "field": "job_id",
                    "job_id": job_id,
                }), 404
            return jsonify(snapshot)

        @server.route("/api/runs/<job_id>/events")
        def stream_run_events(job_id: str) -> Response:
            lastEventId = flask_request.headers.get("Last-Event-ID")
            startIndexText = lastEventId or flask_request.args.get("start") or "0"
            try:
                startIndex = int(startIndexText)
            except ValueError:
                startIndex = 0
            if lastEventId:
                startIndex += 1

            return Response(
                self._registry.StreamEvents(job_id, startIndex=startIndex),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

    def StartRunFromPayload(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        extraFacts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
        productName = str(
            payload.get("product_name") or extraFacts.get("product_name") or ""
        ).strip()
        description = str(
            payload.get("description") or extraFacts.get("description") or ""
        ).strip()
        kurlyUrl = str(
            payload.get("url")
            or payload.get("kurly_url")
            or extraFacts.get("url")
            or ""
        ).strip()
        query = str(
            payload.get("query") or productName or description or kurlyUrl
        ).strip()
        if not query:
            return {
                "error": "missing_query",
                "message": "At least one of query, product_name, description, or url is required.",
                "field": "query",
                "hint": "Provide product_name for normal UI use or query for direct API use.",
            }, 400

        facts = self.BuildRunFacts(
            productName=productName,
            description=description,
            kurlyUrl=kurlyUrl,
            extraFacts=extraFacts,
        )
        jobId, reused = self.StartPipelineRun(query=query, facts=facts)
        snapshot = self._registry.BuildUiResult(jobId)
        return {
            "job_id": jobId,
            "status": snapshot.get("job_status") or "queued",
            "reused": reused,
            "events_url": f"/api/runs/{jobId}/events",
            "result_url": f"/api/runs/{jobId}",
        }, 202

    def BuildRunFacts(
        self,
        *,
        productName: str,
        description: str,
        kurlyUrl: str,
        extraFacts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        facts = dict(extraFacts or {})
        facts.update({
            "product_name": productName,
            "description": description,
            "url": kurlyUrl,
            "source_urls": [kurlyUrl] if kurlyUrl else facts.get("source_urls", []),
            "origin_country": facts.get("origin_country") or "KR",
            "intended_use": facts.get("intended_use") or "human consumption",
        })
        return facts

    def StartPipelineRun(
        self,
        *,
        query: str,
        facts: Mapping[str, Any],
    ) -> tuple[str, bool]:
        jobId = f"job_{uuid.uuid4().hex[:10]}"
        runId = self._registry.CreateRun(
            jobId,
            status="queued",
            query=query,
            facts=facts,
            events=[
                {
                    "ts": time.strftime("%H:%M:%S"),
                    "stage": "Pipeline",
                    "status": "queued",
                    "message": "작업이 등록되었습니다.",
                }
            ],
            reuseActive=True,
        )
        if runId != jobId:
            return runId, True

        self._service.StartBackgroundRun(
            jobId,
            PipelineRunRequest(query=query, facts=dict(facts)),
        )
        return jobId, False

    def _AllowRunCreate(self, clientKey: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._rateLimitLock:
            timestamps = self._runCreateTimestamps[clientKey]
            while timestamps and now - timestamps[0] >= self._rateLimitWindowSeconds:
                timestamps.popleft()
            if len(timestamps) >= self._maxRunCreatesPerMinute:
                retryAfter = self._rateLimitWindowSeconds - (now - timestamps[0])
                return False, max(1, ceil(retryAfter))
            timestamps.append(now)
            return True, 0
