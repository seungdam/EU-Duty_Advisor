"""HTTP API boundary for pipeline runs."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict, deque
from math import ceil
from pathlib import Path
from threading import Lock

from flask import Flask, Response, jsonify, request as flask_request
from flask.typing import ResponseReturnValue
from pydantic import ValidationError

from backend.api_contract import (
    ApiErrorResponse,
    QuestionAnswersRequestPayload,
    RunCreateAcceptedResponse,
    RunCreateRequestPayload,
)
from backend.pipeline_executor import PipelineRunCapacityError
from backend.pipeline_service import PipelineRunRequest, PipelineRunService, RunRegistry
from bussiness_logic.utils.json_types import JsonMapping, JsonObject


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

    def RegisterRoutes(self, server: Flask) -> None:
        @server.route("/api/runs", methods=["POST"])
        def create_run() -> ResponseReturnValue:
            isAllowed, retryAfterSeconds = self._AllowRunCreate(
                flask_request.remote_addr or "unknown",
            )
            if not isAllowed:
                return jsonify(ApiErrorResponse(
                    error="rate_limited",
                    message="Too many pipeline run create requests.",
                    hint="Wait before creating another run.",
                    retry_after_seconds=retryAfterSeconds,
                ).ToDict()), 429, {"Retry-After": str(retryAfterSeconds)}

            payload = flask_request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                return jsonify(ApiErrorResponse(
                    error="invalid_json_payload",
                    message="Request body must be a JSON object.",
                    hint="Send Content-Type: application/json with an object payload.",
                ).ToDict()), 400
            responsePayload, statusCode = self.StartRunFromPayload(payload)
            return jsonify(responsePayload), statusCode

        @server.route("/api/reconstruction-runs", methods=["POST"])
        def rerun_input_reconstruction() -> ResponseReturnValue:
            payload = flask_request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                return jsonify(ApiErrorResponse(
                    error="invalid_json_payload",
                    message="Request body must be a JSON object.",
                    hint="Send url or product_id.",
                ).ToDict()), 400
            responsePayload, statusCode = self.RerunCachedInputReconstruction(
                payload,
            )
            return jsonify(responsePayload), statusCode

        @server.route("/api/runs/<job_id>")
        def read_run_snapshot(job_id: str) -> ResponseReturnValue:
            snapshot = self.ReadRunSnapshot(job_id)
            if not snapshot:
                return jsonify(ApiErrorResponse(
                    error="run_not_found",
                    message="No run exists for the requested job_id.",
                    field="job_id",
                    job_id=job_id,
                ).ToDict()), 404
            return jsonify(snapshot)

        @server.route("/api/runs/<job_id>/question-answers", methods=["POST"])
        def answer_classification_questions(job_id: str) -> ResponseReturnValue:
            payload = flask_request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                return jsonify(ApiErrorResponse(
                    error="invalid_json_payload",
                    message="Request body must be a JSON object.",
                    hint="Send an answers array.",
                ).ToDict()), 400
            try:
                requestPayload = QuestionAnswersRequestPayload.model_validate(payload)
            except ValidationError as error:
                firstError = error.errors()[0] if error.errors() else {}
                return jsonify(ApiErrorResponse(
                    error="invalid_question_answer_payload",
                    message=str(firstError.get("msg") or "Invalid answer payload."),
                    field=".".join(
                        str(part) for part in (firstError.get("loc") or ("answers",))
                    ),
                ).ToDict()), 400
            self._RestorePersistedRun(job_id)
            runDirectory = self._ResolveRunDirectory(job_id)
            if runDirectory is None:
                return jsonify(ApiErrorResponse(
                    error="run_not_found",
                    message="No run exists for the requested job_id.",
                    field="job_id",
                    job_id=job_id,
                ).ToDict()), 404
            try:
                snapshot = self._service.ReclassifyWithQuestionAnswers(
                    job_id,
                    runDirectory,
                    [answer.ToDict() for answer in requestPayload.answers],
                )
            except KeyError as error:
                return jsonify(ApiErrorResponse(
                    error="question_not_found",
                    message=str(error),
                    field="user_question_id",
                    job_id=job_id,
                ).ToDict()), 404
            except (OSError, ValueError, RuntimeError) as error:
                return jsonify(ApiErrorResponse(
                    error="classification_replay_failed",
                    message=str(error),
                    field="answers",
                    job_id=job_id,
                ).ToDict()), 409
            return jsonify(snapshot), 200

        @server.route("/api/runs/<job_id>/document-packages")
        def list_document_packages(job_id: str) -> ResponseReturnValue:
            payload = self.ReadDocumentPackageCollection(job_id)
            if not payload:
                return jsonify(ApiErrorResponse(
                    error="run_not_found",
                    message="No run exists for the requested job_id.",
                    field="job_id",
                    job_id=job_id,
                ).ToDict()), 404
            return jsonify(payload)

        @server.route("/api/runs/<job_id>/document-packages/<package_id>")
        def read_document_package(job_id: str, package_id: str) -> ResponseReturnValue:
            runPayload = self.ReadDocumentPackageCollection(job_id)
            if not runPayload:
                return jsonify(ApiErrorResponse(
                    error="run_not_found",
                    message="No run exists for the requested job_id.",
                    field="job_id",
                    job_id=job_id,
                ).ToDict()), 404
            packagePayload = self.ReadDocumentPackageDetail(job_id, package_id)
            if not packagePayload:
                return jsonify(ApiErrorResponse(
                    error="document_package_not_found",
                    message="No document package matches the requested package_id or TARIC10.",
                    field="package_id",
                    job_id=job_id,
                ).ToDict()), 404
            return jsonify(packagePayload)

        @server.route("/api/admin/runs/<job_id>/blackboard")
        def read_admin_blackboard(job_id: str) -> ResponseReturnValue:
            payload = self.ReadAdminBlackboardView(job_id)
            if not payload:
                return jsonify(ApiErrorResponse(
                    error="run_blackboard_not_found",
                    message="No blackboard artifact exists for the requested job_id.",
                    field="job_id",
                    job_id=job_id,
                ).ToDict()), 404
            return jsonify(payload)

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

    def ReadAdminBlackboardView(self, jobId: str) -> JsonObject:
        runDirectory = self._ResolveRunDirectory(jobId)
        if runDirectory is None:
            return {}
        blackboardPath = runDirectory / "blackboard.json"
        if not blackboardPath.is_file():
            return {}
        try:
            blackboard = json.loads(blackboardPath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(blackboard, dict):
            return {}
        return {
            "job_id": jobId,
            "run_id": str(blackboard.get("run_id") or ""),
            "run_dir": str(runDirectory),
            "blackboard_keys": sorted(blackboard.keys()),
            "product_evidence_state": blackboard.get("product_evidence_state") or {},
            "product_understanding": blackboard.get("product_understanding") or {},
        }

    def _ResolveRunDirectory(self, jobId: str) -> Path | None:
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+", jobId or ""):
            return None
        snapshotEntry = self._registry.ReadSnapshotByIdentifier(jobId)
        if snapshotEntry is not None:
            _, snapshot = snapshotEntry
            resultData = snapshot.get("result") or snapshot.get("partial_result") or {}
            if isinstance(resultData, dict):
                runDirText = str(resultData.get("run_dir") or "")
                if runDirText and Path(runDirText).is_dir():
                    return Path(runDirText)
        from bussiness_logic.pipeline.run_paths import (
            PIPELINE_OUTPUTS_ROOT,
        )

        for candidate in PIPELINE_OUTPUTS_ROOT.glob(f"*/{jobId}"):
            if (candidate / "blackboard.json").is_file():
                return candidate
        restoredArtifactDirectory = (
            Path(__file__).resolve().parents[2] / "artifacts" / jobId
        )
        if (restoredArtifactDirectory / "blackboard.json").is_file():
            return restoredArtifactDirectory
        return None

    def ReadDocumentPackageCollection(self, jobId: str) -> JsonObject:
        self._RestorePersistedRun(jobId)
        return self._registry.BuildDocumentPackageCollection(jobId)

    def ReadDocumentPackageDetail(
        self,
        jobId: str,
        packageId: str,
    ) -> JsonObject:
        self._RestorePersistedRun(jobId)
        return self._registry.BuildDocumentPackageDetail(jobId, packageId)

    def ReadRunSnapshot(self, jobId: str) -> JsonObject:
        self._RestorePersistedRun(jobId)
        return self._registry.BuildUiResult(jobId)

    def _RestorePersistedRun(self, jobId: str) -> None:
        if self._registry.BuildUiResult(jobId):
            return
        runDirectory = self._ResolveRunDirectory(jobId)
        if runDirectory is None:
            return
        snapshotPath = runDirectory / "api_snapshot.json"
        if snapshotPath.is_file():
            try:
                snapshot = json.loads(snapshotPath.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                snapshot = None
            if isinstance(snapshot, dict):
                blackboardPath = runDirectory / "blackboard.json"
                if blackboardPath.is_file():
                    try:
                        persistedBlackboard = json.loads(
                            blackboardPath.read_text(encoding="utf-8"),
                        )
                    except (OSError, ValueError):
                        persistedBlackboard = None
                    if isinstance(persistedBlackboard, dict):
                        snapshot["document_packages"] = list(
                            persistedBlackboard.get("document_packages") or [],
                        )
                self._registry.RestoreRun(jobId, snapshot)
                if self._registry.BuildUiResult(jobId):
                    return

        blackboardPath = runDirectory / "blackboard.json"
        if not blackboardPath.is_file():
            return
        try:
            blackboard = json.loads(blackboardPath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(blackboard, dict):
            return
        candidateSets = blackboard.get("candidate_code_sets") or []
        documentPackages = blackboard.get("document_packages") or []
        result = self._registry.BuildPipelineResultProjection({
            "blackboard": blackboard,
            "candidate_code_set": candidateSets[-1] if candidateSets else None,
            "document_package": documentPackages[-1] if documentPackages else None,
            "component_runs": blackboard.get("component_runs") or [],
            "run_id": str((blackboard.get("run_context") or {}).get("run_id") or ""),
            "run_dir": str(runDirectory),
        })
        productEvidenceState = blackboard.get("product_evidence_state") or {}
        observedFacts = (
            productEvidenceState.get("observed_facts") or {}
            if isinstance(productEvidenceState, dict)
            else {}
        )
        if not isinstance(observedFacts, dict):
            observedFacts = {}
        sourceUrls = observedFacts.get("source_urls") or []
        if not isinstance(sourceUrls, list):
            sourceUrls = []
        facts = {
            "product_name": observedFacts.get("product_name") or "",
            "description": observedFacts.get("description") or "",
            "url": str(sourceUrls[0] if sourceUrls else ""),
            "source_urls": sourceUrls[:8],
            "origin_country": observedFacts.get("origin_country") or "unknown",
            "intended_use": observedFacts.get("intended_use") or "unknown",
        }
        ingredients = observedFacts.get("ingredients") or []
        if isinstance(ingredients, list) and ingredients:
            facts["user_input_facts"] = {"ingredients": ingredients[:20]}
        self._registry.RestoreRun(jobId, {
            "query": facts.get("product_name") or "",
            "facts": facts,
            "events": [],
            "result": result,
            "document_packages": documentPackages,
        })

    def StartRunFromPayload(self, payload: JsonMapping) -> tuple[JsonObject, int]:
        try:
            requestPayload = RunCreateRequestPayload.model_validate(payload)
        except ValidationError as error:
            firstError = error.errors()[0] if error.errors() else {}
            errorLocation = firstError.get("loc") or ("request",)
            errorField = ".".join(str(part) for part in errorLocation)
            errorMessage = str(firstError.get("msg") or "Invalid request value.")
            return ApiErrorResponse(
                error="invalid_run_create_payload",
                message=errorMessage,
                field=errorField,
                hint="Check the structured input field and submit it again.",
            ).ToDict(), 400
        extraFacts = dict(requestPayload.facts)
        for reservedField in (
            "ingredients",
            "intended_use",
            "origin_country",
            "user_input_facts",
        ):
            extraFacts.pop(reservedField, None)
        productName = str(
            requestPayload.productName or extraFacts.get("product_name") or ""
        ).strip()
        description = str(
            requestPayload.description or extraFacts.get("description") or ""
        ).strip()
        kurlyUrl = str(
            requestPayload.url
            or requestPayload.kurlyUrl
            or extraFacts.get("url")
            or ""
        ).strip()
        query = str(
            requestPayload.query or productName or description or kurlyUrl
        ).strip()
        if not query:
            return ApiErrorResponse(
                error="missing_query",
                message="At least one of query, product_name, description, or url is required.",
                field="query",
                hint="Provide product_name for normal UI use or query for direct API use.",
            ).ToDict(), 400

        facts = self.BuildRunFacts(
            productName=productName,
            description=description,
            kurlyUrl=kurlyUrl,
            extraFacts=extraFacts,
            inputFacts=(
                requestPayload.inputFacts.ToDict()
                if requestPayload.inputFacts is not None
                else None
            ),
        )
        try:
            jobId, reused = self.StartPipelineRun(query=query, facts=facts)
        except PipelineRunCapacityError as error:
            return ApiErrorResponse(
                error="pipeline_capacity_exhausted",
                message="No pipeline execution capacity is currently available.",
                field="job_id",
                hint="Wait for a running or queued pipeline run to finish.",
                job_id=error.runId,
            ).ToDict(), 503
        snapshot = self._registry.BuildUiResult(jobId)
        return RunCreateAcceptedResponse(
            job_id=jobId,
            status=snapshot.get("job_status") or "queued",
            reused=reused,
            events_url=f"/api/runs/{jobId}/events",
            result_url=f"/api/runs/{jobId}",
        ).ToDict(), 202

    def RerunCachedInputReconstruction(
        self,
        payload: JsonMapping,
    ) -> tuple[JsonObject, int]:
        productIdentifier = str(
            payload.get("url")
            or payload.get("kurly_url")
            or payload.get("product_id")
            or ""
        ).strip()
        if not productIdentifier:
            return ApiErrorResponse(
                error="missing_cached_product_identifier",
                message="url or product_id is required.",
                field="url",
            ).ToDict(), 400
        try:
            from bussiness_logic.product.pipeline.kurly_url_facts import (
                RerunCachedInputReconstruction,
            )
            from backend.pipeline_projection import InputProcessingViewProjector

            facts = RerunCachedInputReconstruction(productIdentifier)
            jobId = "reconstruct_{0}".format(uuid.uuid4().hex[:10])
            inputProcessingView = (
                InputProcessingViewProjector().BuildInputProcessingViewFromFacts(
                    facts,
                )
            )
            requestFacts = {
                "product_id": facts.get("product_id") or "",
                "product_name": facts.get("product_name") or "",
                "description": facts.get("description") or "",
                "url": facts.get("url") or "",
                "source_urls": facts.get("source_urls") or [],
            }
            return {
                "job_id": jobId,
                "job_status": "completed",
                "request": {
                    "query": str(
                        facts.get("product_name")
                        or facts.get("description")
                        or facts.get("url")
                        or productIdentifier
                    ),
                    "facts": requestFacts,
                },
                "input_processing_view": inputProcessingView,
                "events": [
                    {
                        "ts": time.strftime("%H:%M:%S"),
                        "stage": "Input_Reconstruction",
                        "status": "completed",
                        "message": "캐시된 OCR evidence로 LLM reconstruction만 재실행했습니다.",
                    }
                ],
                "warnings": facts.get("warnings") or [],
            }, 200
        except FileNotFoundError as error:
            return ApiErrorResponse(
                error="cached_reconstruction_artifact_not_found",
                message=str(error),
                field="product_id",
            ).ToDict(), 404
        except (ValidationError, ValueError, RuntimeError) as error:
            return ApiErrorResponse(
                error="cached_reconstruction_failed",
                message=str(error),
                field="reconstruction",
            ).ToDict(), 400

    def BuildRunFacts(
        self,
        *,
        productName: str,
        description: str,
        kurlyUrl: str,
        extraFacts: JsonMapping | None = None,
        inputFacts: JsonMapping | None = None,
    ) -> JsonObject:
        facts: JsonObject = dict(extraFacts or {})
        facts.update({
            "product_name": productName,
            "description": description,
            "url": kurlyUrl,
            "source_urls": [kurlyUrl] if kurlyUrl else facts.get("source_urls", []),
        })
        normalizedInputFacts = {
            key: value
            for key, value in dict(inputFacts or {}).items()
            if value not in ("", [], None)
        }
        if normalizedInputFacts:
            facts["user_input_facts"] = normalizedInputFacts
        return facts

    def StartPipelineRun(
        self,
        *,
        query: str,
        facts: JsonMapping,
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
