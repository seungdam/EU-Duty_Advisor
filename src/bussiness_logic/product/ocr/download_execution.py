"""Bounded process-local execution for OCR image downloads."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import TypeVar


DownloadResult = TypeVar("DownloadResult")


class DownloadExecutionCoordinator:
    """Run downloads with fixed workers and bounded pending capacity."""

    def __init__(self, *, maxWorkers: int = 4, maxQueuedDownloads: int = 4) -> None:
        if maxWorkers < 1:
            raise ValueError("maxWorkers must be at least 1")
        if maxQueuedDownloads < 0:
            raise ValueError("maxQueuedDownloads must not be negative")

        self._capacity = threading.BoundedSemaphore(
            maxWorkers + maxQueuedDownloads,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=maxWorkers,
            thread_name_prefix="image-download",
        )
        self._stateLock = threading.Lock()
        self._isShutdown = False

    def Submit(
        self,
        operation: Callable[[], DownloadResult],
    ) -> Future[DownloadResult]:
        self._capacity.acquire()
        with self._stateLock:
            if self._isShutdown:
                self._capacity.release()
                raise RuntimeError("DownloadExecutionCoordinator is shut down")
            try:
                future = self._executor.submit(operation)
            except BaseException:
                self._capacity.release()
                raise
        future.add_done_callback(lambda _future: self._capacity.release())
        return future

    def Shutdown(self, *, wait: bool = True) -> None:
        with self._stateLock:
            if self._isShutdown:
                return
            self._isShutdown = True
        self._executor.shutdown(wait=wait)


SHARED_DOWNLOAD_EXECUTION_COORDINATOR = DownloadExecutionCoordinator()
