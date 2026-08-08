from __future__ import annotations

import threading
from pathlib import Path

from bussiness_logic.product.ocr.download_execution import (
    DownloadExecutionCoordinator,
)
from bussiness_logic.product.ocr.ocr_execution import OcrExecutionCoordinator
from bussiness_logic.product.ocr.ocr_fallback import ProductOcrFallbackRunner
from bussiness_logic.product.ocr.paddle_ocr import (
    ProductOcrEngine,
    ProductStructuredOcrResult,
)


def test_download_submit_blocks_when_running_and_pending_capacity_is_full() -> None:
    coordinator = DownloadExecutionCoordinator(
        maxWorkers=1,
        maxQueuedDownloads=1,
    )
    releaseTasks = threading.Event()
    firstTaskStarted = threading.Event()
    thirdSubmitted = threading.Event()
    thirdFuture: list[object] = []

    def blocking_task() -> str:
        firstTaskStarted.set()
        assert releaseTasks.wait(timeout=3.0)
        return "released"

    try:
        firstFuture = coordinator.Submit(blocking_task)
        assert firstTaskStarted.wait(timeout=3.0)
        secondFuture = coordinator.Submit(blocking_task)

        def submit_third() -> None:
            thirdFuture.append(coordinator.Submit(lambda: "third"))
            thirdSubmitted.set()

        submitThread = threading.Thread(target=submit_third)
        submitThread.start()
        assert not thirdSubmitted.wait(timeout=0.1)

        releaseTasks.set()
        assert thirdSubmitted.wait(timeout=3.0)
        submitThread.join(timeout=3.0)
        assert firstFuture.result(timeout=3.0) == "released"
        assert secondFuture.result(timeout=3.0) == "released"
        assert thirdFuture[0].result(timeout=3.0) == "third"
    finally:
        releaseTasks.set()
        coordinator.Shutdown(wait=True)


class _DownloadState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.startedUrls: list[str] = []
        self.startedAtFirstOcr: int | None = None
        self.completedUrls: list[str] = []
        self.ocrStarted = threading.Event()
        self.overlapObserved = False

    def Enter(self, imageUrl: str) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.startedUrls.append(imageUrl)

    def Complete(self, imageUrl: str) -> None:
        with self.lock:
            self.active -= 1
            self.completedUrls.append(imageUrl)


class _OutOfOrderDownloader:
    def __init__(self, state: _DownloadState) -> None:
        self._state = state

    def Download(self, imageUrl: str, _downloadTimeoutSeconds: int) -> bytes:
        self._state.Enter(imageUrl)
        try:
            if imageUrl.endswith(("first.jpg", "failed.jpg")):
                assert self._state.ocrStarted.wait(timeout=3.0)
            if imageUrl.endswith("failed.jpg"):
                raise RuntimeError("expected download failure")
            return imageUrl.encode("utf-8")
        finally:
            self._state.Complete(imageUrl)


class _DownloadAwareOcrEngine(ProductOcrEngine):
    def __init__(self, downloadState: _DownloadState) -> None:
        self._downloadState = downloadState

    def ExtractTextFromImage(self, imageBytes: bytes) -> str:
        return self.ExtractStructuredTextFromImage(imageBytes).text

    def ExtractStructuredTextFromImage(
        self,
        imageBytes: bytes,
    ) -> ProductStructuredOcrResult:
        with self._downloadState.lock:
            if self._downloadState.startedAtFirstOcr is None:
                self._downloadState.startedAtFirstOcr = len(
                    self._downloadState.startedUrls
                )
            self._downloadState.overlapObserved = (
                self._downloadState.overlapObserved
                or len(self._downloadState.completedUrls) < 5
            )
        self._downloadState.ocrStarted.set()
        text = "informative product " + imageBytes.decode("utf-8")
        return ProductStructuredOcrResult(text=text, rawText=text)


def test_download_ocr_overlap_preserves_order_and_isolates_failure(
    tmp_path: Path,
) -> None:
    downloadCoordinator = DownloadExecutionCoordinator(
        maxWorkers=2,
        maxQueuedDownloads=1,
    )
    ocrCoordinator = OcrExecutionCoordinator()
    downloadState = _DownloadState()
    imageUrls = [
        "https://example.com/first.jpg",
        "https://example.com/second.jpg",
        "https://example.com/failed.jpg",
        "https://example.com/fourth.jpg",
        "https://example.com/fifth.jpg",
    ]
    statuses: list[tuple[int, str, str]] = []
    runner = ProductOcrFallbackRunner(
        _DownloadAwareOcrEngine(downloadState),
        imageDownloader=_OutOfOrderDownloader(downloadState),
        downloadExecutionCoordinator=downloadCoordinator,
        ocrExecutionCoordinator=ocrCoordinator,
        maxPendingOcrImages=2,
    )

    try:
        results = runner.Run(
            imageUrls=imageUrls,
            artifactRootPath=tmp_path,
            productPageUrl="https://www.kurly.com/goods/phase3",
            maxImageCount=5,
            downloadTimeoutSeconds=1,
            imageStatusCallback=lambda index, url, status, _rejection, _failure: (
                statuses.append((index, url, status))
            ),
        )
    finally:
        downloadCoordinator.Shutdown(wait=True)
        ocrCoordinator.Shutdown(wait=True)

    assert downloadState.maximum == 2
    assert downloadState.overlapObserved
    assert (downloadState.startedAtFirstOcr or 0) < len(imageUrls)
    assert downloadState.completedUrls[0].endswith("second.jpg")
    assert [result.imageIndex for result in results] == [1, 2, 3, 4, 5]
    assert [result.imageUrl for result in results] == imageUrls
    assert results[0].error is None
    assert results[1].error is None
    assert "expected download failure" in (results[2].error or "")
    assert Path(results[0].imagePath or "").name == "ocr-fallback-image-01.jpg"
    assert Path(results[1].imagePath or "").name == "ocr-fallback-image-02.jpg"
    finalStatuses = [
        status
        for _index, _url, status in statuses
        if status in {"extracted", "rejected", "failed"}
    ]
    assert finalStatuses == [
        "extracted",
        "extracted",
        "failed",
        "extracted",
        "extracted",
    ]
