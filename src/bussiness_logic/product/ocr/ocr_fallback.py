"""Product detail image OCR fallback runner."""

import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from bussiness_logic.product.ocr.paddle_ocr import (
    ProductOcrEngine,
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


class ProductOcrImageResult(BaseModel):
    """OCR fallback 대상 이미지 하나의 처리 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    imageUrl: str = Field(alias="image_url")
    imagePath: Optional[str] = Field(default=None, alias="image_path")
    imagePaths: List[str] = Field(default_factory=list, alias="image_paths")
    ocrText: str = Field(default="", alias="ocr_text")
    structuredOcr: ProductStructuredOcrResult = Field(
        default_factory=ProductStructuredOcrResult,
        alias="structured_ocr",
    )
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
    ) -> Path:
        artifactDirectory = self._BuildArtifactDirectory(
            artifactRootPath,
            productPageUrl,
        )
        artifactDirectory.mkdir(parents=True, exist_ok=True)
        for artifactPath in artifactDirectory.glob("ocr-fallback-image-*"):
            if artifactPath.is_file():
                artifactPath.unlink(missing_ok=True)
        return artifactDirectory

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
            self.DeleteImage(artifactPath)
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
        self.DeleteImage(artifactPath)
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

    def DeleteImage(self, artifactPath: Optional[Path]) -> None:
        if artifactPath is not None:
            artifactPath.unlink(missing_ok=True)

    def _BuildArtifactDirectory(
        self,
        artifactRootPath: Path,
        productPageUrl: str,
    ) -> Path:
        return artifactRootPath / self._ExtractProductId(productPageUrl)

    def _ExtractProductId(self, productPageUrl: str) -> str:
        parsedUrl = urlparse(productPageUrl)
        pathParts = [pathPart for pathPart in parsedUrl.path.split("/") if pathPart]
        if len(pathParts) >= 2 and pathParts[0] == "goods":
            return pathParts[1]
        if len(pathParts) >= 2 and pathParts[0] == "products":
            return "global-{0}".format(self._BuildSafePathName(pathParts[1]))
        if (
            len(pathParts) >= 3
            and pathParts[0] == "en"
            and pathParts[1] == "products"
        ):
            return "global-{0}".format(self._BuildSafePathName(pathParts[2]))
        return "unknown"

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
    ) -> None:
        self._ocrEngine = ocrEngine
        self._imageDownloader = imageDownloader or ProductOcrImageDownloader(
            downloadUserAgent,
        )
        self._artifactStore = artifactStore or ProductOcrArtifactStore()
        self._textQualityEvaluator = (
            textQualityEvaluator or ProductOcrTextQualityEvaluator()
        )

    def Run(
        self,
        imageUrls: List[str],
        artifactRootPath: Path,
        productPageUrl: str,
        maxImageCount: int,
        downloadTimeoutSeconds: int,
    ) -> List[ProductOcrImageResult]:
        artifactDirectory = self._artifactStore.PrepareArtifactDirectory(
            artifactRootPath=artifactRootPath,
            productPageUrl=productPageUrl,
        )

        imageResults: List[ProductOcrImageResult] = []
        batchImageCount = max(1, maxImageCount)
        for batchStartIndex in range(0, len(imageUrls), batchImageCount):
            batchImageUrls = imageUrls[
                batchStartIndex : batchStartIndex + batchImageCount
            ]
            for batchOffset, imageUrl in enumerate(batchImageUrls, start=1):
                imageResult = self._ExtractImageText(
                    imageIndex=batchStartIndex + batchOffset,
                    imageUrl=imageUrl,
                    artifactDirectory=artifactDirectory,
                    downloadTimeoutSeconds=downloadTimeoutSeconds,
                )
                if imageResult is not None:
                    imageResults.append(imageResult)

        return imageResults

    @staticmethod
    def BuildCombinedOcrText(imageResults: List[ProductOcrImageResult]) -> str:
        return "\n".join(
            imageResult.ocrText
            for imageResult in imageResults
            if imageResult.ocrText
        )

    def _ExtractImageText(
        self,
        imageIndex: int,
        imageUrl: str,
        artifactDirectory: Path,
        downloadTimeoutSeconds: int,
    ) -> Optional[ProductOcrImageResult]:
        artifactPath: Optional[Path] = None
        try:
            imageBytes = self._imageDownloader.Download(
                imageUrl,
                downloadTimeoutSeconds,
            )
            artifactPath = self._artifactStore.WriteImage(
                artifactDirectory=artifactDirectory,
                imageIndex=imageIndex,
                imageUrl=imageUrl,
                imageBytes=imageBytes,
            )
            structuredOcrResult = self._ocrEngine.ExtractStructuredTextFromImage(
                imageBytes,
            )
            ocrText = structuredOcrResult.text
            if (
                not isinstance(ocrText, str)
                or not self._textQualityEvaluator.HasInformativeResult(
                    structuredOcrResult,
                )
            ):
                self._artifactStore.DeleteImage(artifactPath)
                return None
            imageTiles = self._ocrEngine.BuildArtifactImageTiles(imageBytes)
            artifactPaths = self._artifactStore.ReplaceImageWithInformativeTiles(
                artifactPath=artifactPath,
                imageIndex=imageIndex,
                imageUrl=imageUrl,
                imageTiles=imageTiles,
                structuredOcrResult=structuredOcrResult,
                textQualityEvaluator=self._textQualityEvaluator,
            )
            if not artifactPaths:
                return None
            return ProductOcrImageResult(
                imageUrl=imageUrl,
                imagePath=str(artifactPaths[0]),
                imagePaths=[str(path) for path in artifactPaths],
                ocrText=ocrText,
                structuredOcr=structuredOcrResult,
            )
        except Exception as error:
            self._artifactStore.DeleteImage(artifactPath)
            return ProductOcrImageResult(
                imageUrl=imageUrl,
                error="OCR fallback failed for image {0}: {1}".format(
                    imageUrl,
                    error,
                ),
            )
