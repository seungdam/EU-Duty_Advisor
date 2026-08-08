from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bussiness_logic.product.ocr.ocr_execution import OcrExecutionCoordinator
from bussiness_logic.product.ocr.ocr_fallback import ProductOcrFallbackRunner
from bussiness_logic.product.ocr.paddle_ocr import (
    ProductOcrEngine,
    ProductOcrTextRegion,
    ProductStructuredOcrResult,
)


class _ConcurrentCallState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.total = 0
        self.threadIds: set[int] = set()

    def Enter(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.total += 1
            self.threadIds.add(threading.get_ident())

    def Exit(self) -> None:
        with self.lock:
            self.active -= 1


class _OverlappingDownloader:
    def __init__(
        self,
        barrier: threading.Barrier,
        state: _ConcurrentCallState,
    ) -> None:
        self._barrier = barrier
        self._state = state

    def Download(self, _imageUrl: str, _downloadTimeoutSeconds: int) -> bytes:
        self._state.Enter()
        try:
            self._barrier.wait(timeout=3.0)
            return b"fake-image"
        finally:
            self._state.Exit()


class _TrackingOcrEngine(ProductOcrEngine):
    def __init__(
        self,
        state: _ConcurrentCallState,
        *,
        screening: bool = False,
    ) -> None:
        self._state = state
        self._screening = screening

    def ExtractTextFromImage(self, imageBytes: bytes) -> str:
        return self.ExtractStructuredTextFromImage(imageBytes).text

    def ExtractStructuredTextFromImage(
        self,
        _imageBytes: bytes,
    ) -> ProductStructuredOcrResult:
        return self._Extract()

    def ExtractStructuredTextWithRegionsFromImage(
        self,
        _imageBytes: bytes,
    ) -> tuple[ProductStructuredOcrResult, list[ProductOcrTextRegion]]:
        return self._Extract(), []

    def _Extract(self) -> ProductStructuredOcrResult:
        self._state.Enter()
        try:
            time.sleep(0.03)
            text = (
                "영양정보 나트륨 10mg 탄수화물 20g 당류 3g 지방 4g "
                "제품명 테스트 식품유형 과자 원재료 밀가루 내용량 100g "
                "보관방법 실온 소비기한 제조일로부터 일년"
                if self._screening
                else "제품명 테스트 상품 원재료 밀가루 설탕"
            )
            return ProductStructuredOcrResult(text=text, rawText=text)
        finally:
            self._state.Exit()


def test_two_runs_overlap_downloads_but_serialize_shared_ocr(
    tmp_path: Path,
) -> None:
    coordinator = OcrExecutionCoordinator()
    downloadState = _ConcurrentCallState()
    ocrState = _ConcurrentCallState()
    downloadBarrier = threading.Barrier(2)
    postOcrBarrier = threading.Barrier(2)
    structuredEngine = _TrackingOcrEngine(ocrState)
    screeningEngine = _TrackingOcrEngine(ocrState, screening=True)

    def run_pipeline(runNumber: int) -> str:
        imageUrl = f"https://example.com/image-{runNumber}.jpg"
        runner = ProductOcrFallbackRunner(
            structuredEngine,
            screeningEngine=screeningEngine,
            imageDownloader=_OverlappingDownloader(downloadBarrier, downloadState),
            ocrExecutionCoordinator=coordinator,
        )
        result = runner.Run(
            imageUrls=[imageUrl],
            artifactRootPath=tmp_path / f"run-{runNumber}",
            productPageUrl=f"https://www.kurly.com/goods/{runNumber}",
            maxImageCount=1,
            downloadTimeoutSeconds=1,
        )
        postOcrBarrier.wait(timeout=3.0)
        assert len(result) == 1
        assert result[0].error is None
        assert result[0].imageUrl == imageUrl
        return result[0].ocrText

    try:
        with ThreadPoolExecutor(max_workers=2) as runExecutor:
            outputs = list(runExecutor.map(run_pipeline, (1, 2)))
    finally:
        coordinator.Shutdown(wait=True)

    assert downloadState.maximum == 2
    assert ocrState.maximum == 1
    assert ocrState.total == 4
    assert len(ocrState.threadIds) == 1
    assert all(output.startswith("영양정보") for output in outputs)


def test_ocr_worker_survives_task_exception() -> None:
    coordinator = OcrExecutionCoordinator()

    def fail() -> None:
        raise RuntimeError("expected OCR failure")

    try:
        with pytest.raises(RuntimeError, match="expected OCR failure"):
            coordinator.Execute(fail)
        assert coordinator.Execute(lambda: "next OCR succeeded") == (
            "next OCR succeeded"
        )
    finally:
        coordinator.Shutdown(wait=True)
