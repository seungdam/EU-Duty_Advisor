"""동일 OCR 전처리에서 VLM 4종과 고정 Reconstruction 1종 비교 smoke.

웹 수집과 이미지 다운로드는 한 번만 수행한다. 네 경로는 서로 다른
VLM evidence를 만들며 screening, ROI, PP-Structure, merge는 기존 Pipeline을
그대로 사용한다. 각 evidence는 동일 Reconstruction RuntimeAdapter로 실행한다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Iterable, Sequence
from urllib.request import Request, urlopen

PROJECT_ROOT_PATH = Path(__file__).resolve().parent
SRC_ROOT_PATH = PROJECT_ROOT_PATH / "src"
APP_CONFIG_PATH = PROJECT_ROOT_PATH / ".appconfig.ocr_reconstruction_smoke.toml"
ENV_FILE_PATH = PROJECT_ROOT_PATH / ".env.ocr_reconstruction_smoke"
os.environ["ASAP_APP_CONFIG_PATH"] = str(APP_CONFIG_PATH)
os.environ["ASAP_ENV_FILE"] = str(ENV_FILE_PATH)
if str(SRC_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT_PATH))

from bussiness_logic.app_config import AppConfig, LlmProfileName, LoadAppConfig
from bussiness_logic.artifact_paths import ExtractProductIdFromUrl
from bussiness_logic.bridge.adapter import RuntimeAdapter
from bussiness_logic.bridge.runtime_adapter import BuildPipelineRuntimeAdapter
from bussiness_logic.bridge.schema import (
    LlmGenerationOptions,
    LlmImageInput,
    LlmRequest,
    LlmResponse,
    LlmRuntimeConfig,
    LlmRuntimeKind,
)
from bussiness_logic.input_process.reconstruction import (
    InputReconstructionResult,
    ProductInputEvidenceBuilder,
    ProductInputReconstructionService,
)
from bussiness_logic.product.ocr.ocr_fallback import (
    ProductOcrArtifactStore,
    ProductOcrFallbackRunner,
    ProductOcrImageDownloader,
    ProductOcrImageResult,
    ProductOcrTextQualityEvaluator,
)
from bussiness_logic.product.ocr.paddle_ocr import (
    PaddleOcrEngine,
    ProductOcrEngine,
    ProductOcrTextRegion,
    ProductStructuredOcrEngine,
    ProductStructuredOcrResult,
)
from bussiness_logic.product.ocr.vlm_adapter import BridgeVlmAdapter
from bussiness_logic.product.web_parser.kurly_domestic import KurlyDomesticPageParser
from bussiness_logic.product.web_parser.kurly_global import KurlyGlobalPageParser
from bussiness_logic.product.web_parser.kurly_market_collector import (
    GetSharedKurlyCrawlerGate,
    KurlyPageCollector,
)
from bussiness_logic.product.web_parser.kurly_market_schema import (
    KurlyCollectionResult,
)
from bussiness_logic.product.web_parser.kurly_page_adapter import KurlyPageAdapter

DEFAULT_ARTIFACT_ROOT_PATH = (
    PROJECT_ROOT_PATH / "artifacts" / "ocr-reconstruction-smoke"
)
PREFLIGHT_IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAI0lEQVQIHW3BAQEAAAABIP6P"
    "zgFV5CnyFHmKPEWeIk+Rp8gzVoYX8WHqMxQAAAAASUVORK5CYII="
)
RECONSTRUCTION_PROFILE_DEFINITIONS = (
    ("gpt_reconstruction", LlmProfileName.INPUT_RECONSTRUCTION_GPT),
    ("gemini_reconstruction", LlmProfileName.INPUT_RECONSTRUCTION_GEMINI),
    ("claude_reconstruction", LlmProfileName.INPUT_RECONSTRUCTION_CLAUDE),
)
EXPECTED_VLM_ROUTE_COUNT = 4
DEFAULT_RECONSTRUCTION_MODEL = "gemini"
RECONSTRUCTION_PROFILES = {
    name.removesuffix("_reconstruction"): (name, profileName)
    for name, profileName in RECONSTRUCTION_PROFILE_DEFINITIONS
}


@dataclass(slots=True)
class RuntimeMetrics:
    requestCount: int = 0
    imageCount: int = 0
    imageBytes: int = 0
    inputTokens: int = 0
    outputTokens: int = 0
    totalTokens: int = 0

    def Snapshot(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.requestCount,
            self.imageCount,
            self.imageBytes,
            self.inputTokens,
            self.outputTokens,
            self.totalTokens,
        )

    def Delta(
        self,
        before: tuple[int, int, int, int, int, int],
    ) -> dict[str, int]:
        after = self.Snapshot()
        keys = (
            "request_count",
            "image_count",
            "image_bytes",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        )
        return {
            key: after[index] - before[index]
            for index, key in enumerate(keys)
        }


@dataclass(frozen=True, slots=True)
class ReconstructionRoute:
    name: str
    service: ProductInputReconstructionService
    runtime: dict[str, str]
    metrics: RuntimeMetrics


@dataclass(frozen=True, slots=True)
class SmokeRoute:
    name: str
    structuredOcrEngine: "CapturingStructuredOcrEngine"
    structuredRuntime: dict[str, str]
    visionMetrics: RuntimeMetrics
    reconstructionRoute: ReconstructionRoute


class TableShapeParser(HTMLParser):
    """표 HTML의 물리적 행별 셀 수를 비교용으로 읽는다."""

    def __init__(self) -> None:
        super().__init__()
        self.rowCellCounts: list[int] = []
        self._currentCellCount = 0
        self._insideRow = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalizedTag = tag.lower()
        if normalizedTag == "tr":
            self._insideRow = True
            self._currentCellCount = 0
            return
        if normalizedTag not in {"td", "th"} or not self._insideRow:
            return
        colspan = dict(attrs).get("colspan")
        self._currentCellCount += (
            int(colspan)
            if isinstance(colspan, str) and colspan.isdigit()
            else 1
        )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "tr" or not self._insideRow:
            return
        self.rowCellCounts.append(self._currentCellCount)
        self._currentCellCount = 0
        self._insideRow = False


class StaticImageDownloader(ProductOcrImageDownloader):
    """동일하게 다운로드한 원본 bytes를 네 비교 경로에 제공한다."""

    def __init__(self, imagesByUrl: dict[str, bytes]) -> None:
        self._imagesByUrl = imagesByUrl

    def Download(self, imageUrl: str, downloadTimeoutSeconds: int) -> bytes:
        del downloadTimeoutSeconds
        return self._imagesByUrl[imageUrl]


class SharedInputOcrArtifactStore(ProductOcrArtifactStore):
    """route 산출물은 분리하되 원본 이미지는 shared-input을 참조한다."""

    def __init__(self, imagePathsByUrl: dict[str, Path]) -> None:
        self._imagePathsByUrl = imagePathsByUrl

    def WriteImage(
        self,
        artifactDirectory: Path,
        imageIndex: int,
        imageUrl: str,
        imageBytes: bytes,
    ) -> Path:
        del artifactDirectory, imageIndex, imageBytes
        return self._imagePathsByUrl[imageUrl]

    def ReplaceImageWithInformativeTiles(
        self,
        artifactPath: Path,
        imageIndex: int,
        imageUrl: str,
        imageTiles: Sequence[tuple[int | None, bytes]],
        structuredOcrResult: ProductStructuredOcrResult,
        textQualityEvaluator: ProductOcrTextQualityEvaluator,
    ) -> list[Path]:
        del imageIndex, imageUrl, imageTiles
        return (
            [artifactPath]
            if textQualityEvaluator.HasInformativeResult(structuredOcrResult)
            else []
        )


class CachedScreeningOcrEngine(ProductOcrEngine):
    """raw OCR screening을 한 번 수행해 네 VLM 경로에 동일하게 제공한다."""

    def __init__(self, delegate: PaddleOcrEngine) -> None:
        self._delegate = delegate
        self._results: dict[
            bytes,
            tuple[ProductStructuredOcrResult, list[ProductOcrTextRegion]],
        ] = {}

    def Prime(self, imageBytesList: Sequence[bytes]) -> float:
        startedAt = perf_counter()
        for imageBytes in imageBytesList:
            if imageBytes not in self._results:
                self._results[imageBytes] = (
                    self._delegate.ExtractStructuredTextWithRegionsFromImage(
                        imageBytes,
                    )
                )
        return perf_counter() - startedAt

    def Clear(self) -> None:
        self._results.clear()

    def ExtractTextFromImage(self, imageBytes: bytes) -> str:
        return self.ExtractStructuredTextWithRegionsFromImage(imageBytes)[0].text

    def ExtractStructuredTextWithRegionsFromImage(
        self,
        imageBytes: bytes,
    ) -> tuple[ProductStructuredOcrResult, list[ProductOcrTextRegion]]:
        if imageBytes not in self._results:
            self.Prime([imageBytes])
        return self._results[imageBytes]


class CapturingStructuredOcrEngine(ProductOcrEngine):
    """screening 병합 전 VLM 직접 출력을 smoke 결과에 보존한다."""

    def __init__(self, delegate: ProductOcrEngine) -> None:
        self._delegate = delegate
        self._extractions: list[dict[str, object]] = []
        self._inputArtifactDirectory: Path | None = None

    def ExtractTextFromImage(self, imageBytes: bytes) -> str:
        return self.ExtractStructuredTextFromImage(imageBytes).text

    def ExtractStructuredTextFromImage(
        self,
        imageBytes: bytes,
    ) -> ProductStructuredOcrResult:
        inputHash = hashlib.sha256(imageBytes).hexdigest()
        inputArtifactPath = self._WriteInputArtifact(imageBytes, inputHash)
        inputDetails = {
            "input_sha256": inputHash,
            "input_bytes": len(imageBytes),
            "input_artifact_path": (
                str(inputArtifactPath) if inputArtifactPath is not None else None
            ),
        }
        try:
            result = self._delegate.ExtractStructuredTextFromImage(imageBytes)
        except Exception as error:
            self._extractions.append(
                {
                    **inputDetails,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            raise
        self._extractions.append(
            {
                **inputDetails,
                "extracted_text": result.text,
                "raw_text": result.rawText,
                "structured_text": result.structuredText,
                "text_merge_mode": result.textMergeMode,
                "used_structured_tables": result.usedStructuredTables,
                "fallback_reason": result.fallbackReason,
                "tables": [
                    table.model_dump(mode="json", by_alias=True)
                    for table in result.tables
                ],
                "table_candidates": [
                    candidate.model_dump(mode="json", by_alias=True)
                    for candidate in result.tableCandidates
                ],
                "layout_diagnostics": [
                    diagnostic.model_dump(mode="json", by_alias=True)
                    for diagnostic in result.layoutDiagnostics
                ],
                "warnings": list(result.warnings),
            }
        )
        return result

    def BuildArtifactImageTiles(
        self,
        imageBytes: bytes,
    ) -> list[tuple[int | None, bytes]]:
        return self._delegate.BuildArtifactImageTiles(imageBytes)

    def ResetExtractions(self) -> None:
        self._extractions.clear()

    def SetInputArtifactDirectory(self, artifactDirectory: Path) -> None:
        self._inputArtifactDirectory = artifactDirectory

    def _WriteInputArtifact(
        self,
        imageBytes: bytes,
        inputHash: str,
    ) -> Path | None:
        if self._inputArtifactDirectory is None:
            return None
        suffix = ReadImageSuffix(imageBytes)
        artifactPath = self._inputArtifactDirectory / (
            f"vlm-input-{inputHash}{suffix}"
        )
        artifactPath.parent.mkdir(parents=True, exist_ok=True)
        if not artifactPath.exists():
            artifactPath.write_bytes(imageBytes)
        return artifactPath

    def TakeExtractions(self) -> list[dict[str, object]]:
        extractions = list(self._extractions)
        self._extractions.clear()
        return extractions


def ReadImageSuffix(imageBytes: bytes) -> str:
    if imageBytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if imageBytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if imageBytes.startswith(b"RIFF") and imageBytes[8:12] == b"WEBP":
        return ".webp"
    return ".img"


def ParseArguments(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "동일한 PaddleOCR screening 이미지에서 VLM 4종의 표 복원과 "
            "고정 Reconstruction 모델의 4x1 결과를 비교합니다."
        ),
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="product_urls",
        help="검사할 상품 URL입니다. 여러 번 지정할 수 있습니다.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="웹 수집 브라우저를 표시합니다.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="API와 모델 연결만 검증하고 종료합니다.",
    )
    parser.add_argument(
        "--reconstruction-model",
        choices=tuple(RECONSTRUCTION_PROFILES),
        default=DEFAULT_RECONSTRUCTION_MODEL,
        help="4x1 비교에서 고정할 Reconstruction 모델입니다.",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=None,
        help="상품별 이미지 수입니다. 기본값은 appconfig, 0이면 전체입니다.",
    )
    parser.add_argument(
        "--vlm-input-mode",
        choices=("pipeline", "full_image"),
        default="pipeline",
        help=(
            "pipeline은 실제 ROI 입력, full_image는 ROI 손실을 확인하는 "
            "원본 이미지 대조군입니다."
        ),
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT_PATH,
    )
    parser.add_argument("--summary-path", type=Path, default=None)
    parsedArguments = parser.parse_args(arguments)
    if parsedArguments.max_images is not None and parsedArguments.max_images < 0:
        parser.error("--max-images must be greater than or equal to 0")
    return parsedArguments


def BuildMeasuredRuntimeAdapter(
    profileName: LlmProfileName,
) -> tuple[RuntimeAdapter[object], RuntimeMetrics]:
    delegate = BuildPipelineRuntimeAdapter(profileName)
    metrics = RuntimeMetrics()

    def Generate(
        _descriptor: object,
        _runtimeConfig: LlmRuntimeConfig,
        request: LlmRequest,
    ) -> LlmResponse:
        metrics.requestCount += 1
        metrics.imageCount += len(request.imageInputs)
        metrics.imageBytes += sum(
            len(imageInput.imageBytes)
            for imageInput in request.imageInputs
        )
        response = delegate.Generate(request)
        metrics.inputTokens += response.tokenUsage.inputTokens or 0
        metrics.outputTokens += response.tokenUsage.outputTokens or 0
        metrics.totalTokens += response.tokenUsage.totalTokens or 0
        return response

    return (
        RuntimeAdapter(
            delegate.RuntimeConfig(),
            delegate.RuntimeDescriptor(),
            Generate,
        ),
        metrics,
    )


def BuildRuntimeSummary(adapter: RuntimeAdapter[object]) -> dict[str, str]:
    runtimeConfig = adapter.RuntimeConfig()
    return {
        "runtime": runtimeConfig.runtimeKind.value,
        "provider": str(runtimeConfig.extraOptions.get("provider") or ""),
        "model": runtimeConfig.modelName or "",
    }


def BuildPreflightRequest(*, includeImage: bool) -> LlmRequest:
    return LlmRequest(
        system_prompt="You are a model connection health check.",
        user_prompt="Reply with exactly: ok",
        image_inputs=(
            [
                LlmImageInput(
                    media_type="image/png",
                    image_bytes=PREFLIGHT_IMAGE_BYTES,
                    source_ref="preflight://blank-image",
                )
            ]
            if includeImage
            else []
        ),
        generation_options=LlmGenerationOptions(
            temperature=0.0,
            max_tokens=64,
        ),
    )


def RequirePaddleVlmConnection(appConfig: AppConfig) -> None:
    smokeConfig = appConfig.kurly_smoke
    serverUrl = smokeConfig.structured_ocr_vl_rec_server_url
    modelName = smokeConfig.structured_ocr_vl_rec_api_model_name
    if not serverUrl or not modelName:
        raise RuntimeError(
            "PaddleOCR-VL preflight requires server URL and API model name."
        )
    endpointUrl = serverUrl.rstrip("/") + "/v1/chat/completions"
    imageDataUrl = "data:image/png;base64,{0}".format(
        base64.b64encode(PREFLIGHT_IMAGE_BYTES).decode("ascii"),
    )
    payload = {
        "model": modelName,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Reply with exactly: ok"},
                    {
                        "type": "image_url",
                        "image_url": {"url": imageDataUrl},
                    },
                ],
            }
        ],
        "max_tokens": 64,
        "temperature": 0.0,
        "stream": False,
    }
    request = Request(
        endpointUrl,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=smokeConfig.timeout_seconds) as response:
        responsePayload = json.loads(response.read().decode("utf-8"))
    choices = (
        responsePayload.get("choices")
        if isinstance(responsePayload, dict)
        else None
    )
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("PaddleOCR-VL preflight returned no model response.")


def RunPreflight(
    appConfig: AppConfig,
    reconstructionProfile: tuple[str, LlmProfileName],
) -> None:
    checks = (
        (*reconstructionProfile, False),
        ("gpt_vlm", LlmProfileName.PRODUCT_VLM_GPT, True),
        ("gemini_vlm", LlmProfileName.PRODUCT_VLM_GEMINI, True),
        ("claude_vlm", LlmProfileName.PRODUCT_VLM_CLAUDE, True),
    )
    totalCheckCount = len(checks) + 1
    print(
        f"[preflight 1/{totalCheckCount}] "
        "provider=paddleocr_vl status=checking"
    )
    RequirePaddleVlmConnection(appConfig)
    paddleModel = appConfig.kurly_smoke.structured_ocr_vl_rec_api_model_name
    print(
        f"[preflight 1/{totalCheckCount}] provider=paddleocr_vl "
        f"model={paddleModel} status=ok"
    )
    for checkIndex, (label, profileName, includeImage) in enumerate(
        checks,
        start=2,
    ):
        adapter = BuildPipelineRuntimeAdapter(profileName)
        runtimeSummary = BuildRuntimeSummary(adapter)
        print(
            (
                "[preflight {0}/{1}] profile={2} provider={3} "
                "model={4} status=checking"
            ).format(
                checkIndex,
                totalCheckCount,
                label,
                runtimeSummary["provider"],
                runtimeSummary["model"],
            )
        )
        response = adapter.Generate(
            BuildPreflightRequest(includeImage=includeImage)
        )
        if not response.generatedText.strip():
            raise RuntimeError(f"{label} preflight returned an empty response.")
        print(
            (
                "[preflight {0}/{1}] profile={2} provider={3} "
                "model={4} status=ok"
            ).format(
                checkIndex,
                totalCheckCount,
                label,
                runtimeSummary["provider"],
                runtimeSummary["model"],
            )
        )


def BuildSmokeRoutes(
    appConfig: AppConfig,
    runRootPath: Path,
    reconstructionProfile: tuple[str, LlmProfileName],
) -> list[SmokeRoute]:
    smokeConfig = appConfig.kurly_smoke
    dictionaryPath = (
        appConfig.paths.ResolvePath(
            PROJECT_ROOT_PATH,
            smokeConfig.input_dictionary_path,
        )
        if smokeConfig.input_dictionary_path is not None
        else None
    )
    routes: list[SmokeRoute] = []
    routeDefinitions: list[
        tuple[str, ProductOcrEngine, dict[str, str], RuntimeMetrics]
    ] = [
        (
            "paddleocr_vl",
            BuildStructuredOcrEngine(appConfig),
            {
                "runtime": (
                    "remote"
                    if smokeConfig.structured_ocr_vl_rec_server_url
                    else "local"
                ),
                "provider": "paddleocr_vl",
                "model": str(
                    smokeConfig.structured_ocr_vl_rec_api_model_name or "default"
                ),
            },
            RuntimeMetrics(),
        ),
    ]
    hostedRouteProfiles = (
        (
            "gpt_vlm",
            LlmProfileName.PRODUCT_VLM_GPT,
            LlmRuntimeKind.OPENAI,
            {"openai"},
        ),
        (
            "gemini_vlm",
            LlmProfileName.PRODUCT_VLM_GEMINI,
            LlmRuntimeKind.OPENAI,
            {"google_ai_studio", "google", "gemini"},
        ),
        (
            "claude_vlm",
            LlmProfileName.PRODUCT_VLM_CLAUDE,
            LlmRuntimeKind.ANTHROPIC,
            {"anthropic"},
        ),
    )
    for routeName, profileName, expectedRuntime, expectedProviders in hostedRouteProfiles:
        runtimeAdapter, visionMetrics = BuildMeasuredRuntimeAdapter(profileName)
        runtimeSummary = BuildRuntimeSummary(runtimeAdapter)
        if (
            runtimeAdapter.RuntimeKind() != expectedRuntime
            or runtimeSummary["provider"] not in expectedProviders
        ):
            raise RuntimeError(
                "llm_profiles.{0} must configure the {1} VLM provider.".format(
                    profileName.value,
                    routeName.removesuffix("_vlm"),
                )
            )
        routeDefinitions.append(
            (
                routeName,
                BuildStructuredOcrEngine(
                    appConfig,
                    vlPipeline=BridgeVlmAdapter(runtimeAdapter),
                ),
                runtimeSummary,
                visionMetrics,
            )
        )
    for routeName, ocrEngine, runtimeSummary, visionMetrics in routeDefinitions:
        reconstructionName, profileName = reconstructionProfile
        reconstructionRuntime, reconstructionMetrics = BuildMeasuredRuntimeAdapter(
            profileName,
        )
        reconstructionRoute = ReconstructionRoute(
            name=reconstructionName,
            service=ProductInputReconstructionService(
                dictionaryPath=(
                    str(dictionaryPath)
                    if dictionaryPath is not None
                    else None
                ),
                runtimeAdapter=reconstructionRuntime,
                fuzzyMinRatio=smokeConfig.input_dictionary_fuzzy_min_ratio,
                llmMaxTokens=smokeConfig.llm_input_reconstruction_max_tokens,
                llmArtifactRootPath=(
                    runRootPath / routeName / reconstructionName
                ),
            ),
            runtime=BuildRuntimeSummary(reconstructionRuntime),
            metrics=reconstructionMetrics,
        )
        routes.append(
            SmokeRoute(
                name=routeName,
                structuredOcrEngine=CapturingStructuredOcrEngine(ocrEngine),
                structuredRuntime=runtimeSummary,
                visionMetrics=visionMetrics,
                reconstructionRoute=reconstructionRoute,
            )
        )
    return routes


def BuildStructuredOcrEngine(
    appConfig: AppConfig,
    vlPipeline: object | None = None,
) -> ProductStructuredOcrEngine:
    smokeConfig = appConfig.kurly_smoke
    return ProductStructuredOcrEngine(
        vlExtraOptions=(
            smokeConfig.BuildStructuredOcrVlExtraOptions()
            if vlPipeline is None
            else None
        ),
        vlPipeline=vlPipeline,
        useProjectionTiling=smokeConfig.structured_ocr_use_projection_tiling,
        maxTileHeightPixels=smokeConfig.structured_ocr_max_tile_height_pixels,
        maxTileSidePixels=smokeConfig.structured_ocr_max_tile_side_pixels,
        tileOverlapPixels=smokeConfig.structured_ocr_tile_overlap_pixels,
        allowHardCutFallback=(
            smokeConfig.structured_ocr_allow_hard_cut_fallback
        ),
        enableDirectTableRecognitionDiagnostic=True,
    )


def BuildCollector(appConfig: AppConfig, headed: bool) -> KurlyPageCollector:
    smokeConfig = appConfig.kurly_smoke
    return KurlyPageCollector(
        parser=KurlyPageAdapter(
            domesticParser=KurlyDomesticPageParser(),
            globalParser=KurlyGlobalPageParser(),
        ),
        headless=False if headed else smokeConfig.headless,
        timeoutMilliseconds=smokeConfig.timeout_seconds * 1000,
        scrollCount=smokeConfig.scroll_count,
        concurrencyGate=GetSharedKurlyCrawlerGate(
            smokeConfig.crawl_concurrency,
        ),
    )


def DownloadImages(
    imageUrls: Sequence[str],
    timeoutSeconds: int,
) -> tuple[dict[str, bytes], list[dict[str, str]], float]:
    downloader = ProductOcrImageDownloader()
    imagesByUrl: dict[str, bytes] = {}
    errors: list[dict[str, str]] = []
    startedAt = perf_counter()
    for imageUrl in imageUrls:
        try:
            imagesByUrl[imageUrl] = downloader.Download(imageUrl, timeoutSeconds)
        except Exception as error:  # noqa: BLE001 - smoke는 URL별 실패를 계속 기록한다.
            errors.append({"image_url": imageUrl, "error": str(error)})
    return imagesByUrl, errors, perf_counter() - startedAt


def WriteSharedInputArtifacts(
    runRootPath: Path,
    productPageUrl: str,
    imagesByUrl: dict[str, bytes],
) -> tuple[dict[str, Path], Path]:
    artifactStore = ProductOcrArtifactStore()
    artifactDirectory = artifactStore.PrepareArtifactDirectory(
        runRootPath / "shared-input",
        productPageUrl,
    )
    imagePathsByUrl: dict[str, Path] = {}
    imageRecords: list[dict[str, object]] = []
    for imageIndex, (imageUrl, imageBytes) in enumerate(
        imagesByUrl.items(),
        start=1,
    ):
        artifactPath = artifactStore.WriteImage(
            artifactDirectory,
            imageIndex,
            imageUrl,
            imageBytes,
        )
        imagePathsByUrl[imageUrl] = artifactPath
        imageRecords.append(
            {
                "image_index": imageIndex,
                "image_url": imageUrl,
                "artifact_path": str(artifactPath),
                "sha256": hashlib.sha256(imageBytes).hexdigest(),
                "bytes": len(imageBytes),
            }
        )
    manifestPath = artifactDirectory / "manifest.json"
    manifestPath.write_text(
        json.dumps({"images": imageRecords}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return imagePathsByUrl, manifestPath


def WriteVlmCheckpoint(
    runRootPath: Path,
    route: SmokeRoute,
    productPageUrl: str,
    sharedInputManifestPath: Path,
    vlmInputMode: str,
    extractions: Sequence[dict[str, object]],
) -> Path:
    artifactDirectory = (
        runRootPath
        / route.name
        / ExtractProductIdFromUrl(productPageUrl)
    )
    artifactDirectory.mkdir(parents=True, exist_ok=True)
    checkpointPath = artifactDirectory / "vlm-extractions.json"
    checkpointPath.write_text(
        json.dumps(
            {
                "route": route.name,
                "product_url": productPageUrl,
                "vlm_input_mode": vlmInputMode,
                "structured_ocr_runtime": route.structuredRuntime,
                "shared_input_manifest_path": str(sharedInputManifestPath),
                "metrics": BuildDirectVlmMetrics(extractions),
                "extractions": list(extractions),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return checkpointPath


def RunRoute(
    route: SmokeRoute,
    collectionResult: KurlyCollectionResult,
    imagesByUrl: dict[str, bytes],
    sharedImagePathsByUrl: dict[str, Path],
    sharedInputManifestPath: Path,
    screeningOcrEngine: ProductOcrEngine,
    runRootPath: Path,
    timeoutSeconds: int,
    vlmInputMode: str,
) -> dict[str, object]:
    visionSnapshot = route.visionMetrics.Snapshot()
    sharedProductDirectory = sharedInputManifestPath.parent
    route.structuredOcrEngine.SetInputArtifactDirectory(
        sharedProductDirectory / "vlm-inputs",
    )
    route.structuredOcrEngine.ResetExtractions()
    ocrStartedAt = perf_counter()
    ocrResults = ProductOcrFallbackRunner(
        route.structuredOcrEngine,
        imageDownloader=StaticImageDownloader(imagesByUrl),
        artifactStore=SharedInputOcrArtifactStore(sharedImagePathsByUrl),
        screeningEngine=screeningOcrEngine,
        useStructuredOcrRegionCrop=vlmInputMode == "pipeline",
        enableTableGroundingDiagnostic=True,
    ).Run(
        imageUrls=list(imagesByUrl),
        artifactRootPath=runRootPath / route.name,
        productPageUrl=collectionResult.productPageUrl,
        maxImageCount=len(imagesByUrl),
        downloadTimeoutSeconds=timeoutSeconds,
        reuseArtifactImages=False,
    )
    ocrElapsedSeconds = perf_counter() - ocrStartedAt
    directVlmExtractions = route.structuredOcrEngine.TakeExtractions()
    checkpointPath = WriteVlmCheckpoint(
        runRootPath,
        route,
        collectionResult.productPageUrl,
        sharedInputManifestPath,
        vlmInputMode,
        directVlmExtractions,
    )
    combinedOcrText = ProductOcrFallbackRunner.BuildCombinedOcrText(ocrResults)
    reconstructionEvidence = ProductInputEvidenceBuilder().BuildFromPipelineParts(
        collectionResult=collectionResult,
        ocrImageResults=ocrResults,
        combinedOcrText=combinedOcrText,
    )
    reconstructionEvidenceHash = hashlib.sha256(
        json.dumps(
            reconstructionEvidence.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    reconstructionRoute = route.reconstructionRoute
    reconstructionSnapshot = reconstructionRoute.metrics.Snapshot()
    reconstructionStartedAt = perf_counter()
    try:
        reconstructionResult = reconstructionRoute.service.ReconstructFromPipelineParts(
            collectionResult=collectionResult,
            ocrImageResults=ocrResults,
            combinedOcrText=combinedOcrText,
        )
    except Exception as error:  # noqa: BLE001 - route 오류를 결과에 보존한다.
        reconstructionOutput: dict[str, object] = {
            "reconstruction_profile": reconstructionRoute.name,
            "runtime": reconstructionRoute.runtime,
            "metrics": {
                "wall_seconds": round(
                    perf_counter() - reconstructionStartedAt,
                    3,
                ),
                "runtime": reconstructionRoute.metrics.Delta(
                    reconstructionSnapshot,
                ),
            },
            "error": f"{type(error).__name__}: {error}",
        }
    else:
        reconstructionOutput = {
            "reconstruction_profile": reconstructionRoute.name,
            "runtime": reconstructionRoute.runtime,
            "metrics": BuildReconstructionMetrics(
                reconstructionResult,
                perf_counter() - reconstructionStartedAt,
                reconstructionRoute.metrics.Delta(reconstructionSnapshot),
            ),
            "result": BuildReconstructionOutput(reconstructionResult),
        }
    return {
        "route": route.name,
        "vlm_input_mode": vlmInputMode,
        "structured_ocr_runtime": route.structuredRuntime,
        "ocr_metrics": BuildOcrMetrics(
            ocrResults,
            ocrElapsedSeconds,
            route.visionMetrics.Delta(visionSnapshot),
        ),
        "vlm_direct_metrics": BuildDirectVlmMetrics(directVlmExtractions),
        "vlm_extractions": directVlmExtractions,
        "vlm_checkpoint_path": str(checkpointPath),
        "shared_input_manifest_path": str(sharedInputManifestPath),
        "reconstruction_input": {
            "combined_ocr_text_sha256": hashlib.sha256(
                combinedOcrText.encode("utf-8"),
            ).hexdigest(),
            "combined_ocr_text_characters": len(combinedOcrText),
            "evidence_sha256": reconstructionEvidenceHash,
            "admitted_vlm_table_count": sum(
                record.sourceType == "vlm_table"
                for record in reconstructionEvidence.records
            ),
        },
        "reconstruction": reconstructionOutput,
    }


def BuildOcrMetrics(
    imageResults: Sequence[ProductOcrImageResult],
    elapsedSeconds: float,
    runtimeMetrics: dict[str, int],
) -> dict[str, object]:
    tables = [
        table
        for imageResult in imageResults
        for table in imageResult.structuredOcr.tables
    ]
    tableCandidates = [
        candidate
        for imageResult in imageResults
        for candidate in imageResult.structuredOcr.tableCandidates
    ]
    groundingDiagnostics = [
        diagnostic
        for imageResult in imageResults
        for diagnostic in imageResult.structuredOcr.tableGroundingDiagnostics
    ]
    groundingRows = [
        row
        for diagnostic in groundingDiagnostics
        for row in diagnostic.rows
    ]
    groundingIssueCounts = Counter(
        re.sub(r"^cell_\d+_", "", issue.split(":", 1)[0])
        for row in groundingRows
        for issue in row.issues
    )
    groundedRowCount = sum(row.status == "grounded" for row in groundingRows)
    return {
        "wall_seconds": round(elapsedSeconds, 3),
        "raw_ocr_seconds": round(sum(
            imageResult.processingTimes.get("raw_ocr", 0.0)
            for imageResult in imageResults
        ), 3),
        "roi_build_seconds": round(sum(
            imageResult.processingTimes.get("roi_build", 0.0)
            for imageResult in imageResults
        ), 3),
        "structured_ocr_seconds": round(sum(
            imageResult.processingTimes.get("structured_ocr", 0.0)
            for imageResult in imageResults
        ), 3),
        "image_count": len(imageResults),
        "successful_image_count": sum(
            imageResult.error is None and imageResult.skippedReason is None
            for imageResult in imageResults
        ),
        "skipped_image_count": sum(
            imageResult.skippedReason is not None
            for imageResult in imageResults
        ),
        "error_count": sum(imageResult.error is not None for imageResult in imageResults),
        "roi_applied_count": sum(
            any(
                warning.startswith("structured_ocr_roi_applied")
                for warning in imageResult.structuredOcr.warnings
            )
            for imageResult in imageResults
        ),
        "table_count": len(tables),
        "candidate_table_count": len(tableCandidates),
        "localization_valid_count": sum(
            candidate.localizationStatus == "valid"
            for candidate in tableCandidates
        ),
        "localization_rejected_count": sum(
            candidate.localizationStatus == "invalid"
            for candidate in tableCandidates
        ),
        "structure_verified_table_count": sum(
            candidate.validationStatus == "structure_verified"
            for candidate in tableCandidates
        ),
        "rejected_table_candidate_count": sum(
            candidate.validationStatus == "rejected"
            for candidate in tableCandidates
        ),
        "table_grounding_diagnostic_count": len(groundingDiagnostics),
        "table_grounding_grounded_count": sum(
            diagnostic.status == "grounded"
            for diagnostic in groundingDiagnostics
        ),
        "table_grounding_partial_count": sum(
            diagnostic.status == "partial"
            for diagnostic in groundingDiagnostics
        ),
        "table_grounding_rejected_count": sum(
            diagnostic.status == "rejected"
            for diagnostic in groundingDiagnostics
        ),
        "row_grounding_grounded_count": groundedRowCount,
        "row_grounding_rejected_count": sum(
            row.status == "rejected"
            for row in groundingRows
        ),
        "row_grounding_derived_bbox_count": sum(
            row.derivedBounds is not None
            for row in groundingRows
        ),
        "row_grounding_total_count": len(groundingRows),
        "row_grounding_rate": (
            round(groundedRowCount / len(groundingRows), 4)
            if groundingRows
            else None
        ),
        "row_grounding_issue_counts": dict(groundingIssueCounts),
        "admitted_table_count": len(tables),
        "verified_table_count": sum(
            table.validationStatus == "verified"
            for table in tables
        ),
        "table_validation_issue_count": sum(
            len(table.validationIssues)
            for table in tables
        ),
        "raw_text_characters": sum(
            len(imageResult.structuredOcr.rawText)
            for imageResult in imageResults
        ),
        "structured_text_characters": sum(
            len(imageResult.structuredOcr.structuredText)
            for imageResult in imageResults
        ),
        "runtime": runtimeMetrics,
    }


def BuildDirectVlmMetrics(
    extractions: Sequence[dict[str, object]],
) -> dict[str, object]:
    successfulExtractions = [
        extraction
        for extraction in extractions
        if "error" not in extraction
    ]
    candidates = [
        candidate
        for extraction in successfulExtractions
        if isinstance(candidateRecords := extraction.get("table_candidates"), list)
        for candidate in candidateRecords
        if isinstance(candidate, dict)
    ]
    admittedTables = [
        table
        for extraction in successfulExtractions
        if isinstance(tables := extraction.get("tables"), list)
        for table in tables
        if isinstance(table, dict)
    ]
    layoutDiagnostics = [
        diagnostic
        for extraction in successfulExtractions
        if isinstance(
            diagnostics := extraction.get("layout_diagnostics"),
            list,
        )
        for diagnostic in diagnostics
        if isinstance(diagnostic, dict)
    ]
    layoutRegions = [
        region
        for diagnostic in layoutDiagnostics
        if isinstance(regions := diagnostic.get("regions"), list)
        for region in regions
        if isinstance(region, dict)
    ]
    directRecognitionTables = [
        table
        for diagnostic in layoutDiagnostics
        if isinstance(
            tables := diagnostic.get("direct_recognition_tables"),
            list,
        )
        for table in tables
        if isinstance(table, dict)
    ]
    return {
        "extraction_count": len(extractions),
        "successful_extraction_count": len(successfulExtractions),
        "error_count": len(extractions) - len(successfulExtractions),
        "input_bytes": sum(
            int(extraction.get("input_bytes") or 0)
            for extraction in extractions
        ),
        "extracted_text_characters": sum(
            len(value)
            for extraction in successfulExtractions
            if isinstance(value := extraction.get("extracted_text"), str)
        ),
        "raw_text_characters": sum(
            len(value)
            for extraction in successfulExtractions
            if isinstance(value := extraction.get("raw_text"), str)
        ),
        "structured_text_characters": sum(
            len(value)
            for extraction in successfulExtractions
            if isinstance(value := extraction.get("structured_text"), str)
        ),
        "table_count": len(admittedTables),
        "candidate_table_count": len(candidates),
        "layout_diagnostic_count": len(layoutDiagnostics),
        "layout_region_count": len(layoutRegions),
        "layout_table_region_count": sum(
            str(region.get("label") or "").lower() == "table"
            for region in layoutRegions
        ),
        "layout_selected_region_count": sum(
            region.get("selected_for_vlm") is True
            for region in layoutRegions
        ),
        "direct_recognition_attempt_count": sum(
            diagnostic.get("direct_recognition_attempted") is True
            for diagnostic in layoutDiagnostics
        ),
        "direct_recognition_payload_count": sum(
            int(diagnostic.get("direct_recognition_payload_count") or 0)
            for diagnostic in layoutDiagnostics
        ),
        "direct_recognition_table_count": len(directRecognitionTables),
        "direct_recognition_error_count": sum(
            bool(diagnostic.get("direct_recognition_error"))
            for diagnostic in layoutDiagnostics
        ),
        "localization_valid_count": sum(
            candidate.get("localization_status") == "valid"
            for candidate in candidates
        ),
        "localization_inferred_count": sum(
            candidate.get("localization_status") == "inferred"
            for candidate in candidates
        ),
        "localization_rejected_count": sum(
            candidate.get("localization_status") == "invalid"
            for candidate in candidates
        ),
        "structure_verified_count": sum(
            candidate.get("validation_status") == "structure_verified"
            for candidate in candidates
        ),
        "rejected_candidate_count": sum(
            candidate.get("validation_status") == "rejected"
            for candidate in candidates
        ),
        "admitted_table_count": len(admittedTables),
        "fallback_reasons": sorted({
            str(reason)
            for extraction in successfulExtractions
            if (reason := extraction.get("fallback_reason"))
        }),
    }


def BuildReconstructionMetrics(
    result: InputReconstructionResult,
    elapsedSeconds: float,
    runtimeMetrics: dict[str, int],
) -> dict[str, object]:
    return {
        "wall_seconds": round(elapsedSeconds, 3),
        "used_llm_reconstruction": result.usedLlmReconstruction,
        "fallback_reason": result.fallbackReason,
        "product_fact_count": len(result.productFacts),
        "reconstructed_table_count": len(result.reconstructedTables),
        "reconstructed_row_count": sum(
            len(table.rows)
            for table in result.reconstructedTables
        ),
        "unresolved_fact_count": len(result.unresolvedFacts),
        "missing_fact_reason_count": len(result.missingFactReasons),
        "conflict_count": len(result.conflicts),
        "evidence_trace_count": len(result.evidenceTraces),
        "runtime": runtimeMetrics,
    }


def BuildReconstructionOutput(
    result: InputReconstructionResult,
) -> dict[str, object]:
    payload = result.model_dump(mode="json", by_alias=True)
    return {
        key: payload.get(key)
        for key in (
            "product_facts",
            "reconstructed_tables",
            "unresolved_facts",
            "missing_fact_reasons",
            "conflicts",
            "evidence_traces",
            "warnings",
        )
    }


def BuildComparison(routeResults: Sequence[dict[str, object]]) -> dict[str, object]:
    return {
        "vlm_direct": BuildDirectVlmComparison(routeResults),
        "table_restoration": BuildTableRestorationComparison(routeResults),
        "reconstruction_4x1": BuildReconstructionComparison(
            routeResults,
        ),
    }


def BuildDirectVlmComparison(
    routeResults: Sequence[dict[str, object]],
) -> dict[str, object]:
    inputHashesByRoute: dict[str, list[str]] = {}
    rawTextByRoute: dict[str, str] = {}
    rawTextCharactersByRoute: dict[str, int] = {}
    for routeResult in routeResults:
        routeName = str(routeResult.get("route") or "")
        extractions = routeResult.get("vlm_extractions")
        extractionRecords = (
            [item for item in extractions if isinstance(item, dict)]
            if isinstance(extractions, list)
            else []
        )
        inputHashesByRoute[routeName] = [
            str(value)
            for extraction in extractionRecords
            if (value := extraction.get("input_sha256"))
        ]
        rawText = "\n".join(
            value
            for extraction in extractionRecords
            if isinstance(value := extraction.get("raw_text"), str) and value
        )
        rawTextByRoute[routeName] = rawText
        rawTextCharactersByRoute[routeName] = len(rawText)

    routeNames = list(rawTextByRoute)
    pairwiseTextSimilarity = []
    for firstRoute, secondRoute in combinations(routeNames, 2):
        firstText = NormalizeComparisonText(rawTextByRoute[firstRoute])
        secondText = NormalizeComparisonText(rawTextByRoute[secondRoute])
        similarity = (
            SequenceMatcher(
                None,
                firstText,
                secondText,
                autojunk=False,
            ).ratio()
            if firstText and secondText
            else 0.0
        )
        pairwiseTextSimilarity.append(
            {
                "routes": [firstRoute, secondRoute],
                "similarity_ratio": round(similarity, 4),
            }
        )

    hashSequences = list(inputHashesByRoute.values())
    sameInputForAllRoutes = (
        len(hashSequences) == EXPECTED_VLM_ROUTE_COUNT
        and bool(hashSequences[0])
        and all(sequence == hashSequences[0] for sequence in hashSequences[1:])
    )
    hasCompleteInputFingerprints = (
        len(hashSequences) == EXPECTED_VLM_ROUTE_COUNT
        and all(hashSequences)
    )
    return {
        "same_input_for_all_routes": sameInputForAllRoutes,
        "input_comparison_status": (
            "insufficient_data"
            if not hasCompleteInputFingerprints
            else "same"
            if sameInputForAllRoutes
            else "different"
        ),
        "input_sha256_by_route": inputHashesByRoute,
        "raw_text_characters_by_route": rawTextCharactersByRoute,
        "raw_text_sha256_by_route": {
            routeName: hashlib.sha256(rawText.encode("utf-8")).hexdigest()
            for routeName, rawText in rawTextByRoute.items()
        },
        "pairwise_raw_text_similarity": pairwiseTextSimilarity,
        "metric_warning": (
            "Text length and pairwise similarity measure extraction consistency, "
            "not ground-truth OCR accuracy."
        ),
    }


def BuildTableRestorationComparison(
    routeResults: Sequence[dict[str, object]],
) -> dict[str, object]:
    routes: dict[str, dict[str, object]] = {}
    for routeResult in routeResults:
        routeName = str(routeResult.get("route") or "")
        extractions = routeResult.get("vlm_extractions")
        extractionRecords = (
            [item for item in extractions if isinstance(item, dict)]
            if isinstance(extractions, list)
            else []
        )
        tableDetails = []
        admittedTableCount = 0
        for extractionIndex, extraction in enumerate(extractionRecords, start=1):
            tables = extraction.get("tables")
            admittedTableCount += len(tables) if isinstance(tables, list) else 0
            candidates = extraction.get("table_candidates")
            candidateRecords = (
                candidates
                if isinstance(candidates, list)
                else tables
                if isinstance(tables, list)
                else []
            )
            tableDetails.extend(
                BuildTableAgreement(table, extractionIndex)
                for table in candidateRecords
                if isinstance(table, dict)
            )

        comparableTables = [
            table
            for table in tableDetails
            if table["pp_structure_available"]
        ]
        issueCounts = Counter(
            str(issue)
            for table in tableDetails
            for issue in table["validation_issues"]
        )
        routes[routeName] = {
            "detected_table_count": len(tableDetails),
            "candidate_table_count": len(tableDetails),
            "admitted_table_count": admittedTableCount,
            "localization_valid_count": sum(
                table.get("localization_status") == "valid"
                for extraction in extractionRecords
                if isinstance(candidates := extraction.get("table_candidates"), list)
                for table in candidates
                if isinstance(table, dict)
            ),
            "localization_rejected_count": sum(
                table.get("localization_status") == "invalid"
                for extraction in extractionRecords
                if isinstance(candidates := extraction.get("table_candidates"), list)
                for table in candidates
                if isinstance(table, dict)
            ),
            "rejected_table_count": sum(
                table.get("validation_status") == "rejected"
                for extraction in extractionRecords
                if isinstance(candidates := extraction.get("table_candidates"), list)
                for table in candidates
                if isinstance(table, dict)
            ),
            "pp_structure_comparable_table_count": len(comparableTables),
            "pp_structure_unscorable_candidate_count": (
                len(tableDetails) - len(comparableTables)
            ),
            "pp_agreement_metric_eligibility": (
                "diagnostic_only" if comparableTables else "not_comparable"
            ),
            "verified_table_count": sum(
                table["validation_status"] in {"verified", "structure_verified"}
                for table in tableDetails
            ),
            "structure_exact_match_rate": BuildMean(
                float(table["structure_exact_match"])
                for table in comparableTables
            ),
            "mean_ordered_cell_similarity": BuildMean(
                float(table["ordered_cell_similarity"])
                for table in comparableTables
            ),
            "mean_cell_precision": BuildMean(
                float(table["cell_precision"])
                for table in comparableTables
            ),
            "mean_cell_recall": BuildMean(
                float(table["cell_recall"])
                for table in comparableTables
            ),
            "mean_cell_f1": BuildMean(
                float(table["cell_f1"])
                for table in comparableTables
            ),
            "validation_issue_counts": dict(sorted(issueCounts.items())),
            "tables": tableDetails,
        }
    return {
        "reference": "PaddleOCR TableRecognitionPipelineV2",
        "cross_provider_ranking_eligible": False,
        "routes": routes,
        "limitation": (
            "PP-Structure localizes table regions before VLM extraction. Its "
            "recognition output is diagnostic evidence, not ground truth or a "
            "cross-provider ranking."
        ),
    }


def BuildTableAgreement(
    table: dict[str, object],
    extractionIndex: int,
) -> dict[str, object]:
    evidence = table.get("table_recognition_evidence")
    evidence = evidence if isinstance(evidence, dict) else None
    vlmCells = NormalizeCellTexts(table.get("cell_texts"))
    ppCells = NormalizeCellTexts(
        evidence.get("cell_texts") if evidence is not None else None,
    )
    vlmShape = ParseTableShape(str(table.get("html") or ""))
    ppShape = ParseTableShape(
        str(evidence.get("html") or "") if evidence is not None else "",
    )
    matchedCellCount = sum((Counter(vlmCells) & Counter(ppCells)).values())
    precision = matchedCellCount / len(vlmCells) if vlmCells else 0.0
    recall = matchedCellCount / len(ppCells) if ppCells else 0.0
    cellF1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    orderedSimilarity = (
        SequenceMatcher(
            None,
            "\n".join(vlmCells),
            "\n".join(ppCells),
            autojunk=False,
        ).ratio()
        if vlmCells and ppCells
        else 0.0
    )
    validationIssues = table.get("validation_issues")
    return {
        "extraction_index": extractionIndex,
        "table_index": table.get("table_index"),
        "source_name": table.get("source_name"),
        "pp_structure_available": evidence is not None,
        "validation_status": table.get("validation_status"),
        "validation_issues": (
            validationIssues
            if isinstance(validationIssues, list)
            else []
        ),
        "vlm_row_cell_counts": vlmShape,
        "pp_structure_row_cell_counts": ppShape,
        "structure_exact_match": bool(vlmShape and vlmShape == ppShape),
        "vlm_cell_count": len(vlmCells),
        "pp_structure_cell_count": len(ppCells),
        "matched_cell_count": matchedCellCount,
        "cell_precision": round(precision, 4),
        "cell_recall": round(recall, 4),
        "cell_f1": round(cellF1, 4),
        "ordered_cell_similarity": round(orderedSimilarity, 4),
    }


def ParseTableShape(html: str) -> list[int]:
    parser = TableShapeParser()
    parser.feed(html)
    return parser.rowCellCounts


def NormalizeCellTexts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        normalized
        for item in value
        if isinstance(item, str)
        and (normalized := NormalizeComparisonText(item))
    ]


def BuildMean(values: Iterable[float]) -> float | None:
    valueList = list(values)
    return round(sum(valueList) / len(valueList), 4) if valueList else None


def BuildReconstructionComparison(
    routeResults: Sequence[dict[str, object]],
) -> dict[str, object]:
    cells: list[dict[str, object]] = []
    factSetsByVlm: dict[str, set[str]] = {}
    reconstructionProfiles: set[str] = set()
    inputHashesByVlm: dict[str, str] = {}
    admittedTableCountsByVlm: dict[str, int] = {}

    for routeResult in routeResults:
        routeName = str(routeResult.get("route") or "")
        reconstructionInput = routeResult.get("reconstruction_input")
        if isinstance(reconstructionInput, dict):
            inputHash = reconstructionInput.get("evidence_sha256") or (
                reconstructionInput.get("combined_ocr_text_sha256")
            )
            if inputHash:
                inputHashesByVlm[routeName] = str(inputHash)
            admittedTableCountsByVlm[routeName] = int(
                reconstructionInput.get("admitted_vlm_table_count") or 0
            )
        record = routeResult.get("reconstruction")
        if not isinstance(record, dict):
            cells.append({"vlm_route": routeName, "status": "missing"})
            continue
        reconstructionName = str(record.get("reconstruction_profile") or "")
        reconstructionProfiles.add(reconstructionName)
        result = record.get("result")
        cell = {
            "vlm_route": routeName,
            "reconstruction_profile": reconstructionName,
            "status": "ok" if isinstance(result, dict) else "error",
            "runtime": record.get("runtime"),
            "metrics": record.get("metrics"),
        }
        if "error" in record:
            cell["error"] = record["error"]
        cells.append(cell)
        if isinstance(result, dict):
            factSetsByVlm[routeName] = BuildFactSet(result)

    uniqueFactSets = {
        tuple(sorted(factSet))
        for factSet in factSetsByVlm.values()
    }
    sameInputForAllRoutes = (
        len(inputHashesByVlm) == EXPECTED_VLM_ROUTE_COUNT
        and len(set(inputHashesByVlm.values())) == 1
    )
    productFactsDiffer = len(uniqueFactSets) > 1
    hasCompleteInputFingerprints = (
        len(inputHashesByVlm) == EXPECTED_VLM_ROUTE_COUNT
    )
    hasCompleteProductFactOutputs = (
        len(factSetsByVlm) == EXPECTED_VLM_ROUTE_COUNT
    )
    inputComparisonStatus = (
        "insufficient_data"
        if not hasCompleteInputFingerprints
        else "same"
        if sameInputForAllRoutes
        else "different"
    )
    vlmInputDiffers = (
        None
        if inputComparisonStatus == "insufficient_data"
        else inputComparisonStatus == "different"
    )
    if not hasCompleteInputFingerprints:
        causalAttribution = "insufficient_input_fingerprints"
    elif not hasCompleteProductFactOutputs:
        causalAttribution = "insufficient_reconstruction_outputs"
    elif not productFactsDiffer:
        causalAttribution = "no_observed_output_difference"
    elif sameInputForAllRoutes:
        causalAttribution = "reconstruction_variability_on_same_input"
    else:
        causalAttribution = "indeterminate"
    return {
        "selected_reconstruction_profile": (
            next(iter(reconstructionProfiles))
            if len(reconstructionProfiles) == 1
            else sorted(reconstructionProfiles)
        ),
        "expected_cell_count": EXPECTED_VLM_ROUTE_COUNT,
        "observed_cell_count": len(cells),
        "successful_cell_count": len(factSetsByVlm),
        "same_input_for_all_routes": sameInputForAllRoutes,
        "input_sha256_by_route": inputHashesByVlm,
        "admitted_vlm_table_count_by_route": admittedTableCountsByVlm,
        "product_facts_differ": productFactsDiffer,
        "input_comparison_status": inputComparisonStatus,
        "output_comparison_status": (
            "complete" if hasCompleteProductFactOutputs else "insufficient_data"
        ),
        "vlm_input_differs": vlmInputDiffers,
        "product_fact_difference_coincides_with_vlm_input_difference": (
            hasCompleteProductFactOutputs
            and productFactsDiffer
            and vlmInputDiffers is True
        ),
        "causal_attribution": causalAttribution,
        "cells": cells,
        "fact_agreement_across_vlms": BuildFactAgreementGroup(factSetsByVlm),
        "metric_warning": (
            "Fact agreement measures model consistency, not factual accuracy."
        ),
    }


def BuildFactAgreementGroup(
    factSets: dict[str, set[str]],
) -> dict[str, object]:
    memberNames = list(factSets)
    pairwiseAgreement = []
    for firstMember, secondMember in combinations(memberNames, 2):
        firstFacts = factSets[firstMember]
        secondFacts = factSets[secondMember]
        unionFacts = firstFacts | secondFacts
        pairwiseAgreement.append(
            {
                "members": [firstMember, secondMember],
                "agreement_ratio": (
                    round(len(firstFacts & secondFacts) / len(unionFacts), 4)
                    if unionFacts
                    else 0.0
                ),
                "common_fact_count": len(firstFacts & secondFacts),
            }
        )
    commonFacts = (
        set.intersection(*(factSets[name] for name in memberNames))
        if memberNames
        else set()
    )
    return {
        "successful_member_count": len(memberNames),
        "pairwise_fact_agreement": pairwiseAgreement,
        "common_facts": sorted(commonFacts),
    }


def BuildFactSet(reconstruction: object) -> set[str]:
    if not isinstance(reconstruction, dict):
        return set()
    productFacts = reconstruction.get("product_facts")
    if not isinstance(productFacts, list):
        return set()
    return {
        "{0}:{1}".format(
            NormalizeComparisonText(str(fact.get("field_name") or "")),
            NormalizeComparisonText(str(fact.get("normalized_value") or "")),
        )
        for fact in productFacts
        if isinstance(fact, dict) and fact.get("normalized_value")
    }


def NormalizeComparisonText(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def main(arguments: list[str] | None = None) -> int:
    parsedArguments = ParseArguments(arguments)
    appConfig = LoadAppConfig(PROJECT_ROOT_PATH)
    reconstructionProfile = RECONSTRUCTION_PROFILES[
        parsedArguments.reconstruction_model
    ]
    smokeConfig = appConfig.kurly_smoke
    productUrls = list(parsedArguments.product_urls or smokeConfig.product_urls)
    if not productUrls and not parsedArguments.preflight_only:
        print(
            "실행할 URL이 없습니다. --url 또는 "
            ".appconfig.ocr_reconstruction_smoke.toml을 설정하세요."
        )
        return 1
    maxImageCount = (
        smokeConfig.max_ocr_image_count
        if parsedArguments.max_images is None
        else parsedArguments.max_images
    )
    runRootPath = (
        parsedArguments.artifact_root
        / datetime.now().strftime("%Y%m%dT%H%M%S")
    )
    try:
        RunPreflight(appConfig, reconstructionProfile)
    except Exception as error:  # noqa: BLE001 - CLI 연결 오류를 보고한다.
        print(f"preflight 실패: {type(error).__name__}: {error}")
        return 2
    if parsedArguments.preflight_only:
        print("preflight 완료")
        return 0

    runRootPath.mkdir(parents=True, exist_ok=True)
    try:
        routes = BuildSmokeRoutes(
            appConfig,
            runRootPath,
            reconstructionProfile,
        )
        screeningOcrEngine = CachedScreeningOcrEngine(PaddleOcrEngine())
    except Exception as error:  # noqa: BLE001 - CLI 설정/모델 오류를 보고한다.
        print(f"smoke 초기화 실패: {type(error).__name__}: {error}")
        return 2

    collector = BuildCollector(appConfig, parsedArguments.headed)
    productResults: list[dict[str, object]] = []
    for productIndex, productUrl in enumerate(productUrls, start=1):
        print(f"[{productIndex}/{len(productUrls)}] {productUrl}")
        try:
            collectionStartedAt = perf_counter()
            collector.ValidateProductPageUrl(productUrl)
            renderedEvidence = collector.CollectRenderedPageEvidence(productUrl)
            collectionResult = collector.BuildCollectionResult(renderedEvidence)
            collectionElapsedSeconds = perf_counter() - collectionStartedAt
            imageUrls = (
                collectionResult.ocrCandidateImageUrls
                or collectionResult.productDetailImageUrls
            )
            if maxImageCount:
                imageUrls = imageUrls[:maxImageCount]
            imagesByUrl, downloadErrors, downloadElapsedSeconds = DownloadImages(
                imageUrls,
                smokeConfig.timeout_seconds,
            )
            if not imagesByUrl:
                raise RuntimeError(
                    "비교 가능한 상품 상세 이미지를 "
                    "다운로드하지 못했습니다."
                )
            sharedImagePathsByUrl, sharedInputManifestPath = (
                WriteSharedInputArtifacts(
                    runRootPath,
                    collectionResult.productPageUrl,
                    imagesByUrl,
                )
            )
            screeningOcrEngine.Clear()
            screeningElapsedSeconds = screeningOcrEngine.Prime(
                list(imagesByUrl.values()),
            )
            routeResults = [
                RunRoute(
                    route,
                    collectionResult,
                    imagesByUrl,
                    sharedImagePathsByUrl,
                    sharedInputManifestPath,
                    screeningOcrEngine,
                    runRootPath,
                    smokeConfig.timeout_seconds,
                    parsedArguments.vlm_input_mode,
                )
                for route in routes
            ]
            productResult = {
                "product_url": productUrl,
                "product_id": ExtractProductIdFromUrl(productUrl),
                "collection_seconds": round(collectionElapsedSeconds, 3),
                "download_seconds": round(downloadElapsedSeconds, 3),
                "downloaded_image_count": len(imagesByUrl),
                "downloaded_image_bytes": sum(map(len, imagesByUrl.values())),
                "download_errors": downloadErrors,
                "vlm_input_mode": parsedArguments.vlm_input_mode,
                "shared_input_manifest_path": str(sharedInputManifestPath),
                "shared_screening_ocr_seconds": round(
                    screeningElapsedSeconds,
                    3,
                ),
                "routes": routeResults,
                "comparison": BuildComparison(routeResults),
            }
            productResults.append(productResult)
            for routeResult in routeResults:
                ocrMetrics = routeResult["ocr_metrics"]
                directVlmMetrics = routeResult["vlm_direct_metrics"]
                reconstruction = routeResult["reconstruction"]
                assert isinstance(ocrMetrics, dict)
                assert isinstance(directVlmMetrics, dict)
                assert isinstance(reconstruction, dict)
                print(
                    "  vlm={0} ocr={1}s direct_raw_chars={2} tables={3} "
                    "image_payload_bytes={4}".format(
                        routeResult["route"],
                        ocrMetrics["wall_seconds"],
                        directVlmMetrics["raw_text_characters"],
                        directVlmMetrics["table_count"],
                        ocrMetrics["runtime"]["image_bytes"],
                    )
                )
                metrics = reconstruction.get("metrics")
                metrics = metrics if isinstance(metrics, dict) else {}
                print(
                    "    reconstruction={0} status={1} time={2}s facts={3}".format(
                        reconstruction.get("reconstruction_profile"),
                        "error" if reconstruction.get("error") else "ok",
                        metrics.get("wall_seconds", "?"),
                        metrics.get("product_fact_count", "?"),
                    )
                )
            comparison = productResult["comparison"]
            assert isinstance(comparison, dict)
            directComparison = comparison["vlm_direct"]
            tableComparison = comparison["table_restoration"]
            reconstructionComparison = comparison["reconstruction_4x1"]
            assert isinstance(directComparison, dict)
            assert isinstance(tableComparison, dict)
            assert isinstance(reconstructionComparison, dict)
            print(
                "  reconstruction_cells={0}/{1} profile={2} "
                "same_vlm_input={3}".format(
                    reconstructionComparison["successful_cell_count"],
                    reconstructionComparison["expected_cell_count"],
                    reconstructionComparison["selected_reconstruction_profile"],
                    directComparison["same_input_for_all_routes"],
                )
            )
            tableRoutes = tableComparison["routes"]
            assert isinstance(tableRoutes, dict)
            for routeName, tableMetrics in tableRoutes.items():
                if not isinstance(tableMetrics, dict):
                    continue
                print(
                    "  table_restoration vlm={0} detected={1} comparable={2} "
                    "structure_match={3} cell_f1={4} eligibility={5}".format(
                        routeName,
                        tableMetrics["detected_table_count"],
                        tableMetrics["pp_structure_comparable_table_count"],
                        tableMetrics["structure_exact_match_rate"],
                        tableMetrics["mean_cell_f1"],
                        tableMetrics["pp_agreement_metric_eligibility"],
                    )
                )
            for pairwiseResult in directComparison["pairwise_raw_text_similarity"]:
                print(
                    "  vlm_raw_text_similarity {0}={1}".format(
                        " vs ".join(pairwiseResult["routes"]),
                        pairwiseResult["similarity_ratio"],
                    )
                )
        except Exception as error:  # noqa: BLE001 - 다음 URL smoke를 계속한다.
            productResults.append(
                {
                    "product_url": productUrl,
                    "product_id": ExtractProductIdFromUrl(productUrl),
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            print(f"  실패: {type(error).__name__}: {error}")

    summaryPath = parsedArguments.summary_path or runRootPath / "summary.json"
    summaryPath.parent.mkdir(parents=True, exist_ok=True)
    summaryPath.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),
                "selected_reconstruction_model": (
                    parsedArguments.reconstruction_model
                ),
                "vlm_input_mode": parsedArguments.vlm_input_mode,
                "reconstruction_pipeline_step": {
                    "pipeline": "KurlyUrlIntakePipeline",
                    "step": "reconstruct_product_input",
                    "component": "ProductInputReconstructionService",
                    "method": "ReconstructFromPipelineParts",
                },
                "comparison_contract": (
                    "PaddleOCR-VL, GPT, Gemini, and Claude restore tables from the "
                    "same input mode. pipeline uses the production ROI; full_image "
                    "is a separate preprocessing-loss control. PP-Structure first "
                    "localizes table regions, then each VLM extracts ordered "
                    "key/value/source_text rows from those regions. VLM rows are "
                    "compared with PaddleOCR TableRecognitionPipelineV2 evidence "
                    "for diagnostics only. "
                    "Each merged VLM evidence is then passed unchanged to the "
                    "ProductInputReconstructionService.ReconstructFromPipelineParts "
                    "method using one fixed reconstruction profile per run. "
                    "PP-Structure agreement is not ground-truth accuracy or a "
                    "cross-provider ranking."
                ),
                "products": productResults,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"summary -> {summaryPath}")
    return 0 if any("routes" in result for result in productResults) else 1


if __name__ == "__main__":
    raise SystemExit(main())
