"""UI-facing pipeline service and run registry."""

from __future__ import annotations

import json
import hashlib
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.api_contract import (
    DocumentPackageCollectionResponse,
    DocumentPackageDetailResponse,
    PipelineEventPayload,
    RunCompleteSsePayload,
    RunNotFoundSsePayload,
    RunPausedSsePayload,
)
from backend.pipeline_executor import PipelineRunCapacityError, PipelineRunExecutor
from backend.pipeline_projection import PipelineResultProjector
from bussiness_logic.document.document_package_builder import BuildDocumentPackage
from bussiness_logic.utils.json_types import JsonMapping, JsonObject, JsonValue


PipelineCallable = Callable[..., dict[str, object]]


def _ClassificationNeedsMoreFacts(result: JsonMapping) -> bool:
    candidateCodeSet = result.get("candidate_code_set")
    return (
        isinstance(candidateCodeSet, Mapping)
        and candidateCodeSet.get("classification_status") == "needs_more_facts"
    )


def _ClassificationFailed(result: JsonMapping) -> bool:
    candidateCodeSet = result.get("candidate_code_set")
    return (
        isinstance(candidateCodeSet, Mapping)
        and candidateCodeSet.get("classification_status") == "failed"
    )


def _RequiresUserInput(result: JsonMapping) -> bool:
    blackboard = result.get("blackboard")
    questions = result.get("user_questions") or (
        blackboard.get("user_questions")
        if isinstance(blackboard, Mapping)
        else []
    ) or []
    return (
        _ClassificationNeedsMoreFacts(result)
        and any(
            isinstance(question, Mapping) and bool(question.get("active"))
            for question in questions
        )
    )


class PipelineRunRequest(BaseModel):
    """Pipeline 실행 요청 DTO."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    query: str
    facts: JsonObject = Field(default_factory=dict)
    includeCelexExcerpt: bool = Field(
        default=False,
        alias="include_celex_excerpt",
    )


class RunRegistry:
    """Thread-safe run state and event buffer."""

    def __init__(self) -> None:
        self._runs: dict[str, JsonObject] = {}
        self._condition = threading.Condition(threading.Lock())
        self._projector = PipelineResultProjector()

    def CreateRun(
        self,
        runId: str,
        *,
        query: str,
        facts: JsonMapping,
        status: str = "queued",
        events: list[JsonObject] | None = None,
        reuseActive: bool = False,
    ) -> str:
        requestSignature = self._BuildRequestSignature(query, facts)
        with self._condition:
            if reuseActive:
                activeRunId = self._FindActiveRunBySignatureLocked(requestSignature)
                if activeRunId:
                    return activeRunId
            self._runs[runId] = {
                "status": status,
                "query": query,
                "request_signature": requestSignature,
                "facts": self._projector.CompactInputFacts(facts),
                "events": [],
            }
            for event in events or []:
                self._AppendEventLocked(runId, event)
            self._condition.notify_all()
            return runId

    def UpdateRun(self, runId: str, **updates: JsonValue) -> None:
        with self._condition:
            run = self._runs.setdefault(runId, {"events": []})
            if isinstance(updates.get("facts"), Mapping):
                updates["facts"] = self._projector.CompactInputFacts(updates["facts"])
            run.update(updates)
            self._condition.notify_all()

    def FindActiveRun(
        self,
        *,
        query: str,
        facts: JsonMapping,
    ) -> str | None:
        requestSignature = self._BuildRequestSignature(query, facts)
        with self._condition:
            return self._FindActiveRunBySignatureLocked(requestSignature)

    def AppendEvent(self, runId: str, event: JsonMapping) -> None:
        with self._condition:
            self._AppendEventLocked(runId, event)
            self._condition.notify_all()

    def ReadSnapshot(self, runId: str) -> JsonObject | None:
        with self._condition:
            run = self._runs.get(runId)
            if run is None:
                return None
            return self._SnapshotCopyLocked(run)

    def ReadSnapshotByIdentifier(self, identifier: str) -> tuple[str, JsonObject] | None:
        with self._condition:
            if identifier in self._runs:
                return identifier, self._SnapshotCopyLocked(self._runs[identifier])
            for jobId, run in self._runs.items():
                resultData = self._ResultData(run)
                if str(resultData.get("run_id") or "") == identifier:
                    return jobId, self._SnapshotCopyLocked(run)
        return None

    def BuildUiResult(self, runId: str) -> JsonObject:
        snapshot = self.ReadSnapshot(runId)
        if snapshot is None:
            return {}
        return self._projector.BuildUiResult(snapshot, runId)

    def RestoreRun(self, runId: str, snapshot: JsonMapping) -> None:
        """Restore a durable run snapshot after a server restart."""
        result = snapshot.get("result")
        if not isinstance(result, Mapping):
            return
        status = str(snapshot.get("status") or "")
        if status not in {"awaiting_input", "completed", "failed"}:
            status = (
                "awaiting_input"
                if _RequiresUserInput(result)
                else "failed"
                if _ClassificationNeedsMoreFacts(result)
                else "completed"
            )
        with self._condition:
            self._runs[runId] = {
                "status": status,
                "query": str(snapshot.get("query") or ""),
                "facts": self._projector.CompactInputFacts(
                    snapshot.get("facts") if isinstance(snapshot.get("facts"), Mapping) else {},
                ),
                "request_signature": self._BuildRequestSignature(
                    str(snapshot.get("query") or ""),
                    snapshot.get("facts") if isinstance(snapshot.get("facts"), Mapping) else {},
                ),
                "include_celex_excerpt": bool(
                    snapshot.get("include_celex_excerpt")
                ),
                "events": list(snapshot.get("events") or []),
                "result": dict(result),
                "partial_result": dict(result),
                "document_packages": list(snapshot.get("document_packages") or []),
            }
            self._condition.notify_all()

    def PersistRun(self, runId: str) -> None:
        snapshot = self.ReadSnapshot(runId)
        if snapshot is None:
            return
        result = snapshot.get("result")
        if not isinstance(result, Mapping):
            return
        runDir = Path(str(result.get("run_dir") or ""))
        if not runDir.is_dir():
            return
        target = runDir / "api_snapshot.json"
        temporary = target.with_suffix(".tmp")
        payload = {
            "status": snapshot.get("status") or "completed",
            "query": snapshot.get("query") or "",
            "facts": snapshot.get("facts") or {},
            "include_celex_excerpt": bool(
                snapshot.get("include_celex_excerpt")
            ),
            "events": snapshot.get("events") or [],
            "result": dict(result),
        }
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        except OSError:
            temporary.unlink(missing_ok=True)

    def BuildDocumentPackageCollection(self, runId: str) -> JsonObject:
        snapshotEntry = self.ReadSnapshotByIdentifier(runId)
        if snapshotEntry is None:
            return {}
        jobId, snapshot = snapshotEntry
        resultData = self._ResultData(snapshot)
        packages = self._DocumentPackagesFromSnapshot(snapshot, resultData)
        return DocumentPackageCollectionResponse(
            job_id=jobId,
            run_id=resultData.get("run_id"),
            total=len(packages),
            packages=packages,
        ).ToDict()

    def BuildDocumentPackageDetail(
        self,
        runId: str,
        packageId: str,
    ) -> JsonObject:
        snapshotEntry = self.ReadSnapshotByIdentifier(runId)
        if snapshotEntry is None:
            return {}
        jobId, snapshot = snapshotEntry
        resultData = self._ResultData(snapshot)
        packages = self._DetailedDocumentPackagesFromSnapshot(snapshot, resultData)
        for package in packages:
            if packageId in {
                str(package.get("document_package_id") or ""),
                str(package.get("taric10") or ""),
            }:
                package = self._EnrichDocumentPackageForDetail(package)
                return DocumentPackageDetailResponse(
                    job_id=jobId,
                    run_id=resultData.get("run_id"),
                    document_package=package,
                ).ToDict()
        return {}

    def _EnrichDocumentPackageForDetail(self, package: JsonMapping) -> JsonObject:
        publicPackage = dict(package)
        if "requirements" in publicPackage:
            return publicPackage
        taric10 = str(publicPackage.get("taric10") or "").strip()
        if not taric10:
            return publicPackage

        try:
            productFacts = publicPackage.get("product_facts") or {}
            rebuilt = asdict(BuildDocumentPackage(
                taric10,
                product_facts=(
                    dict(productFacts)
                    if isinstance(productFacts, Mapping)
                    else {}
                ),
            ))
        except Exception as exc:  # noqa: BLE001
            summary = dict(publicPackage.get("checklist_summary") or {})
            summary["a2m_enrichment_error"] = str(exc)
            publicPackage["checklist_summary"] = summary
            return publicPackage

        preserved = {
            key: publicPackage.get(key)
            for key in (
                "object_type",
                "created_by",
                "created_at",
                "document_package_id",
                "candidate_id",
                "taric10_branch",
                "taric10_branch_index",
                "taric10_branch_count",
                "taric10_resolution_mode",
                "taric10_is_recommended",
            )
            if key in publicPackage
        }
        publicPackage.update({
            key: value
            for key, value in rebuilt.items()
            if key not in {"object_type", "created_by", "created_at", "document_package_id", "candidate_id"}
        })
        publicPackage.update(preserved)
        summary = dict(publicPackage.get("checklist_summary") or {})
        summary["a2m_guidelines_count"] = len(publicPackage.get("a2m_guidelines") or [])
        publicPackage["checklist_summary"] = summary
        return publicPackage

    def _DocumentPackagesFromSnapshot(
        self,
        snapshot: JsonMapping,
        resultData: JsonMapping,
    ) -> list[JsonObject]:
        packages = self._projector.ExtractDocumentPackages(resultData)
        if packages:
            return packages
        snapshotPackages = snapshot.get("document_packages")
        if not isinstance(snapshotPackages, list):
            return []
        return self._projector.ExtractDocumentPackages({
            "document_packages": snapshotPackages,
        })

    def _DetailedDocumentPackagesFromSnapshot(
        self,
        snapshot: JsonMapping,
        resultData: JsonMapping,
    ) -> list[JsonObject]:
        snapshotPackages = snapshot.get("document_packages")
        if isinstance(snapshotPackages, list) and snapshotPackages:
            return [
                self._projector.PublicDocumentPackage(package)
                for package in snapshotPackages
                if isinstance(package, Mapping)
            ]
        return self._DocumentPackagesFromSnapshot(snapshot, resultData)

    def BuildPipelineResultProjection(
        self,
        pipelineOutput: JsonMapping,
    ) -> JsonObject:
        return self._projector.BuildPipelineResultProjection(pipelineOutput)

    def StreamEvents(
        self,
        runId: str,
        *,
        startIndex: int = 0,
        heartbeatSeconds: float = 15.0,
    ) -> Iterator[str]:
        eventIndex = max(0, startIndex)
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: runId not in self._runs
                    or self._HasPendingEvent(runId, eventIndex)
                    or self._IsTerminal(runId),
                    timeout=heartbeatSeconds,
                )
                run = self._runs.get(runId)
                runMissing = run is None
                events = [] if runMissing else list(run.get("events") or [])
                status = "" if runMissing else str(run.get("status") or "")

            if runMissing:
                yield self._FormatSse(
                    "error",
                    RunNotFoundSsePayload(
                        message="No run exists for the requested run_id.",
                        run_id=runId,
                    ).ToDict(),
                )
                return

            while eventIndex < len(events):
                yield self._FormatSse(
                    "pipeline_event",
                    PipelineEventPayload.model_validate(events[eventIndex]).ToDict(),
                    eventId=str(eventIndex),
                )
                eventIndex += 1

            if status in {"completed", "failed"} and eventIndex >= len(events):
                yield self._FormatSse(
                    "run_complete",
                    RunCompleteSsePayload(
                        run_id=runId,
                        status=status,
                    ).ToDict(),
                )
                return
            if status == "awaiting_input" and eventIndex >= len(events):
                yield self._FormatSse(
                    "run_paused",
                    RunPausedSsePayload(
                        run_id=runId,
                        status="awaiting_input",
                    ).ToDict(),
                )
                return

            yield ": heartbeat\n\n"

    def _AppendEventLocked(self, runId: str, event: JsonMapping) -> None:
        run = self._runs.setdefault(runId, {"events": []})
        eventData = self._projector.CompactEvent(event)
        eventData.setdefault("ts", time.strftime("%H:%M:%S"))
        run.setdefault("events", []).append(eventData)
        partialResult = eventData.get("partial_result")
        if isinstance(partialResult, dict):
            documentPackages = partialResult.pop("document_packages", None)
            if isinstance(documentPackages, list):
                run["document_packages"] = documentPackages
            run["partial_result"] = partialResult

    def _SnapshotCopyLocked(self, run: JsonMapping) -> JsonObject:
        snapshot = dict(run)
        snapshot["events"] = list(run.get("events") or [])
        partialResult = run.get("partial_result")
        if isinstance(partialResult, dict):
            snapshot["partial_result"] = dict(partialResult)
        result = run.get("result")
        if isinstance(result, dict):
            snapshot["result"] = dict(result)
        return snapshot

    def _ResultData(self, snapshot: JsonMapping) -> JsonObject:
        result = snapshot.get("result")
        if isinstance(result, Mapping):
            return dict(result)
        partialResult = snapshot.get("partial_result")
        if isinstance(partialResult, Mapping):
            return dict(partialResult)
        return {}

    def _HasPendingEvent(self, runId: str, eventIndex: int) -> bool:
        run = self._runs.get(runId)
        return bool(run is not None and len(run.get("events") or []) > eventIndex)

    def _IsTerminal(self, runId: str) -> bool:
        run = self._runs.get(runId)
        return bool(
            run is not None
            and run.get("status") in {"awaiting_input", "completed", "failed"}
        )

    def _BuildRequestSignature(self, query: str, facts: JsonMapping) -> str:
        payload = {
            "query": query,
            "facts": dict(facts),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _FindActiveRunBySignatureLocked(self, requestSignature: str) -> str | None:
        for runId, run in self._runs.items():
            if run.get("status") not in {"queued", "running", "awaiting_input"}:
                continue
            if run.get("request_signature") == requestSignature:
                return runId
        return None

    def _FormatSse(
        self,
        eventName: str,
        payload: JsonMapping,
        eventId: str | None = None,
    ) -> str:
        eventIdLine = "id: {0}\n".format(eventId) if eventId is not None else ""
        return "{0}event: {1}\ndata: {2}\n\n".format(
            eventIdLine,
            eventName,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )


class PipelineRunService:
    """Pipeline execution facade for UI/API layers."""

    def __init__(
        self,
        registry: RunRegistry,
        pipelineCallable: PipelineCallable,
        runExecutor: PipelineRunExecutor | None = None,
    ) -> None:
        self._registry = registry
        self._pipelineCallable = pipelineCallable
        self._runExecutor = runExecutor
        self._answerLock = threading.Lock()

    def StartBackgroundRun(
        self,
        runId: str,
        request: PipelineRunRequest,
    ) -> None:
        if self._runExecutor is None:
            raise RuntimeError("PipelineRunExecutor is required for background runs")
        try:
            self._runExecutor.Submit(
                runId,
                lambda: self.Run(runId, request),
            )
        except PipelineRunCapacityError:
            self._registry.UpdateRun(
                runId,
                status="failed",
                finished_at=time.time(),
                error="pipeline_capacity_exhausted",
            )
            self._registry.AppendEvent(runId, {
                "stage": "Pipeline",
                "status": "failed",
                "message": "Pipeline execution capacity is full.",
            })
            raise

    def Run(self, runId: str, request: PipelineRunRequest) -> None:
        self._registry.UpdateRun(
            runId,
            status="running",
            started_at=time.time(),
            query=request.query,
            facts=dict(request.facts),
            include_celex_excerpt=request.includeCelexExcerpt,
        )
        try:
            pipelineOutput = self._pipelineCallable(
                query=request.query,
                facts=dict(request.facts),
                include_celex_excerpt=request.includeCelexExcerpt,
                job_id=runId,
                progress_callback=lambda event: self._registry.AppendEvent(
                    runId,
                    event,
                ),
            )
            pipelineOutput = self._StripRuntimeObjects(pipelineOutput)
            blackboard = pipelineOutput.get("blackboard") or {}
            documentPackages = (
                blackboard.get("document_packages") or []
                if isinstance(blackboard, Mapping)
                else []
            )
            result = self._registry.BuildPipelineResultProjection(pipelineOutput)
            candidateCodeSet = pipelineOutput.get("candidate_code_set")
            needsMoreFacts = _ClassificationNeedsMoreFacts(pipelineOutput)
            classificationFailed = _ClassificationFailed(pipelineOutput)
            status = (
                "awaiting_input"
                if _RequiresUserInput(pipelineOutput)
                else "failed"
                if needsMoreFacts or classificationFailed
                else "completed"
            )
            failureReason = (
                str(candidateCodeSet.get("failure_reason") or "classification unresolved")
                if (needsMoreFacts or classificationFailed)
                and isinstance(candidateCodeSet, Mapping)
                else ""
            )
            self._registry.UpdateRun(
                runId,
                status=status,
                finished_at=time.time(),
                result=result,
                partial_result=result,
                document_packages=documentPackages,
                error=failureReason if status == "failed" else None,
            )
            self._registry.AppendEvent(
                runId,
                {
                    "stage": (
                        "Classification"
                        if needsMoreFacts or classificationFailed
                        else "Pipeline"
                    ),
                    "status": status,
                    "message": (
                        "분류를 계속하려면 사용자 응답이 필요합니다."
                        if status == "awaiting_input"
                        else failureReason
                        if status == "failed"
                        else "전체 파이프라인 완료"
                    ),
                    "run_id": result.get("run_id"),
                    "partial_result": result,
                },
            )
            self._registry.PersistRun(runId)
        except Exception as exc:  # noqa: BLE001
            self._registry.UpdateRun(
                runId,
                status="failed",
                finished_at=time.time(),
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            self._registry.AppendEvent(
                runId,
                {
                    "stage": "Pipeline",
                    "status": "failed",
                    "message": str(exc),
                },
            )

    def ReclassifyWithQuestionAnswers(
        self,
        runId: str,
        runDirectory: Path,
        answers: list[JsonObject],
    ) -> JsonObject:
        """Persist explicit answers and resume classification and document steps."""
        from bussiness_logic.classification.components.classification import (
            ClassificationComponent,
        )
        from bussiness_logic.classification.rules.question_contract import (
            QUESTION_CONTRACT_VERSION,
        )
        from bussiness_logic.document.pipeline.document_recommendation_pipeline import (
            DocumentRecommendationPipeline,
        )
        from bussiness_logic.pipeline.blackboard import BlackboardStore, now_iso
        from bussiness_logic.pipeline.pipeline_context import PipelineContext

        # ponytail: 데모 처리량에서는 전역 직렬화로 파일 갱신을 보호한다.
        # 동시 답변 재실행이 병목이 되면 job별 worker/lock으로 교체한다.
        with self._answerLock:
            blackboardPath = runDirectory / "blackboard.json"
            if not blackboardPath.is_file():
                raise FileNotFoundError(f"blackboard not found: {blackboardPath}")
            blackboard = json.loads(blackboardPath.read_text(encoding="utf-8"))
            if not isinstance(blackboard, dict):
                raise ValueError("blackboard must be a JSON object")
            runContext = blackboard.get("run_context") or {}
            internalRunId = str(runContext.get("run_id") or runId)
            store = BlackboardStore(internalRunId, run_dir=runDirectory)
            questions = [
                item
                for item in (blackboard.get("user_questions") or [])
                if isinstance(item, dict)
            ]
            questionsById = {
                str(item.get("user_question_id") or ""): item
                for item in questions
                if str(item.get("user_question_id") or "")
            }
            answerFacts = blackboard.setdefault("classification_answer_facts", [])
            if not isinstance(answerFacts, list):
                raise ValueError("classification_answer_facts must be a list")
            questionIds = [
                str(payload.get("user_question_id") or "")
                for payload in answers
            ]
            if len(questionIds) != len(set(questionIds)):
                raise ValueError("duplicate user_question_id in answer payload")
            snapshot = self._registry.ReadSnapshot(runId) or {}

            answeredAt = now_iso()
            acceptedIds: list[str] = []
            for payload in answers:
                questionId = str(payload.get("user_question_id") or "")
                answer = str(payload.get("answer") or "").strip().lower()
                if answer not in {"yes", "no", "unknown"}:
                    raise ValueError(f"invalid answer for {questionId}")
                question = questionsById.get(questionId)
                if question is None:
                    raise KeyError(f"unknown user_question_id: {questionId}")
                questionKey = str(question.get("question_key") or "")
                predicateOp = str(question.get("predicate_op") or "")
                if (
                    int(question.get("contract_version") or 0)
                    != QUESTION_CONTRACT_VERSION
                    or not questionKey
                    or not predicateOp
                ):
                    raise ValueError(
                        f"question {questionId} predates contract V2; rerun classification"
                    )
                previousFact = next((
                    item
                    for item in reversed(answerFacts)
                    if isinstance(item, Mapping)
                    and str(item.get("user_question_id") or "") == questionId
                ), None)
                previousAnswer = str(
                    question.get("answer")
                    or (previousFact or {}).get("answer")
                    or ""
                )
                if previousAnswer == answer:
                    if previousFact:
                        question["answer_id"] = previousFact.get("answer_id")
                        question["answered_at"] = previousFact.get("answered_at")
                    continue
                if snapshot.get("status") != "awaiting_input":
                    raise ValueError(
                        f"run {runId} is not awaiting classification answers"
                    )
                if not bool(question.get("active")):
                    raise ValueError(f"question {questionId} is no longer active")
                if previousAnswer not in {"", "unknown"}:
                    raise ValueError(
                        f"question {questionId} already has a different answer"
                    )
                answerId = store.next_id("qa")
                answerFact = {
                    "object_type": "ClassificationAnswerFact",
                    "created_by": "User_Interaction_Component",
                    "created_at": answeredAt,
                    "answer_id": answerId,
                    "user_question_id": questionId,
                    "question_key": questionKey,
                    "contract_version": QUESTION_CONTRACT_VERSION,
                    "answer": answer,
                    "answered_at": answeredAt,
                    "source": "user",
                    "stage": str(question.get("stage") or ""),
                    "parent_code": str(question.get("parent_code") or ""),
                    "candidate_code": str(question.get("candidate_code") or ""),
                    "axis": str(question.get("axis") or ""),
                    "predicate_op": predicateOp,
                    "canonical_field": str(question.get("canonical_field") or ""),
                    "condition_value": str(question.get("condition_value") or ""),
                    "context_scope": str(question.get("context_scope") or ""),
                }
                store.ValidateWrite("classification_answer_facts", answerFact)
                answerFacts.append(answerFact)
                question["answer"] = answer
                question["answered_at"] = answeredAt
                question["answer_id"] = answerId
                acceptedIds.append(answerId)

            store.save(blackboard)
            if (
                not acceptedIds
                and snapshot.get("status") != "awaiting_input"
            ):
                return self._registry.BuildUiResult(runId)
            context = PipelineContext(
                query=str(snapshot.get("query") or ""),
                facts=dict(snapshot.get("facts") or {}),
                store=store,
                includeCelexExcerpt=bool(
                    snapshot.get("include_celex_excerpt")
                ),
                progressCallback=lambda event: self._registry.AppendEvent(
                    runId,
                    event,
                ),
            )
            self._registry.UpdateRun(runId, status="running")
            componentResult = context.ExecuteComponent(ClassificationComponent())
            if not componentResult.success:
                failureReason = (
                    componentResult.error or "classification replay failed"
                )
                self._registry.UpdateRun(
                    runId,
                    status="failed",
                    finished_at=time.time(),
                    error=failureReason,
                )
                self._registry.PersistRun(runId)
                raise RuntimeError(failureReason)

            replayedBlackboard = store.load()
            candidateSets = replayedBlackboard.get("candidate_code_sets") or []
            latestCandidateSet = candidateSets[-1] if candidateSets else {}
            needsMoreFacts = (
                isinstance(latestCandidateSet, dict)
                and latestCandidateSet.get("classification_status")
                == "needs_more_facts"
            )
            pendingQuestions = (
                latestCandidateSet.get("resolver_debug", {}).get(
                    "pending_user_questions",
                    [],
                )
                if isinstance(latestCandidateSet, dict)
                else []
            )
            if not pendingQuestions and isinstance(latestCandidateSet, dict):
                pendingQuestions = (
                    latestCandidateSet.get("resolver_debug", {})
                    .get("unresolved", {})
                    .get("question_options", [])
                )
            activeQuestionKeys = {
                str(item.get("question_key") or "")
                for item in pendingQuestions
                if isinstance(item, dict) and str(item.get("question_key") or "")
            }
            isAwaitingInput = needsMoreFacts and bool(activeQuestionKeys)
            processingError = ""
            for question in replayedBlackboard.get("user_questions") or []:
                if not isinstance(question, dict):
                    continue
                isActive = str(question.get("question_key") or "") in activeQuestionKeys
                question["active"] = isActive
                question["resolved_at"] = None if isActive else answeredAt
            store.save(replayedBlackboard)
            if needsMoreFacts and not isAwaitingInput:
                processingError = str(
                    latestCandidateSet.get("failure_reason")
                    or "classification unresolved without an actionable question"
                )
            elif not isAwaitingInput:
                DocumentRecommendationPipeline().Run(context)
                if context.shouldStop:
                    processingError = str(
                        context.componentResults[-1].error
                        if context.componentResults
                        else "document recommendation replay failed"
                    )
                replayedBlackboard = store.load()
            pipelineOutput = self._StripRuntimeObjects(context.BuildFinalResult())
            result = self._registry.BuildPipelineResultProjection(pipelineOutput)
            documentPackages = replayedBlackboard.get("document_packages") or []
            status = (
                "awaiting_input"
                if isAwaitingInput
                else "failed"
                if processingError
                else "completed"
            )
            self._registry.UpdateRun(
                runId,
                status=status,
                finished_at=time.time(),
                result=result,
                partial_result=result,
                document_packages=documentPackages,
                error=processingError or None,
            )
            self._registry.AppendEvent(
                runId,
                {
                    "stage": (
                        "Classification_Answer"
                        if isAwaitingInput
                        else "Classification"
                        if needsMoreFacts
                        else "Document_Component"
                        if processingError
                        else "Pipeline"
                    ),
                    "status": status,
                    "message": (
                        "추가 사용자 응답이 필요합니다."
                        if isAwaitingInput
                        else processingError
                        if processingError
                        else "사용자 답변을 반영해 분류와 문서 단계를 완료했습니다."
                    ),
                    "answer_ids": acceptedIds,
                    "partial_result": result,
                },
            )
            self._registry.PersistRun(runId)
            return self._registry.BuildUiResult(runId)

    def _StripRuntimeObjects(self, pipelineOutput: Mapping[str, object]) -> JsonObject:
        return {
            key: value
            for key, value in dict(pipelineOutput).items()
            if key != "store"
        }
