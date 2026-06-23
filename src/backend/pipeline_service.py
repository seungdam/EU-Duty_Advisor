"""UI-facing pipeline service and run registry."""

from __future__ import annotations

import json
import hashlib
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


PipelineCallable = Callable[..., dict[str, Any]]


class PipelineRunRequest(BaseModel):
    """Pipeline 실행 요청 DTO."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    query: str
    facts: dict[str, Any] = Field(default_factory=dict)
    includeCelexExcerpt: bool = Field(
        default=False,
        alias="include_celex_excerpt",
    )


class PipelineRunResult(BaseModel):
    """UI가 기본으로 소비하는 pipeline 결과 DTO."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    runId: str = Field(alias="run_id")
    runDir: str = Field(default="", alias="run_dir")
    auditRef: dict[str, Any] = Field(default_factory=dict, alias="audit_ref")
    candidateCodeSet: Optional[dict[str, Any]] = Field(
        default=None,
        alias="candidate_code_set",
    )
    documentPackage: Optional[dict[str, Any]] = Field(
        default=None,
        alias="document_package",
    )
    decision: Optional[dict[str, Any]] = None
    agentResults: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="agent_results",
    )
    userQuestions: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="user_questions",
    )

    @classmethod
    def FromPipelineOutput(
        cls,
        pipelineOutput: Mapping[str, Any],
    ) -> "PipelineRunResult":
        runId = str(pipelineOutput.get("run_id") or "")
        runDir = str(pipelineOutput.get("run_dir") or "")
        blackboard = pipelineOutput.get("blackboard") or {}
        decision = pipelineOutput.get("decision")
        documentPackage = pipelineOutput.get("document_package")
        userQuestionIds = set()
        if isinstance(decision, Mapping):
            userQuestionIds = set(decision.get("user_questions") or [])
        userQuestions = [
            dict(question)
            for question in blackboard.get("user_questions", [])
            if isinstance(question, Mapping)
            and question.get("status") == "open"
            and (
                not userQuestionIds
                or question.get("question_id") in userQuestionIds
            )
        ]
        return cls(
            run_id=runId,
            run_dir=runDir,
            audit_ref={
                "run_id": runId,
                "run_dir": runDir,
                "blackboard_available": bool(pipelineOutput.get("blackboard")),
                "agent_run_count": len(pipelineOutput.get("agent_runs") or []),
            },
            candidate_code_set=pipelineOutput.get("candidate_code_set"),
            document_package=(
                {
                    key: value
                    for key, value in documentPackage.items()
                    if key != "raw_document_package"
                }
                if isinstance(documentPackage, Mapping)
                else documentPackage
            ),
            decision=decision,
            agent_results=list(pipelineOutput.get("agent_results") or []),
            user_questions=userQuestions,
        )

    def ToUiDict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class RunRegistry:
    """Thread-safe run state and event buffer."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._condition = threading.Condition(threading.Lock())

    def CreateRun(
        self,
        runId: str,
        *,
        query: str,
        facts: Mapping[str, Any],
        status: str = "queued",
        events: list[dict[str, Any]] | None = None,
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
                "facts": self._CompactInputFacts(facts),
                "events": [],
            }
            for event in events or []:
                self._AppendEventLocked(runId, event)
            self._condition.notify_all()
            return runId

    def UpdateRun(self, runId: str, **updates: Any) -> None:
        with self._condition:
            run = self._runs.setdefault(runId, {"events": []})
            if isinstance(updates.get("facts"), Mapping):
                updates["facts"] = self._CompactInputFacts(updates["facts"])
            run.update(updates)
            self._condition.notify_all()

    def FindActiveRun(
        self,
        *,
        query: str,
        facts: Mapping[str, Any],
    ) -> str | None:
        requestSignature = self._BuildRequestSignature(query, facts)
        with self._condition:
            return self._FindActiveRunBySignatureLocked(requestSignature)

    def AppendEvent(self, runId: str, event: Mapping[str, Any]) -> None:
        with self._condition:
            self._AppendEventLocked(runId, event)
            self._condition.notify_all()

    def ReadSnapshot(self, runId: str) -> dict[str, Any] | None:
        with self._condition:
            run = self._runs.get(runId)
            if run is None:
                return None
            snapshot = dict(run)
            snapshot["events"] = list(run.get("events") or [])
            partialResult = run.get("partial_result")
            if isinstance(partialResult, dict):
                snapshot["partial_result"] = dict(partialResult)
            result = run.get("result")
            if isinstance(result, dict):
                snapshot["result"] = dict(result)
            return snapshot

    def BuildUiResult(self, runId: str) -> dict[str, Any]:
        snapshot = self.ReadSnapshot(runId)
        if snapshot is None:
            return {}
        baseResult = snapshot.get("result") or snapshot.get("partial_result") or {}
        resultData = self._CompactSnapshotResult(baseResult)
        resultData["job_id"] = runId
        resultData["job_status"] = snapshot.get("status")
        resultData["events"] = snapshot.get("events") or []
        resultData["request"] = self._BuildRequestView(snapshot)
        inputProcessingView = self._BuildSnapshotInputProcessingView(resultData)
        if inputProcessingView:
            resultData["input_processing_view"] = inputProcessingView
        if snapshot.get("error"):
            resultData["error"] = snapshot.get("error")
            resultData["traceback"] = snapshot.get("traceback")
        return resultData

    def BuildPipelineResultProjection(
        self,
        pipelineOutput: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = PipelineRunResult.FromPipelineOutput(pipelineOutput).ToUiDict()
        result.update(self._CompactPipelineResult(pipelineOutput))
        return result

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
                    {
                        "error": "run_not_found",
                        "message": "No run exists for the requested run_id.",
                        "run_id": runId,
                    },
                )
                return

            while eventIndex < len(events):
                yield self._FormatSse(
                    "pipeline_event",
                    events[eventIndex],
                    eventId=str(eventIndex),
                )
                eventIndex += 1

            if status in {"completed", "failed"} and eventIndex >= len(events):
                yield self._FormatSse(
                    "run_complete",
                    {"run_id": runId, "status": status},
                )
                return

            yield ": heartbeat\n\n"

    def _AppendEventLocked(self, runId: str, event: Mapping[str, Any]) -> None:
        run = self._runs.setdefault(runId, {"events": []})
        eventData = self._CompactEvent(event)
        eventData.setdefault("ts", time.strftime("%H:%M:%S"))
        run.setdefault("events", []).append(eventData)
        partialResult = eventData.get("partial_result")
        if isinstance(partialResult, dict):
            run["partial_result"] = partialResult

    def _HasPendingEvent(self, runId: str, eventIndex: int) -> bool:
        run = self._runs.get(runId)
        return bool(run is not None and len(run.get("events") or []) > eventIndex)

    def _IsTerminal(self, runId: str) -> bool:
        run = self._runs.get(runId)
        return bool(run is not None and run.get("status") in {"completed", "failed"})

    def _BuildRequestSignature(self, query: str, facts: Mapping[str, Any]) -> str:
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
            if run.get("status") not in {"queued", "running"}:
                continue
            if run.get("request_signature") == requestSignature:
                return runId
        return None

    def _CompactEvent(self, event: Mapping[str, Any]) -> dict[str, Any]:
        eventData = dict(event)
        rawInput = eventData.pop("raw_input", None)
        inputProcessingView = (
            self._BuildInputProcessingViewFromFacts(rawInput)
            if isinstance(rawInput, Mapping)
            else {}
        )
        if isinstance(rawInput, Mapping):
            eventData["collected_input_summary"] = self._CompactInputFacts(rawInput)
        partialResult = eventData.get("partial_result")
        if isinstance(partialResult, Mapping):
            compactPartialResult = self._CompactPipelineResult(partialResult)
            if inputProcessingView and "input_processing_view" not in compactPartialResult:
                compactPartialResult["input_processing_view"] = inputProcessingView
            eventData["partial_result"] = compactPartialResult
        elif inputProcessingView:
            eventData["partial_result"] = {
                "input_processing_view": inputProcessingView,
            }
        return eventData

    def _CompactSnapshotResult(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            return {}
        allowedKeys = {
            "run_id",
            "run_dir",
            "audit_ref",
            "candidate_code_set",
            "document_package",
            "decision",
            "agent_results",
            "user_questions",
            "input_processing_summary",
            "input_processing_view",
        }
        return {
            key: value
            for key, value in result.items()
            if key in allowedKeys
        }

    def _BuildRequestView(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        facts = snapshot.get("facts")
        return {
            "query": str(snapshot.get("query") or ""),
            "facts": dict(facts) if isinstance(facts, Mapping) else {},
        }

    def _CompactInputFacts(self, rawInput: Mapping[str, Any]) -> dict[str, Any]:
        compact = {
            key: value
            for key, value in rawInput.items()
            if key
            not in {
                "ocr_text",
                "ingredient_list",
                "classification_input_fact_texts",
                "classification_input_product_facts",
                "unresolved_product_facts",
                "product_fact_conflicts",
                "input_reconstruction",
            }
        }
        for textListKey in (
            "ocr_text",
            "ingredient_list",
            "classification_input_fact_texts",
        ):
            if textListKey not in rawInput:
                continue
            textList = rawInput.get(textListKey) or []
            if isinstance(textList, list):
                compact[f"{textListKey}_count"] = len(textList)
                compact[f"{textListKey}_char_count"] = sum(
                    len(str(item)) for item in textList
                )
                if textListKey in {
                    "classification_input_fact_texts",
                }:
                    compact[textListKey] = [
                        str(item)[:500] for item in textList[:24]
                    ]

        for recordListKey in (
            "classification_input_product_facts",
            "unresolved_product_facts",
            "product_fact_conflicts",
        ):
            recordList = rawInput.get(recordListKey)
            if isinstance(recordList, list):
                compact[f"{recordListKey}_count"] = len(recordList)

        composition = rawInput.get("composition")
        if isinstance(composition, list) and len(composition) > 24:
            compact["composition"] = composition[:24]
            compact["composition_count"] = len(composition)
        inputReconstruction = rawInput.get("input_reconstruction")
        if isinstance(inputReconstruction, Mapping):
            compact["input_reconstruction_available"] = True
            compact["input_reconstruction_mode"] = inputReconstruction.get("mode")
            compact["input_reconstruction_error"] = inputReconstruction.get("error")
            compact["reconstructed_table_count"] = (
                inputReconstruction.get("reconstructed_table_count")
                or len(inputReconstruction.get("reconstructed_tables") or [])
            )
            compact["reconstructed_fact_count"] = (
                inputReconstruction.get("fact_count")
                or len(inputReconstruction.get("classification_input_product_facts") or [])
            )
        return compact

    def _CompactPipelineResult(self, pipelineResult: Mapping[str, Any]) -> dict[str, Any]:
        blackboard = pipelineResult.get("blackboard")
        compact = {
            key: value
            for key, value in pipelineResult.items()
            if key
            not in {
                "blackboard",
                "agent_runs",
                "raw_document_package",
                "events",
                "facts",
            }
        }
        if isinstance(blackboard, Mapping):
            inputProcessingSummary = self._BuildInputProcessingSummary(blackboard)
            if inputProcessingSummary:
                compact["input_processing_summary"] = inputProcessingSummary
            inputProcessingView = self._BuildInputProcessingViewFromBlackboard(blackboard)
            if inputProcessingView:
                compact["input_processing_view"] = inputProcessingView
            userQuestions = self._BuildOpenUserQuestions(
                blackboard,
                compact.get("decision"),
            )
            if userQuestions:
                compact["user_questions"] = userQuestions
        documentPackage = compact.get("document_package")
        if isinstance(documentPackage, Mapping):
            compact["document_package"] = {
                key: value
                for key, value in documentPackage.items()
                if key != "raw_document_package"
            }
        return compact

    def _BuildSnapshotInputProcessingView(
        self,
        resultData: Mapping[str, Any],
    ) -> dict[str, Any]:
        existingView = resultData.get("input_processing_view")
        if isinstance(existingView, Mapping):
            return self._CompactInputProcessingView(existingView)
        return {}

    def _BuildInputProcessingViewFromBlackboard(
        self,
        blackboard: Mapping[str, Any],
    ) -> dict[str, Any]:
        productEvidenceState = blackboard.get("product_evidence_state") or {}
        if not isinstance(productEvidenceState, Mapping):
            return {}
        observedFacts = productEvidenceState.get("observed_facts") or {}
        if not isinstance(observedFacts, Mapping):
            return {}
        return self._BuildInputProcessingViewFromFacts(observedFacts)

    def _BuildInputProcessingViewFromFacts(
        self,
        facts: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(facts, Mapping):
            return {}

        sourceUrls = facts.get("source_urls") or []
        if isinstance(sourceUrls, str):
            sourceUrls = [sourceUrls] if sourceUrls.strip() else []
        if not isinstance(sourceUrls, list):
            sourceUrls = []
        url = str(facts.get("url") or (sourceUrls[0] if sourceUrls else "") or "")
        inputReconstruction = facts.get("input_reconstruction") or {}
        if not isinstance(inputReconstruction, Mapping):
            inputReconstruction = {}
        hasBasicInfo = any(
            str(value).strip()
            for value in (
                facts.get("product_name"),
                facts.get("description"),
                url,
            )
        )
        hasReconstruction = any(
            inputReconstruction.get(key)
            for key in (
                "source_evidence_preview",
                "reconstructed_tables",
                "classification_input_product_facts",
                "classification_input_fact_texts",
            )
        )
        hasStructuredFacts = any(
            facts.get(key)
            for key in (
                "classification_input_product_facts",
                "classification_input_fact_texts",
            )
        )
        if not (hasBasicInfo or hasReconstruction or hasStructuredFacts):
            return {}

        inputProcessingView = {
            "page_product_facts": {
                "product_name": facts.get("product_name") or "",
                "description": facts.get("description") or "",
                "url": url,
                "source_urls": [str(item) for item in sourceUrls if str(item).strip()],
            },
            "detail_evidence_rows": inputReconstruction.get(
                "source_evidence_preview",
                [],
            ),
            "reconstructed_detail_tables": inputReconstruction.get(
                "reconstructed_tables",
                [],
            ),
            "classification_input_facts": (
                inputReconstruction.get("classification_input_product_facts")
                or facts.get("classification_input_product_facts")
                or []
            ),
            "classification_input_text_lines": (
                inputReconstruction.get("classification_input_fact_texts")
                or facts.get("classification_input_fact_texts")
                or []
            ),
            "unresolved_input_facts": (
                inputReconstruction.get("unresolved_product_facts")
                or inputReconstruction.get("unresolved_facts")
                or facts.get("unresolved_product_facts")
                or []
            ),
            "input_fact_conflicts": (
                inputReconstruction.get("product_fact_conflicts")
                or inputReconstruction.get("conflicts")
                or facts.get("product_fact_conflicts")
                or []
            ),
            "evidence_source_labels": inputReconstruction.get("source_ref_labels") or {},
            "reconstruction_status": {
                "mode": inputReconstruction.get("mode") or "",
                "used_llm_reconstruction": bool(
                    inputReconstruction.get("used_llm_reconstruction"),
                ),
                "fallback_reason": inputReconstruction.get("fallback_reason") or "",
                "error": inputReconstruction.get("error") or "",
                "detail_table_count": (
                    inputReconstruction.get("reconstructed_table_count")
                    or len(inputReconstruction.get("reconstructed_tables") or [])
                ),
                "classification_fact_count": (
                    inputReconstruction.get("fact_count")
                    or len(inputReconstruction.get("classification_input_product_facts") or [])
                ),
                "classification_text_line_count": (
                    inputReconstruction.get("fact_text_count")
                    or len(inputReconstruction.get("classification_input_fact_texts") or [])
                ),
            },
        }
        return self._CompactInputProcessingView(inputProcessingView)

    def _CompactInputProcessingView(
        self,
        inputProcessingView: Mapping[str, Any],
    ) -> dict[str, Any]:
        basicInfo = inputProcessingView.get("page_product_facts") or {}
        if not isinstance(basicInfo, Mapping):
            basicInfo = {}
        reconstruction = inputProcessingView.get("reconstruction_status") or {}
        if not isinstance(reconstruction, Mapping):
            reconstruction = {}

        return {
            "page_product_facts": {
                "product_name": str(basicInfo.get("product_name") or ""),
                "description": str(basicInfo.get("description") or "")[:1000],
                "url": str(basicInfo.get("url") or ""),
                "source_urls": self._CompactTextList(
                    basicInfo.get("source_urls"),
                    limit=8,
                    textLimit=700,
                ),
            },
            "detail_evidence_rows": self._CompactMappingList(
                inputProcessingView.get("detail_evidence_rows"),
                limit=12,
                textLimit=700,
            ),
            "reconstructed_detail_tables": self._CompactReconstructedTables(
                inputProcessingView.get("reconstructed_detail_tables"),
            ),
            "classification_input_facts": self._CompactMappingList(
                inputProcessingView.get("classification_input_facts"),
                limit=80,
                textLimit=700,
            ),
            "classification_input_text_lines": self._CompactTextList(
                inputProcessingView.get("classification_input_text_lines"),
                limit=80,
                textLimit=700,
            ),
            "unresolved_input_facts": self._CompactMappingList(
                inputProcessingView.get("unresolved_input_facts"),
                limit=40,
                textLimit=700,
            ),
            "input_fact_conflicts": self._CompactTextList(
                inputProcessingView.get("input_fact_conflicts"),
                limit=20,
                textLimit=700,
            ),
            "evidence_source_labels": self._CompactTextMapping(
                inputProcessingView.get("evidence_source_labels"),
                textLimit=200,
            ),
            "reconstruction_status": dict(reconstruction),
        }

    def _CompactTextList(
        self,
        records: Any,
        *,
        limit: int,
        textLimit: int,
    ) -> list[str]:
        if isinstance(records, str):
            records = [records]
        if not isinstance(records, list):
            return []
        return [
            str(item)[:textLimit]
            for item in records[:limit]
            if str(item).strip()
        ]

    def _CompactTextMapping(
        self,
        records: Any,
        *,
        textLimit: int,
    ) -> dict[str, str]:
        if not isinstance(records, Mapping):
            return {}
        return {
            str(key): str(value)[:textLimit]
            for key, value in records.items()
        }

    def _CompactMappingList(
        self,
        records: Any,
        *,
        limit: int,
        textLimit: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(records, list):
            return []
        return [
            {
                str(key): str(value)[:textLimit]
                if isinstance(value, str)
                else value
                for key, value in record.items()
            }
            for record in records[:limit]
            if isinstance(record, Mapping)
        ]

    def _CompactReconstructedTables(self, tables: Any) -> list[dict[str, Any]]:
        if not isinstance(tables, list):
            return []
        compactTables: list[dict[str, Any]] = []
        for table in tables[:8]:
            if not isinstance(table, Mapping):
                continue
            rows = table.get("rows") or []
            compactTable = {
                key: value
                for key, value in table.items()
                if key != "rows"
            }
            compactTable["rows"] = self._CompactMappingList(
                rows,
                limit=80,
                textLimit=700,
            )
            compactTables.append(compactTable)
        return compactTables

    def _BuildInputProcessingSummary(
        self,
        blackboard: Mapping[str, Any],
    ) -> dict[str, Any]:
        productEvidenceState = blackboard.get("product_evidence_state") or {}
        if not isinstance(productEvidenceState, Mapping):
            return {}
        observedFacts = productEvidenceState.get("observed_facts") or {}
        if not isinstance(observedFacts, Mapping):
            observedFacts = {}
        ocrText = observedFacts.get("ocr_text") or []
        composition = observedFacts.get("composition") or []
        inferredFacts = productEvidenceState.get("inferred_facts") or []
        summary = {
            "product_id": productEvidenceState.get("product_id"),
            "product_name": observedFacts.get("product_name"),
            "ocr_text_count": len(ocrText) if isinstance(ocrText, list) else 0,
            "composition_count": len(composition) if isinstance(composition, list) else 0,
            "inferred_fact_count": (
                len(inferredFacts) if isinstance(inferredFacts, list) else 0
            ),
            "unknowns": productEvidenceState.get("unknowns") or [],
        }
        inputReconstruction = observedFacts.get("input_reconstruction")
        if isinstance(inputReconstruction, Mapping):
            summary["input_reconstruction_available"] = True
            summary["input_reconstruction_mode"] = inputReconstruction.get("mode")
            summary["reconstructed_table_count"] = (
                inputReconstruction.get("reconstructed_table_count")
                or len(inputReconstruction.get("reconstructed_tables") or [])
            )
            summary["reconstructed_fact_count"] = (
                inputReconstruction.get("fact_count")
                or len(inputReconstruction.get("classification_input_product_facts") or [])
            )
        return summary

    def _BuildOpenUserQuestions(
        self,
        blackboard: Mapping[str, Any],
        decision: Any,
    ) -> list[dict[str, Any]]:
        userQuestionIds = set()
        if isinstance(decision, Mapping):
            userQuestionIds = set(decision.get("user_questions") or [])
        return [
            dict(question)
            for question in blackboard.get("user_questions", [])
            if isinstance(question, Mapping)
            and question.get("status") == "open"
            and (
                not userQuestionIds
                or question.get("question_id") in userQuestionIds
            )
        ]

    def _FormatSse(
        self,
        eventName: str,
        payload: Mapping[str, Any],
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
    ) -> None:
        self._registry = registry
        self._pipelineCallable = pipelineCallable

    def StartBackgroundRun(self, runId: str, request: PipelineRunRequest) -> None:
        thread = threading.Thread(
            target=self.Run,
            args=(runId, request),
            daemon=True,
        )
        thread.start()

    def Run(self, runId: str, request: PipelineRunRequest) -> None:
        self._registry.UpdateRun(
            runId,
            status="running",
            started_at=time.time(),
            query=request.query,
            facts=dict(request.facts),
        )
        try:
            pipelineOutput = self._pipelineCallable(
                query=request.query,
                facts=dict(request.facts),
                include_celex_excerpt=request.includeCelexExcerpt,
                progress_callback=lambda event: self._registry.AppendEvent(
                    runId,
                    event,
                ),
            )
            pipelineOutput = self._StripRuntimeObjects(pipelineOutput)
            result = self._registry.BuildPipelineResultProjection(pipelineOutput)
            self._registry.UpdateRun(
                runId,
                status="completed",
                finished_at=time.time(),
                result=result,
                partial_result=result,
            )
            self._registry.AppendEvent(
                runId,
                {
                    "stage": "Pipeline",
                    "status": "completed",
                    "message": "전체 파이프라인 완료",
                    "run_id": result.get("run_id"),
                    "partial_result": result,
                },
            )
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

    def _StripRuntimeObjects(self, pipelineOutput: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in dict(pipelineOutput).items()
            if key != "store"
        }
