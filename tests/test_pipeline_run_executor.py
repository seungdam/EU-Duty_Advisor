from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from backend.pipeline_api import PipelineApi
from backend.pipeline_executor import PipelineRunCapacityError, PipelineRunExecutor
from backend.pipeline_service import PipelineRunRequest, PipelineRunService, RunRegistry


def _WaitFor(predicate: Callable[[], bool], timeoutSeconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeoutSeconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def _CompletedOutput(runId: str, runDirectory: Path) -> dict[str, object]:
    candidateSet = {
        "candidate_set_id": f"ccs_{runId}",
        "classification_status": "resolved",
        "candidates": [],
    }
    return {
        "blackboard": {
            "candidate_code_sets": [candidateSet],
            "document_packages": [],
            "component_runs": [],
        },
        "candidate_code_set": candidateSet,
        "component_runs": [],
        "run_id": runId,
        "run_dir": str(runDirectory),
    }


def test_executor_bounds_running_and_pending_pipeline_runs(tmp_path: Path) -> None:
    executor = PipelineRunExecutor(maxWorkers=2, maxQueuedRuns=1)
    releaseTasks = threading.Event()
    stateCondition = threading.Condition()
    activeCount = 0
    maxActiveCount = 0

    def blocking_pipeline(**kwargs: object) -> dict[str, object]:
        nonlocal activeCount, maxActiveCount
        with stateCondition:
            activeCount += 1
            maxActiveCount = max(maxActiveCount, activeCount)
            stateCondition.notify_all()
        try:
            assert releaseTasks.wait(timeout=3.0)
            runId = str(kwargs["job_id"])
            runDirectory = tmp_path / runId
            runDirectory.mkdir()
            return _CompletedOutput(runId, runDirectory)
        finally:
            with stateCondition:
                activeCount -= 1
                stateCondition.notify_all()

    registry = RunRegistry()
    service = PipelineRunService(registry, blocking_pipeline, executor)
    try:
        for runId in ("job_1", "job_2"):
            registry.CreateRun(runId, query=runId, facts={})
            service.StartBackgroundRun(
                runId,
                PipelineRunRequest(query=runId),
            )
        _WaitFor(lambda: maxActiveCount == 2)
        registry.CreateRun("job_3", query="job_3", facts={})
        service.StartBackgroundRun(
            "job_3",
            PipelineRunRequest(query="job_3"),
        )

        registry.CreateRun("job_rejected", query="job_rejected", facts={})
        with pytest.raises(PipelineRunCapacityError):
            service.StartBackgroundRun(
                "job_rejected",
                PipelineRunRequest(query="job_rejected"),
            )
        assert registry.ReadSnapshot("job_rejected")["status"] == "failed"

        releaseTasks.set()
        _WaitFor(lambda: all(
            registry.ReadSnapshot(runId)["status"] == "completed"
            for runId in ("job_1", "job_2", "job_3")
        ))
        assert maxActiveCount == 2
    finally:
        releaseTasks.set()
        executor.Shutdown(wait=True)


def test_executor_survives_task_exception() -> None:
    executor = PipelineRunExecutor(maxWorkers=1, maxQueuedRuns=1)

    def failing_task() -> None:
        failedTaskRan.set()
        raise RuntimeError("expected task failure")

    failedTaskRan = threading.Event()
    completed = threading.Event()
    try:
        executor.Submit("job_failed", failing_task)
        assert failedTaskRan.wait(timeout=3.0)
        executor.Submit("job_after_failure", completed.set)
        executor.Shutdown(wait=True)
        assert completed.is_set()
    finally:
        executor.Shutdown(wait=True)


def test_pipeline_service_preserves_run_statuses(tmp_path: Path) -> None:
    executor = PipelineRunExecutor(maxWorkers=1, maxQueuedRuns=1)
    pipelineStarted = threading.Event()
    releasePipeline = threading.Event()

    def completed_pipeline(**kwargs: object) -> dict[str, object]:
        pipelineStarted.set()
        assert releasePipeline.wait(timeout=3.0)
        return _CompletedOutput(str(kwargs["job_id"]), tmp_path)

    registry = RunRegistry()
    service = PipelineRunService(registry, completed_pipeline, executor)
    registry.CreateRun("job_completed", query="completed", facts={})
    assert registry.ReadSnapshot("job_completed")["status"] == "queued"

    try:
        service.StartBackgroundRun(
            "job_completed",
            PipelineRunRequest(query="completed"),
        )
        assert pipelineStarted.wait(timeout=3.0)
        assert registry.ReadSnapshot("job_completed")["status"] == "running"
        releasePipeline.set()
        _WaitFor(
            lambda: registry.ReadSnapshot("job_completed")["status"] == "completed"
        )

        def failing_pipeline(**_kwargs: object) -> dict[str, object]:
            raise RuntimeError("pipeline failed")

        failedService = PipelineRunService(registry, failing_pipeline, executor)
        registry.CreateRun("job_failed", query="failed", facts={})
        failedService.StartBackgroundRun(
            "job_failed",
            PipelineRunRequest(query="failed"),
        )
        _WaitFor(lambda: registry.ReadSnapshot("job_failed")["status"] == "failed")
        failedSnapshot = registry.ReadSnapshot("job_failed")
        assert failedSnapshot["status"] == "failed"
        assert failedSnapshot["error"] == "pipeline failed"
    finally:
        releasePipeline.set()
        executor.Shutdown(wait=True)


def test_pipeline_api_rejects_run_when_capacity_is_full(tmp_path: Path) -> None:
    executor = PipelineRunExecutor(maxWorkers=1, maxQueuedRuns=0)
    releasePipeline = threading.Event()
    pipelineStarted = threading.Event()

    def blocking_pipeline(**kwargs: object) -> dict[str, object]:
        pipelineStarted.set()
        assert releasePipeline.wait(timeout=3.0)
        return _CompletedOutput(str(kwargs["job_id"]), tmp_path)

    registry = RunRegistry()
    service = PipelineRunService(registry, blocking_pipeline, executor)
    api = PipelineApi(registry, service)

    try:
        acceptedPayload, acceptedStatus = api.StartRunFromPayload({"query": "first"})
        assert acceptedStatus == 202
        assert pipelineStarted.wait(timeout=3.0)

        rejectedPayload, rejectedStatus = api.StartRunFromPayload({"query": "second"})
        assert rejectedStatus == 503
        assert rejectedPayload["error"] == "pipeline_capacity_exhausted"
        rejectedSnapshot = registry.ReadSnapshot(rejectedPayload["job_id"])
        assert rejectedSnapshot["status"] == "failed"
        assert rejectedSnapshot["error"] == "pipeline_capacity_exhausted"
        assert [event["status"] for event in rejectedSnapshot["events"]] == [
            "queued",
            "failed",
        ]

        releasePipeline.set()
        _WaitFor(
            lambda: registry.ReadSnapshot(acceptedPayload["job_id"])["status"]
            == "completed"
        )
    finally:
        releasePipeline.set()
        executor.Shutdown(wait=True)
