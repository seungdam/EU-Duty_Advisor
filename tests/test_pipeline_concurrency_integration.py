from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from bussiness_logic.artifact_paths import ExtractProductIdFromUrl
from bussiness_logic.input_process.reconstruction import InputReconstructionResult
from bussiness_logic.pipeline.blackboard import BlackboardStore
from bussiness_logic.pipeline.pipeline_context import PipelineContext
from bussiness_logic.product.ocr.download_execution import (
    DownloadExecutionCoordinator,
)
from bussiness_logic.product.ocr.ocr_execution import OcrExecutionCoordinator
from bussiness_logic.product.ocr.ocr_fallback import (
    ProductOcrArtifactStore,
    ProductOcrImageDownloader,
)
from bussiness_logic.product.ocr.paddle_ocr import (
    ProductOcrEngine,
    ProductStructuredOcrResult,
)
from bussiness_logic.product.pipeline.kurly_product_collection_pipeline import (
    KurlyProductCollectionPipeline,
)
from bussiness_logic.product.pipeline.kurly_url_intake_pipeline import (
    KurlyUrlIntakePipeline,
)
from bussiness_logic.product.pipeline.kurly_url_intake_schema import (
    KurlyUrlIntakeInput,
    KurlyUrlIntakeResult,
)
from bussiness_logic.product.pipeline.kurly_url_facts import (
    BuildKurlyUrlFactsFromPipelineResult,
)
from bussiness_logic.product.web_parser.kurly_market_collector import (
    KurlyPageCollector,
)
from bussiness_logic.product.web_parser.kurly_market_schema import (
    KurlyCollectionResult,
    KurlyProductDomain,
    KurlyProductPage,
    RenderedPageEvidence,
)


class _ConcurrencyState:
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


class _SupportedParser:
    def IsSupportedProductPageUrl(self, _url: str) -> bool:
        return True


class _FakeKurlyCollector(KurlyPageCollector):
    def __init__(
        self,
        gate: threading.BoundedSemaphore,
        pageBarrier: threading.Barrier,
        pageState: _ConcurrencyState,
        imageUrls: list[str],
    ) -> None:
        super().__init__(parser=_SupportedParser(), concurrencyGate=gate)
        self._pageBarrier = pageBarrier
        self._pageState = pageState
        self._imageUrls = imageUrls

    def _CollectRenderedPageEvidence(
        self,
        productPageUrl: str,
    ) -> RenderedPageEvidence:
        self._pageState.Enter()
        try:
            self._pageBarrier.wait(timeout=3.0)
            return RenderedPageEvidence(
                productPageUrl=productPageUrl,
                productDetailImageUrls=self._imageUrls,
            )
        finally:
            self._pageState.Exit()

    def BuildCollectionResult(
        self,
        renderedPageEvidence: RenderedPageEvidence,
    ) -> KurlyCollectionResult:
        return KurlyCollectionResult(
            productPageUrl=renderedPageEvidence.productPageUrl,
            parsedProductPage=KurlyProductPage(
                productPageUrl=renderedPageEvidence.productPageUrl,
                productDomain=KurlyProductDomain.FOOD,
                productName=renderedPageEvidence.productPageUrl.rsplit("/", 1)[-1],
                requiresOcrFallback=True,
            ),
            productDetailImageUrls=self._imageUrls,
            ocrCandidateImageUrls=self._imageUrls,
        )


class _FakeDownloader:
    def __init__(
        self,
        barrier: threading.Barrier,
        state: _ConcurrencyState,
    ) -> None:
        self._barrier = barrier
        self._state = state

    def Download(self, imageUrl: str, _downloadTimeoutSeconds: int) -> bytes:
        self._state.Enter()
        try:
            self._barrier.wait(timeout=3.0)
            return imageUrl.encode("utf-8")
        finally:
            self._state.Exit()


class _FakeOcrEngine(ProductOcrEngine):
    def __init__(self, state: _ConcurrencyState) -> None:
        self._state = state

    def ExtractTextFromImage(self, imageBytes: bytes) -> str:
        return self.ExtractStructuredTextFromImage(imageBytes).text

    def ExtractStructuredTextFromImage(
        self,
        imageBytes: bytes,
    ) -> ProductStructuredOcrResult:
        self._state.Enter()
        try:
            time.sleep(0.03)
            text = (
                "제품명 테스트 식품유형 과자 원재료 밀가루 설탕 "
                f"이미지 {imageBytes.decode('utf-8')}"
            )
            return ProductStructuredOcrResult(text=text, rawText=text)
        finally:
            self._state.Exit()


class _FakeReconstructionService:
    def ReconstructFromEvidencePackage(
        self,
        _evidencePackage: object,
    ) -> InputReconstructionResult:
        return InputReconstructionResult()


def test_same_product_runs_isolate_ocr_artifacts_and_publish_valid_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    productPageUrl = "https://www.kurly.com/goods/shared-product"
    productDirectory = tmp_path / ExtractProductIdFromUrl(productPageUrl)
    productDirectory.mkdir(parents=True)
    stableArtifactPath = productDirectory / "product-input.json"
    stableArtifactPath.write_text("{}", encoding="utf-8")
    pageState = _ConcurrencyState()
    downloadState = _ConcurrencyState()
    ocrState = _ConcurrencyState()
    pageGate = threading.BoundedSemaphore(2)
    pageBarrier = threading.Barrier(2)
    downloadBarrier = threading.Barrier(2)
    stableWriteBarrier = threading.Barrier(2)
    fakeDownloader = _FakeDownloader(downloadBarrier, downloadState)
    downloadCoordinator = DownloadExecutionCoordinator(
        maxWorkers=2,
        maxQueuedDownloads=2,
    )
    ocrCoordinator = OcrExecutionCoordinator()
    ocrEngine = _FakeOcrEngine(ocrState)
    monkeypatch.setattr(
        ProductOcrImageDownloader,
        "Download",
        lambda _self, imageUrl, timeout: fakeDownloader.Download(
            imageUrl,
            timeout,
        ),
    )
    cacheReadErrors: list[str] = []
    cacheReaderStarted = threading.Event()
    stopCacheReader = threading.Event()

    def ObserveStableCache() -> None:
        while not stopCacheReader.is_set():
            try:
                json.loads(stableArtifactPath.read_text(encoding="utf-8"))
            except PermissionError:
                pass
            except (FileNotFoundError, json.JSONDecodeError) as error:
                cacheReadErrors.append(str(error))
            cacheReaderStarted.set()
            time.sleep(0.001)

    cacheReader = threading.Thread(target=ObserveStableCache)
    cacheReader.start()
    assert cacheReaderStarted.wait(timeout=3.0)

    def RunPipeline(
        runName: str,
    ) -> tuple[KurlyUrlIntakeResult, dict[str, object]]:
        imageUrls = [
            f"https://images.example/{runName}-{index}.jpg"
            for index in (1, 2)
        ]
        pipeline = KurlyUrlIntakePipeline(
            collector=_FakeKurlyCollector(
                pageGate,
                pageBarrier,
                pageState,
                imageUrls,
            ),
            ocrEngine=ocrEngine,
            downloadExecutionCoordinator=downloadCoordinator,
            ocrExecutionCoordinator=ocrCoordinator,
            maxPendingOcrImages=2,
        )
        result = pipeline.Run(
            KurlyUrlIntakeInput(
                productPageUrl=productPageUrl,
                runId=runName,
                runOcrFallback=True,
                artifactRootPath=tmp_path,
                maxOcrImageCount=2,
                downloadTimeoutSeconds=1,
            )
        )
        stableWriteBarrier.wait(timeout=3.0)
        facts = BuildKurlyUrlFactsFromPipelineResult(
            productPageUrl,
            result,
            artifact_root=tmp_path,
        )
        return result, facts

    try:
        with ThreadPoolExecutor(max_workers=2) as runExecutor:
            results = list(runExecutor.map(RunPipeline, ("run-a", "run-b")))
    finally:
        stopCacheReader.set()
        cacheReader.join(timeout=3.0)
        downloadCoordinator.Shutdown(wait=True)
        ocrCoordinator.Shutdown(wait=True)

    assert not cacheReader.is_alive()
    assert pageState.maximum == 2
    assert downloadState.maximum == 2
    assert ocrState.maximum == 1
    assert ocrState.total == 4
    assert len(ocrState.threadIds) == 1
    assert not cacheReadErrors
    stablePayload = json.loads(stableArtifactPath.read_text(encoding="utf-8"))
    assert stablePayload["product_id"] == "shared-product"
    assert not list(productDirectory.glob(".product-input.json.*.tmp"))
    workspaces: list[Path] = []
    for runName, (result, facts) in zip(
        ("run-a", "run-b"),
        results,
        strict=True,
    ):
        expectedUrls = [
            f"https://images.example/{runName}-{index}.jpg"
            for index in (1, 2)
        ]
        otherRunName = "run-b" if runName == "run-a" else "run-a"
        workspace = productDirectory / "runs" / runName
        workspaces.append(workspace)
        assert workspace.is_dir()
        assert result.productPageUrl == productPageUrl
        assert result.errors == []
        assert [item.imageIndex for item in result.ocrImageResults] == [1, 2]
        assert [item.imageUrl for item in result.ocrImageResults] == expectedUrls
        assert all(item.error is None for item in result.ocrImageResults)
        artifactPaths = [
            Path(item.imagePath or "")
            for item in result.ocrImageResults
        ]
        assert all(path.parent == workspace for path in artifactPaths)
        assert [path.read_bytes() for path in artifactPaths] == [
            imageUrl.encode("utf-8")
            for imageUrl in expectedUrls
        ]
        assert runName in result.combinedOcrText
        assert otherRunName not in result.combinedOcrText
        urlIntake = facts.get("url_intake")
        assert isinstance(urlIntake, dict)
        assert urlIntake["artifact_root"] == str(productDirectory)
    assert workspaces[0] != workspaces[1]


def test_cached_reconstruction_remains_in_product_level_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bussiness_logic.product.pipeline import kurly_url_facts

    productPageUrl = "https://www.kurly.com/goods/cache-product"
    productDirectory = tmp_path / "cache-product"
    productDirectory.mkdir(parents=True)
    (productDirectory / "product-input.json").write_text(
        json.dumps({"url": productPageUrl, "warnings": []}),
        encoding="utf-8",
    )
    (productDirectory / "llm-input-reconstruction-request.json").write_text(
        json.dumps({
            "product_page_url": productPageUrl,
            "request": {
                "user_prompt": 'evidence\n{"evidence": []}',
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        kurly_url_facts,
        "PRODUCT_INPUT_ARTIFACT_ROOT",
        tmp_path,
    )
    monkeypatch.setattr(
        kurly_url_facts,
        "_BuildInputReconstructionService",
        lambda _warnings: _FakeReconstructionService(),
    )

    facts = kurly_url_facts.RerunCachedInputReconstruction(productPageUrl)

    assert facts["url"] == productPageUrl
    assert facts["product_id"] == "cache-product"


def test_product_collection_reuses_existing_pipeline_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bussiness_logic.product.pipeline import kurly_url_facts

    capturedRunIds: list[str | None] = []

    def CollectFacts(
        _url: str,
        **options: object,
    ) -> dict[str, object]:
        runId = options.get("runId")
        capturedRunIds.append(runId if isinstance(runId, str) else None)
        return {"url_intake": {"collected": True}}

    monkeypatch.setattr(kurly_url_facts, "CollectKurlyUrlFacts", CollectFacts)
    context = PipelineContext(
        query="shared product",
        facts={"url": "https://www.kurly.com/goods/shared-product"},
        store=cast(
            BlackboardStore,
            SimpleNamespace(run_id="run_existing"),
        ),
    )

    KurlyProductCollectionPipeline().Run(context)

    assert capturedRunIds == ["run_existing"]


@pytest.mark.parametrize("runId", ["", "../other-run", "run/nested"])
def test_ocr_workspace_rejects_unsafe_run_id(
    tmp_path: Path,
    runId: str,
) -> None:
    with pytest.raises(ValueError, match="safe artifact path segment"):
        ProductOcrArtifactStore().PrepareArtifactDirectory(
            tmp_path,
            "https://www.kurly.com/goods/product",
            runId=runId,
        )
