"""Product detail image OCR fallback runner."""

import json
import re
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from bussiness_logic.artifact_paths import ExtractProductIdFromUrl
from bussiness_logic.product.ocr.download_execution import (
    DownloadExecutionCoordinator,
    SHARED_DOWNLOAD_EXECUTION_COORDINATOR,
)
from bussiness_logic.product.ocr.ocr_execution import (
    OcrExecutionCoordinator,
    SHARED_OCR_EXECUTION_COORDINATOR,
)
from bussiness_logic.product.ocr.paddle_ocr import (
    BuildOcrRegionCrop,
    BuildTableGroundingDiagnostics,
    ProductOcrEngine,
    ProductOcrTextRegion,
    ProductOcrTileTextResult,
    ProductStructuredOcrResult,
)


DEFAULT_PRODUCT_OCR_IMAGE_ARTIFACT_ROOT_PATH = (
    Path("artifacts") / "product-ocr-fallback"
)
DEFAULT_PRODUCT_OCR_IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 30
DEFAULT_PRODUCT_OCR_IMAGE_DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
OCR_SCREENING_FOOD_DETAIL_LABELS = (
    "제품명",
    "식품유형",
    "식품의 유형",
    "원재료",
    "원료명",
    "내용량",
    "보관방법",
    "소비기한",
    "유통기한",
    "품목보고",
    "포장재질",
)
OCR_SCREENING_NUTRITION_LABELS = (
    "영양정보",
    "영양성분",
    "나트륨",
    "탄수화물",
    "당류",
    "지방",
    "트랜스지방",
    "포화지방",
    "콜레스테롤",
    "단백질",
)
OCR_SCREENING_STRUCTURED_LABELS = (
    *OCR_SCREENING_FOOD_DETAIL_LABELS,
    *OCR_SCREENING_NUTRITION_LABELS,
)
OCR_SCREENING_QUANTITY_PATTERN = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:mg|g|kg|ml|l|%|kcal|㎎|㎏|㎖)",
    re.IGNORECASE,
)

OcrImageStatusCallback = Callable[[int, str, str, str, str], None]


@dataclass(frozen=True)
class _DownloadedOcrImage:
    imageIndex: int
    imageUrl: str
    imageBytes: bytes | None
    artifactPath: Path | None
    processingTimes: Dict[str, float]
    error: str | None = None


class ProductOcrImageResult(BaseModel):
    """OCR fallback 대상 이미지 하나의 처리 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    imageUrl: str = Field(alias="image_url")
    imageIndex: Optional[int] = Field(default=None, alias="image_index")
    imagePath: Optional[str] = Field(default=None, alias="image_path")
    imagePaths: List[str] = Field(default_factory=list, alias="image_paths")
    ocrText: str = Field(default="", alias="ocr_text")
    structuredOcr: ProductStructuredOcrResult = Field(
        default_factory=ProductStructuredOcrResult,
        alias="structured_ocr",
    )
    tableEvidenceArtifactPath: Optional[str] = Field(
        default=None,
        alias="table_evidence_artifact_path",
    )
    processingTimes: Dict[str, float] = Field(
        default_factory=dict,
        alias="processing_times",
    )
    skippedReason: Optional[str] = Field(default=None, alias="skipped_reason")
    error: Optional[str] = None


class ProductOcrTextQualityEvaluator:
    """숫자/기호만 남은 OCR 결과를 artifact 보존 대상에서 제외한다."""

    def __init__(self, minimumMeaningfulCharacterCount: int = 3) -> None:
        self._minimumMeaningfulCharacterCount = max(
            1,
            minimumMeaningfulCharacterCount,
        )
        self._meaningfulCharacterPattern = re.compile(r"[A-Za-z가-힣]")

    def HasInformativeResult(
        self,
        structuredOcrResult: ProductStructuredOcrResult,
    ) -> bool:
        return any(
            self.HasInformativeText(text)
            for text in [
                structuredOcrResult.structuredText,
                structuredOcrResult.rawText,
            ]
        )

    def HasInformativeTile(
        self,
        structuredOcrResult: ProductStructuredOcrResult,
        tileIndex: Optional[int],
    ) -> bool:
        return any(
            self.HasInformativeText(text)
            for text in [
                self._BuildTileTableText(structuredOcrResult, tileIndex),
                self._BuildTileRawText(structuredOcrResult, tileIndex),
            ]
        )

    def HasInformativeText(self, text: str) -> bool:
        meaningfulCharacters = self._meaningfulCharacterPattern.findall(text or "")
        return len(meaningfulCharacters) >= self._minimumMeaningfulCharacterCount

    def _BuildTileTableText(
        self,
        structuredOcrResult: ProductStructuredOcrResult,
        tileIndex: Optional[int],
    ) -> str:
        return "\n".join(
            table.plainText
            for table in structuredOcrResult.tables
            if table.tileIndex == tileIndex
        )

    def _BuildTileRawText(
        self,
        structuredOcrResult: ProductStructuredOcrResult,
        tileIndex: Optional[int],
    ) -> str:
        return "\n".join(
            rawTileText.text
            for rawTileText in structuredOcrResult.rawTileTexts
            if rawTileText.tileIndex == tileIndex
        )


class ProductOcrImageDownloader:
    """OCR 대상 이미지 bytes를 가져오는 입력 adapter."""

    def __init__(
        self,
        downloadUserAgent: str = DEFAULT_PRODUCT_OCR_IMAGE_DOWNLOAD_USER_AGENT,
    ) -> None:
        self._downloadUserAgent = downloadUserAgent

    def Download(self, imageUrl: str, downloadTimeoutSeconds: int) -> bytes:
        request = Request(
            imageUrl,
            headers={"User-Agent": self._downloadUserAgent},
        )
        with urlopen(request, timeout=downloadTimeoutSeconds) as response:
            return response.read()


class ProductOcrArtifactStore:
    """OCR 입력 이미지 artifact 저장과 정리를 담당한다."""

    def PrepareArtifactDirectory(
        self,
        artifactRootPath: Path,
        productPageUrl: str,
        preserveInputImages: bool = False,
    ) -> Path:
        artifactDirectory = self._BuildArtifactDirectory(
            artifactRootPath,
            productPageUrl,
        )
        artifactDirectory.mkdir(parents=True, exist_ok=True)
        for artifactPath in artifactDirectory.glob("ocr-fallback-image-*"):
            if preserveInputImages:
                continue
            if artifactPath.is_file():
                artifactPath.unlink(missing_ok=True)
        for artifactPath in artifactDirectory.glob("ocr-table-evidence-*.json"):
            if artifactPath.is_file():
                artifactPath.unlink(missing_ok=True)
        return artifactDirectory

    def ReadReusableImage(
        self,
        artifactDirectory: Path,
        imageIndex: int,
    ) -> Optional[Tuple[Path, bytes]]:
        imageStem = "ocr-fallback-image-{0:02d}".format(imageIndex)
        for artifactPath in sorted(artifactDirectory.glob(f"{imageStem}.*")):
            if artifactPath.stem == imageStem and artifactPath.is_file():
                return artifactPath, artifactPath.read_bytes()
        return None

    def PruneUnretainedArtifacts(
        self,
        artifactDirectory: Path,
        retainedArtifactPaths: Sequence[Path],
    ) -> int:
        retainedPaths = {
            artifactPath.resolve()
            for artifactPath in retainedArtifactPaths
            if artifactPath.exists()
        }
        deletedCount = 0
        for artifactPath in artifactDirectory.glob("ocr-fallback-image-*"):
            if (
                artifactPath.is_file()
                and artifactPath.resolve() not in retainedPaths
            ):
                artifactPath.unlink(missing_ok=True)
                deletedCount += 1
        return deletedCount

    def WriteImage(
        self,
        artifactDirectory: Path,
        imageIndex: int,
        imageUrl: str,
        imageBytes: bytes,
    ) -> Path:
        artifactPath = artifactDirectory / self._BuildImageFileName(
            imageIndex,
            imageUrl,
        )
        artifactPath.write_bytes(imageBytes)
        return artifactPath

    def WriteTableEvidence(
        self,
        artifactDirectory: Path,
        imageIndex: int,
        imageUrl: str,
        structuredOcrResult: ProductStructuredOcrResult,
    ) -> Optional[Path]:
        if (
            not structuredOcrResult.tables
            and not structuredOcrResult.tableCandidates
            and not structuredOcrResult.layoutDiagnostics
            and not structuredOcrResult.tableGroundingDiagnostics
        ):
            return None

        artifactPath = artifactDirectory / (
            "ocr-table-evidence-{0:02d}.json".format(imageIndex)
        )
        artifactPath.write_text(
            json.dumps(
                {
                    "image_index": imageIndex,
                    "image_url": imageUrl,
                    "tables": [
                        table.model_dump(mode="json", by_alias=True)
                        for table in structuredOcrResult.tables
                    ],
                    "table_candidates": [
                        candidate.model_dump(mode="json", by_alias=True)
                        for candidate in structuredOcrResult.tableCandidates
                    ],
                    "layout_diagnostics": [
                        diagnostic.model_dump(mode="json", by_alias=True)
                        for diagnostic in structuredOcrResult.layoutDiagnostics
                    ],
                    "table_grounding_diagnostics": [
                        diagnostic.model_dump(mode="json", by_alias=True)
                        for diagnostic in (
                            structuredOcrResult.tableGroundingDiagnostics
                        )
                    ],
                    "warnings": structuredOcrResult.warnings,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return artifactPath

    def ReplaceImageWithInformativeTiles(
        self,
        artifactPath: Path,
        imageIndex: int,
        imageUrl: str,
        imageTiles: Sequence[Tuple[Optional[int], bytes]],
        structuredOcrResult: ProductStructuredOcrResult,
        textQualityEvaluator: ProductOcrTextQualityEvaluator,
    ) -> List[Path]:
        if len(imageTiles) == 1 and imageTiles[0][0] is None:
            if textQualityEvaluator.HasInformativeResult(structuredOcrResult):
                return [artifactPath]
            return []

        retainedTilePaths: List[Path] = []
        artifactDirectory = artifactPath.parent
        for tileIndex, tileBytes in imageTiles:
            if not textQualityEvaluator.HasInformativeTile(
                structuredOcrResult,
                tileIndex,
            ):
                continue
            retainedTilePaths.append(
                self.WriteTileImage(
                    artifactDirectory=artifactDirectory,
                    imageIndex=imageIndex,
                    imageUrl=imageUrl,
                    tileIndex=tileIndex,
                    imageBytes=tileBytes,
                )
            )
        return retainedTilePaths

    def WriteTileImage(
        self,
        artifactDirectory: Path,
        imageIndex: int,
        imageUrl: str,
        tileIndex: Optional[int],
        imageBytes: bytes,
    ) -> Path:
        tilePath = artifactDirectory / self._BuildTileImageFileName(
            imageIndex,
            imageUrl,
            tileIndex,
        )
        tilePath.write_bytes(imageBytes)
        return tilePath

    def _BuildArtifactDirectory(
        self,
        artifactRootPath: Path,
        productPageUrl: str,
    ) -> Path:
        return artifactRootPath / ExtractProductIdFromUrl(productPageUrl)

    def _BuildImageFileName(self, imageIndex: int, imageUrl: str) -> str:
        parsedUrl = urlparse(imageUrl)
        suffix = Path(parsedUrl.path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".img"
        return "ocr-fallback-image-{0:02d}{1}".format(imageIndex, suffix)

    def _BuildTileImageFileName(
        self,
        imageIndex: int,
        imageUrl: str,
        tileIndex: Optional[int],
    ) -> str:
        tileNumber = tileIndex if tileIndex is not None else 1
        return "ocr-fallback-image-{0:02d}-tile-{1:02d}.jpg".format(
            imageIndex,
            tileNumber,
        )

    def _BuildSafePathName(self, value: str) -> str:
        safeValue = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-")
        return safeValue or "unknown"


class ProductOcrFallbackRunner:
    """상품 상세 이미지 다운로드, artifact 저장, OCR 실행을 담당한다."""

    def __init__(
        self,
        ocrEngine: ProductOcrEngine,
        downloadUserAgent: str = DEFAULT_PRODUCT_OCR_IMAGE_DOWNLOAD_USER_AGENT,
        imageDownloader: Optional[ProductOcrImageDownloader] = None,
        artifactStore: Optional[ProductOcrArtifactStore] = None,
        textQualityEvaluator: Optional[ProductOcrTextQualityEvaluator] = None,
        screeningEngine: Optional[ProductOcrEngine] = None,
        useStructuredOcrRegionCrop: bool = True,
        enableTableGroundingDiagnostic: bool = False,
        ocrExecutionCoordinator: Optional[OcrExecutionCoordinator] = None,
        downloadExecutionCoordinator: Optional[
            DownloadExecutionCoordinator
        ] = None,
        maxPendingOcrImages: int = 4,
    ) -> None:
        if maxPendingOcrImages < 1:
            raise ValueError("maxPendingOcrImages must be at least 1")
        self._ocrEngine = ocrEngine
        self._screeningEngine = screeningEngine
        self._imageDownloader = imageDownloader or ProductOcrImageDownloader(
            downloadUserAgent,
        )
        self._artifactStore = artifactStore or ProductOcrArtifactStore()
        self._textQualityEvaluator = (
            textQualityEvaluator or ProductOcrTextQualityEvaluator()
        )
        self._useStructuredOcrRegionCrop = useStructuredOcrRegionCrop
        self._enableTableGroundingDiagnostic = enableTableGroundingDiagnostic
        self._ocrExecutionCoordinator = (
            ocrExecutionCoordinator or SHARED_OCR_EXECUTION_COORDINATOR
        )
        self._downloadExecutionCoordinator = (
            downloadExecutionCoordinator or SHARED_DOWNLOAD_EXECUTION_COORDINATOR
        )
        self._maxPendingOcrImages = min(
            maxPendingOcrImages,
            self._downloadExecutionCoordinator.maxInFlight,
        )

    def Run(
        self,
        imageUrls: List[str],
        artifactRootPath: Path,
        productPageUrl: str,
        maxImageCount: int,
        downloadTimeoutSeconds: int,
        reuseArtifactImages: bool = False,
        imageStatusCallback: OcrImageStatusCallback | None = None,
    ) -> List[ProductOcrImageResult]:
        artifactDirectory = self._artifactStore.PrepareArtifactDirectory(
            artifactRootPath=artifactRootPath,
            productPageUrl=productPageUrl,
            preserveInputImages=reuseArtifactImages,
        )

        selectedImageUrls = imageUrls[: max(0, maxImageCount)]
        selectedImages = iter(enumerate(selectedImageUrls, start=1))
        pendingDownloads: Dict[Future[_DownloadedOcrImage], int] = {}

        def SubmitUntilFull() -> None:
            while len(pendingDownloads) < self._maxPendingOcrImages:
                try:
                    imageIndex, imageUrl = next(selectedImages)
                except StopIteration:
                    return
                future = self._downloadExecutionCoordinator.Submit(
                    lambda imageIndex=imageIndex, imageUrl=imageUrl: (
                        self._DownloadImage(
                            imageIndex=imageIndex,
                            imageUrl=imageUrl,
                            artifactDirectory=artifactDirectory,
                            downloadTimeoutSeconds=downloadTimeoutSeconds,
                            reuseArtifactImages=reuseArtifactImages,
                        )
                    )
                )
                pendingDownloads[future] = imageIndex

        SubmitUntilFull()
        imageResultsByIndex: Dict[int, ProductOcrImageResult] = {}
        while pendingDownloads:
            completedFutures, _ = wait(
                tuple(pendingDownloads),
                return_when=FIRST_COMPLETED,
            )
            completedFuture = min(
                completedFutures,
                key=lambda future: pendingDownloads[future],
            )
            pendingDownloads.pop(completedFuture)
            downloadedImage = completedFuture.result()
            SubmitUntilFull()
            imageResult = (
                ProductOcrImageResult(
                    imageUrl=downloadedImage.imageUrl,
                    imageIndex=downloadedImage.imageIndex,
                    processingTimes={
                        key: round(value, 3)
                        for key, value in downloadedImage.processingTimes.items()
                    },
                    error=downloadedImage.error,
                )
                if downloadedImage.error is not None
                else self._ExtractDownloadedImageText(
                    downloadedImage=downloadedImage,
                    imageStatusCallback=imageStatusCallback,
                )
            )
            imageResultsByIndex[downloadedImage.imageIndex] = imageResult

        imageResults = [
            imageResultsByIndex[imageIndex]
            for imageIndex in range(1, len(selectedImageUrls) + 1)
        ]
        for imageIndex, imageResult in enumerate(imageResults, start=1):
            self._NotifyImageStatus(
                imageStatusCallback,
                imageIndex,
                imageResult.imageUrl,
                (
                    "failed"
                    if imageResult.error
                    else "rejected"
                    if imageResult.skippedReason
                    else "extracted"
                ),
                imageResult.skippedReason or "",
                imageResult.error or "",
            )

        self._artifactStore.PruneUnretainedArtifacts(
            artifactDirectory,
            [
                Path(imagePath)
                for imageResult in imageResults
                for imagePath in (
                    imageResult.imagePaths
                    or (
                        [imageResult.imagePath]
                        if imageResult.imagePath is not None
                        else []
                    )
                )
            ],
        )
        return imageResults

    @staticmethod
    def BuildCombinedOcrText(imageResults: List[ProductOcrImageResult]) -> str:
        return "\n".join(
            imageResult.ocrText
            for imageResult in imageResults
            if imageResult.ocrText
        )

    def _DownloadImage(
        self,
        *,
        imageIndex: int,
        imageUrl: str,
        artifactDirectory: Path,
        downloadTimeoutSeconds: int,
        reuseArtifactImages: bool,
    ) -> _DownloadedOcrImage:
        artifactPath: Optional[Path] = None
        processingTimes: Dict[str, float] = {}
        try:
            reusableImage = (
                self._artifactStore.ReadReusableImage(
                    artifactDirectory,
                    imageIndex,
                )
                if reuseArtifactImages
                else None
            )
            if reusableImage is None:
                startedAt = perf_counter()
                imageBytes = self._imageDownloader.Download(
                    imageUrl,
                    downloadTimeoutSeconds,
                )
                processingTimes["download"] = perf_counter() - startedAt
                artifactPath = self._artifactStore.WriteImage(
                    artifactDirectory=artifactDirectory,
                    imageIndex=imageIndex,
                    imageUrl=imageUrl,
                    imageBytes=imageBytes,
                )
            else:
                artifactPath, imageBytes = reusableImage
                processingTimes["cached_image_read"] = 0.0
            return _DownloadedOcrImage(
                imageIndex=imageIndex,
                imageUrl=imageUrl,
                imageBytes=imageBytes,
                artifactPath=artifactPath,
                processingTimes=processingTimes,
            )
        except Exception as error:
            return _DownloadedOcrImage(
                imageIndex=imageIndex,
                imageUrl=imageUrl,
                imageBytes=None,
                artifactPath=None,
                processingTimes=processingTimes,
                error="OCR fallback failed for image {0}: {1}".format(
                    imageUrl,
                    error,
                ),
            )

    def _ExtractDownloadedImageText(
        self,
        *,
        downloadedImage: _DownloadedOcrImage,
        imageStatusCallback: OcrImageStatusCallback | None,
    ) -> ProductOcrImageResult:
        imageIndex = downloadedImage.imageIndex
        imageUrl = downloadedImage.imageUrl
        imageBytes = downloadedImage.imageBytes
        artifactPath = downloadedImage.artifactPath
        processingTimes = dict(downloadedImage.processingTimes)
        if imageBytes is None or artifactPath is None:
            raise ValueError("downloaded image bytes and artifact path are required")
        artifactDirectory = artifactPath.parent
        try:
            structuredOcrResult, screeningResult = (
                self._ocrExecutionCoordinator.Execute(
                    lambda: self._RunOcrEngines(
                        imageBytes=imageBytes,
                        imageIndex=imageIndex,
                        imageUrl=imageUrl,
                        processingTimes=processingTimes,
                        imageStatusCallback=imageStatusCallback,
                    )
                )
            )
            tableEvidenceArtifactPath = self._artifactStore.WriteTableEvidence(
                artifactDirectory=artifactDirectory,
                imageIndex=imageIndex,
                imageUrl=imageUrl,
                structuredOcrResult=structuredOcrResult,
            )
            ocrText = structuredOcrResult.text
            if (
                not isinstance(ocrText, str)
                or not self._textQualityEvaluator.HasInformativeResult(
                    structuredOcrResult,
                )
            ):
                return ProductOcrImageResult(
                    imageUrl=imageUrl,
                    imageIndex=imageIndex,
                    imagePath=str(artifactPath) if artifactPath is not None else None,
                    ocrText=ocrText if isinstance(ocrText, str) else "",
                    structuredOcr=structuredOcrResult,
                    tableEvidenceArtifactPath=(
                        str(tableEvidenceArtifactPath)
                        if tableEvidenceArtifactPath is not None
                        else None
                    ),
                    processingTimes={
                        key: round(value, 3)
                        for key, value in processingTimes.items()
                    },
                    skippedReason="non_informative_ocr_result",
                )
            imageTiles = (
                [(None, imageBytes)]
                if screeningResult is not None
                else self._ocrExecutionCoordinator.Execute(
                    lambda: self._ocrEngine.BuildArtifactImageTiles(imageBytes)
                )
            )
            artifactPaths = self._artifactStore.ReplaceImageWithInformativeTiles(
                artifactPath=artifactPath,
                imageIndex=imageIndex,
                imageUrl=imageUrl,
                imageTiles=imageTiles,
                structuredOcrResult=structuredOcrResult,
                textQualityEvaluator=self._textQualityEvaluator,
            )
            if not artifactPaths:
                return ProductOcrImageResult(
                    imageUrl=imageUrl,
                    imageIndex=imageIndex,
                    imagePath=str(artifactPath) if artifactPath is not None else None,
                    ocrText=ocrText,
                    structuredOcr=structuredOcrResult,
                    tableEvidenceArtifactPath=(
                        str(tableEvidenceArtifactPath)
                        if tableEvidenceArtifactPath is not None
                        else None
                    ),
                    processingTimes={
                        key: round(value, 3)
                        for key, value in processingTimes.items()
                    },
                    skippedReason="no_informative_artifact_tiles",
                )
            return ProductOcrImageResult(
                imageUrl=imageUrl,
                imageIndex=imageIndex,
                imagePath=str(artifactPaths[0]),
                imagePaths=[str(path) for path in artifactPaths],
                ocrText=ocrText,
                structuredOcr=structuredOcrResult,
                tableEvidenceArtifactPath=(
                    str(tableEvidenceArtifactPath)
                    if tableEvidenceArtifactPath is not None
                    else None
                ),
                processingTimes={
                    key: round(value, 3)
                    for key, value in processingTimes.items()
                },
            )
        except Exception as error:
            return ProductOcrImageResult(
                imageUrl=imageUrl,
                imageIndex=imageIndex,
                processingTimes={
                    key: round(value, 3)
                    for key, value in processingTimes.items()
                },
                error="OCR fallback failed for image {0}: {1}".format(
                    imageUrl,
                    error,
                ),
            )

    def _RunOcrEngines(
        self,
        *,
        imageBytes: bytes,
        imageIndex: int,
        imageUrl: str,
        processingTimes: Dict[str, float],
        imageStatusCallback: OcrImageStatusCallback | None,
    ) -> Tuple[ProductStructuredOcrResult, Optional[ProductStructuredOcrResult]]:
        screeningResult: Optional[ProductStructuredOcrResult] = None
        screeningRegions: List[ProductOcrTextRegion] = []
        if self._screeningEngine is not None:
            try:
                startedAt = perf_counter()
                screeningResult, screeningRegions = (
                    self._screeningEngine.ExtractStructuredTextWithRegionsFromImage(
                        imageBytes,
                    )
                )
                processingTimes["raw_ocr"] = perf_counter() - startedAt
            except Exception:
                screeningResult = None

        shouldRunStructuredOcr, screeningSummary = (
            self._EvaluateStructuredOcrCandidate(
                screeningResult.text if screeningResult is not None else "",
            )
        )
        if screeningResult is not None and not shouldRunStructuredOcr:
            structuredOcrResult = screeningResult.model_copy(
                update={
                    "fallbackReason": None,
                    "textMergeMode": "screened_raw_only",
                    "warnings": [
                        *screeningResult.warnings,
                        "structured_ocr_skipped_by_screening {0}".format(
                            screeningSummary,
                        ),
                    ],
                }
            )
        else:
            structuredInputBytes = imageBytes
            roiBounds: Optional[Tuple[int, int, int, int]] = None
            if screeningRegions and self._useStructuredOcrRegionCrop:
                startedAt = perf_counter()
                regionCrop = BuildOcrRegionCrop(
                    imageBytes,
                    screeningRegions,
                    OCR_SCREENING_STRUCTURED_LABELS,
                )
                processingTimes["roi_build"] = perf_counter() - startedAt
                if regionCrop is not None:
                    structuredInputBytes, roiBounds = regionCrop
            self._NotifyImageStatus(
                imageStatusCallback,
                imageIndex,
                imageUrl,
                "vlm-processing",
                "",
                "",
            )
            startedAt = perf_counter()
            structuredOcrResult = self._ocrEngine.ExtractStructuredTextFromImage(
                structuredInputBytes,
            )
            processingTimes["structured_ocr"] = perf_counter() - startedAt
            if screeningResult is not None:
                structuredOcrResult = self._MergeStructuredAndScreeningOcr(
                    structuredOcrResult,
                    screeningResult,
                    screeningSummary=screeningSummary,
                    roiBounds=roiBounds,
                )
        if (
            self._enableTableGroundingDiagnostic
            and structuredOcrResult.tableCandidates
        ):
            structuredOcrResult = structuredOcrResult.model_copy(
                update={
                    "tableGroundingDiagnostics": BuildTableGroundingDiagnostics(
                        structuredOcrResult.tableCandidates,
                        screeningRegions,
                    )
                }
            )
        return structuredOcrResult, screeningResult

    @staticmethod
    def _NotifyImageStatus(
        callback: OcrImageStatusCallback | None,
        imageIndex: int,
        imageUrl: str,
        status: str,
        rejectionReason: str,
        failureReason: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(
                imageIndex,
                imageUrl,
                status,
                rejectionReason,
                failureReason,
            )
        except Exception:
            # 진행 알림 실패가 OCR 결과를 실패로 바꾸면 안 된다.
            pass

    def _MergeStructuredAndScreeningOcr(
        self,
        structuredResult: ProductStructuredOcrResult,
        screeningResult: ProductStructuredOcrResult,
        *,
        screeningSummary: str,
        roiBounds: Optional[Tuple[int, int, int, int]],
    ) -> ProductStructuredOcrResult:
        rawText = screeningResult.rawText or screeningResult.text
        rawTileTexts = (
            screeningResult.rawTileTexts
            or [ProductOcrTileTextResult(text=rawText)]
            if rawText
            else []
        )
        roiWarning = (
            "structured_ocr_full_image_requested"
            if not self._useStructuredOcrRegionCrop
            else "structured_ocr_roi_applied bounds={0},{1},{2},{3}".format(
                *roiBounds,
            )
            if roiBounds is not None
            else "structured_ocr_roi_unavailable"
        )
        warnings = list(
            dict.fromkeys(
                [
                    *screeningResult.warnings,
                    *structuredResult.warnings,
                    "structured_ocr_screening {0}".format(screeningSummary),
                    roiWarning,
                ]
            )
        )
        if not structuredResult.usedStructuredTables:
            return screeningResult.model_copy(
                update={
                    "fallbackReason": structuredResult.fallbackReason,
                    "textMergeMode": "screened_raw_only",
                    "tables": [],
                    "tableCandidates": structuredResult.tableCandidates,
                    "layoutDiagnostics": structuredResult.layoutDiagnostics,
                    "warnings": warnings,
                }
            )

        mergedText = structuredResult.structuredText
        if rawText:
            mergedText = "[structured_tables]\n{0}\n\n[raw_ocr_tiles]\n{1}".format(
                structuredResult.structuredText,
                rawText,
            )
        return structuredResult.model_copy(
            update={
                "text": mergedText,
                "rawText": rawText,
                "rawTileTexts": rawTileTexts,
                "textMergeMode": "structured_plus_screening_raw",
                "warnings": warnings,
            }
        )

    def _EvaluateStructuredOcrCandidate(
        self,
        text: str,
    ) -> Tuple[bool, str]:
        normalizedText = re.sub(r"\s+", "", (text or "").lower())
        meaningfulCharacterCount = len(re.findall(r"[A-Za-z가-힣]", normalizedText))
        nutritionMatchCount = sum(
            label.replace(" ", "") in normalizedText
            for label in OCR_SCREENING_NUTRITION_LABELS
        )
        foodDetailMatchCount = sum(
            label.replace(" ", "") in normalizedText
            for label in OCR_SCREENING_FOOD_DETAIL_LABELS
        )
        quantityMatchCount = len(
            OCR_SCREENING_QUANTITY_PATTERN.findall(text or "")
        )
        isCandidate = meaningfulCharacterCount >= 40 and (
            (
                nutritionMatchCount >= 3
                and quantityMatchCount >= 3
            )
            or (
                nutritionMatchCount >= 2
                and foodDetailMatchCount >= 3
                and quantityMatchCount >= 4
            )
            or (
                foodDetailMatchCount >= 5
                and quantityMatchCount >= 3
            )
        )
        return isCandidate, (
            "nutrition_labels={0} food_detail_labels={1} "
            "quantities={2} meaningful_characters={3}"
        ).format(
            nutritionMatchCount,
            foodDetailMatchCount,
            quantityMatchCount,
            meaningfulCharacterCount,
        )
