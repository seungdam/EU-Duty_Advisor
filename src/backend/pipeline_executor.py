"""Bounded process-local execution for background pipeline runs."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor


_LOGGER = logging.getLogger(__name__)
PipelineTask = Callable[[], None]


class PipelineRunCapacityError(RuntimeError):
    """Raised when the process cannot admit another pipeline run."""

    def __init__(self, runId: str) -> None:
        self.runId = runId
        super().__init__(f"Pipeline run capacity exhausted: {runId}")


class PipelineRunExecutor:
    """Fixed thread pool with an explicit running plus pending task limit."""

    def __init__(self, *, maxWorkers: int, maxQueuedRuns: int) -> None:
        if maxWorkers < 1:
            raise ValueError("maxWorkers must be at least 1")
        if maxQueuedRuns < 0:
            raise ValueError("maxQueuedRuns must not be negative")

        self._capacity = threading.BoundedSemaphore(maxWorkers + maxQueuedRuns)
        self._executor = ThreadPoolExecutor(
            max_workers=maxWorkers,
            thread_name_prefix="pipeline-run",
        )
        self._stateLock = threading.Lock()
        self._isShutdown = False

    def Submit(self, runId: str, task: PipelineTask) -> None:
        with self._stateLock:
            if self._isShutdown or not self._capacity.acquire(blocking=False):
                raise PipelineRunCapacityError(runId)
            try:
                future = self._executor.submit(task)
            except BaseException:
                self._capacity.release()
                raise

        future.add_done_callback(
            lambda completedFuture: self._OnTaskDone(runId, completedFuture),
        )

    def Shutdown(self, *, wait: bool = True) -> None:
        with self._stateLock:
            if self._isShutdown:
                return
            self._isShutdown = True
        self._executor.shutdown(wait=wait)

    def _OnTaskDone(self, runId: str, future: Future[None]) -> None:
        self._capacity.release()
        error = future.exception()
        if error is not None:
            _LOGGER.error(
                "Pipeline executor task failed (run_id=%s)",
                runId,
                exc_info=(type(error), error, error.__traceback__),
            )
