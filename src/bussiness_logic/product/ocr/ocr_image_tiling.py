"""OCR 입력 이미지의 고해상도 타일 분할 helper."""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
import numpy as np
import numpy.typing as npt

ImageArray = npt.NDArray[np.uint8]

@dataclass(frozen=True)
class ProductOcrImageTile:
    """OCR 입력으로 넘길 단일 이미지 타일."""

    tileIndex: Optional[int]
    image: ImageArray


@dataclass(frozen=True)
class ProductOcrImageTilePlan:
    """타일 분할 결과와 이미지 가공 warning."""

    tiles: List[ProductOcrImageTile] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def AsTuples(self) -> List[Tuple[Optional[int], Any]]:
        return [(tile.tileIndex, tile.image) for tile in self.tiles]


class ProductOcrImageTilePlanner:
    """긴 상품 상세 이미지를 표 경계 손실이 적은 타일로 나눈다."""

    def __init__(
        self,
        useImageTiling: bool = True,
        useProjectionTiling: bool = True,
        maxTileHeightPixels: int = 2400,
        maxTileSidePixels: int = 4000,
        tileOverlapPixels: int = 240,
        allowHardCutFallback: bool = False,
    ) -> None:
        self._useImageTiling = useImageTiling
        self._useProjectionTiling = useProjectionTiling
        self._maxTileHeightPixels = max(512, maxTileHeightPixels)
        self._maxTileSidePixels = max(
            self._maxTileHeightPixels,
            max(512, maxTileSidePixels),
        )
        maximumOverlapPixels = max(0, (self._maxTileHeightPixels - 256) // 2)
        self._tileOverlapPixels = min(max(0, tileOverlapPixels), maximumOverlapPixels)
        self._allowHardCutFallback = allowHardCutFallback

    def BuildTiles(self, image: Any) -> List[Tuple[Optional[int], Any]]:
        return self.BuildTilePlan(image).AsTuples()

    def BuildTilePlan(self, image: Any) -> ProductOcrImageTilePlan:
        if not self._useImageTiling:
            return ProductOcrImageTilePlan(
                tiles=[ProductOcrImageTile(tileIndex=None, image=image)],
            )

        imageHeight, imageWidth = self._ReadImageSize(image)
        verticalImages, verticalWarnings, verticalTransformed = (self._BuildHeightLimitedImages(image, imageHeight))
        sideLimitedImages: List[Any] = []
        warnings = list(verticalWarnings)
        sideTransformed = False
        for verticalImage in verticalImages:
            trimmedImage, trimmed, trimWarning = self._TrimHorizontalOuterMargins(verticalImage,)
            if trimWarning:
                warnings.append(trimWarning)
            sideTransformed = sideTransformed or trimmed

            widthLimitedImages, widthWarnings, widthTransformed = (self._BuildWidthLimitedImages(trimmedImage))
            sideLimitedImages.extend(widthLimitedImages)
            warnings.extend(widthWarnings)
            sideTransformed = sideTransformed or widthTransformed

        transformed = verticalTransformed or sideTransformed
        if len(sideLimitedImages) == 1 and not transformed:
            return ProductOcrImageTilePlan(
                tiles=[ProductOcrImageTile(tileIndex=None, image=sideLimitedImages[0])],
                warnings=warnings,
            )

        return ProductOcrImageTilePlan(
            tiles=[
                ProductOcrImageTile(tileIndex=tileIndex, image=tileImage)
                for tileIndex, tileImage in enumerate(sideLimitedImages, start=1)
            ],
            warnings=warnings,
        )

    def _BuildHeightLimitedImages(
        self,
        image: Any,
        imageHeight: int,
    ) -> Tuple[List[Any], List[str], bool]:
        if imageHeight <= self._maxTileHeightPixels: return [image], [], False

        if self._useProjectionTiling:
            projectionImages = self._BuildProjectionImages(
                image=image,
                dimensionSize=imageHeight,
                maxTileSize=self._maxTileHeightPixels,
                axisName="height",
            )
            if projectionImages:
                return (
                    projectionImages,
                    [
                        "height_projection_split_applied original_height={0} "
                        "tile_count={1}".format(
                            imageHeight,
                            len(projectionImages),
                        )
                    ],
                    True,
                )

        if self._allowHardCutFallback:
            fixedImages = self._BuildFixedHeightImages(image, imageHeight)
            return (
                fixedImages,
                [
                    "height_hard_cut_fallback_used original_height={0} "
                    "tile_count={1}".format(imageHeight, len(fixedImages))
                ],
                len(fixedImages) > 1,
            )

        return (
            [image],
            [
                "height_projection_not_found_preserved_original "
                "height={0} max_height={1}".format(
                    imageHeight,
                    self._maxTileHeightPixels,
                )
            ],
            False,
        )

    def _BuildWidthLimitedImages(
        self,
        image: Any,
    ) -> Tuple[List[Any], List[str], bool]:
        _, imageWidth = self._ReadImageSize(image)
        if imageWidth <= self._maxTileSidePixels:
            return [image], [], False

        if self._useProjectionTiling:
            projectionImages = self._BuildProjectionImages(
                image=image,
                dimensionSize=imageWidth,
                maxTileSize=self._maxTileSidePixels,
                axisName="width",
            )
            if projectionImages:
                return (
                    projectionImages,
                    [
                        "width_projection_split_applied original_width={0} "
                        "tile_count={1}".format(imageWidth, len(projectionImages))
                    ],
                    True,
                )

        if self._allowHardCutFallback:
            fixedImages = self._BuildFixedWidthImages(image, imageWidth)
            return (
                fixedImages,
                [
                    "width_hard_cut_fallback_used original_width={0} "
                    "tile_count={1}".format(imageWidth, len(fixedImages))
                ],
                len(fixedImages) > 1,
            )

        return (
            [image],
            [
                "width_projection_not_found_preserved_original "
                "width={0} max_side={1}".format(
                    imageWidth,
                    self._maxTileSidePixels,
                )
            ],
            False,
        )

    def _BuildProjectionImages(
        self,
        image: Any,
        dimensionSize: int,
        maxTileSize: int,
        axisName: str,
    ) -> List[Any]:
        if axisName == "height":
            lowActivityBands = self._FindHorizontalLowActivityBands(image)
        else:
            lowActivityBands = self._FindVerticalLowActivityBands(image)
        if not lowActivityBands:
            return []

        cutPoints = self._BuildProjectionCutPoints(
            lowActivityBands,
            dimensionSize=dimensionSize,
            maxTileSize=maxTileSize,
        )
        if not cutPoints:
            return []

        images: List[Any] = []
        previousCutPoint = 0
        for cutPoint in [*cutPoints, dimensionSize]:
            tileStart, tileEnd = self._BuildOverlappedTileRange(
                dimensionSize=dimensionSize,
                maxTileSize=maxTileSize,
                coreStart=previousCutPoint,
                coreEnd=cutPoint,
            )
            if tileEnd > tileStart:
                images.append(
                    self._SliceImage(
                        image=image,
                        axisName=axisName,
                        start=tileStart,
                        end=tileEnd,
                    )
                )
            previousCutPoint = cutPoint
        return images

    def _BuildFixedHeightImages(self, image: Any, imageHeight: int) -> List[Any]:
        stride = self._maxTileHeightPixels - self._tileOverlapPixels
        if stride <= 0:
            return [image]

        images: List[Any] = []
        startY = 0
        while startY < imageHeight:
            endY = min(startY + self._maxTileHeightPixels, imageHeight)
            images.append(image[startY:endY, :])
            if endY >= imageHeight:
                break
            startY = max(0, endY - self._tileOverlapPixels)
        return images

    def _BuildFixedWidthImages(self, image: Any, imageWidth: int) -> List[Any]:
        stride = self._maxTileSidePixels - self._tileOverlapPixels
        if stride <= 0:
            return [image]

        images: List[Any] = []
        startX = 0
        while startX < imageWidth:
            endX = min(startX + self._maxTileSidePixels, imageWidth)
            images.append(image[:, startX:endX])
            if endX >= imageWidth:
                break
            startX = max(0, endX - self._tileOverlapPixels)
        return images

    def _TrimHorizontalOuterMargins(
        self,
        image: Any,
    ) -> Tuple[Any, bool, Optional[str]]:
        imageHeight, imageWidth = self._ReadImageSize(image)
        if imageWidth <= 0 or imageHeight <= 0:
            return image, False, None

        densityProfiles = self._BuildDensityProfiles(image=image, axis=0)
        if densityProfiles is None:
            return image, False, None
        inkDensity, edgeDensity = densityProfiles

        try:
            import numpy as np
        except ImportError:
            return image, False, None

        activeIndexes = np.where((inkDensity > 0.003) | (edgeDensity > 0.001))[0]
        if len(activeIndexes) == 0:
            return image, False, None

        paddingPixels = max(32, self._tileOverlapPixels // 2)
        left = max(0, int(activeIndexes[0]) - paddingPixels)
        right = min(imageWidth, int(activeIndexes[-1]) + 1 + paddingPixels)

        minimumMarginPixels = max(24, imageWidth // 100)
        if left < minimumMarginPixels and imageWidth - right < minimumMarginPixels:
            return image, False, None

        trimmedWidth = right - left
        minimumTrimmedWidth = max(128, imageWidth // 5)
        if trimmedWidth < minimumTrimmedWidth:
            return (
                image,
                False,
                "outer_margin_trim_skipped_suspicious_bbox width={0} left={1} "
                "right={2}".format(imageWidth, left, right),
            )

        return (
            image[:, left:right],
            True,
            "outer_margin_trim_applied original_width={0} trimmed_width={1} "
            "left={2} right={3}".format(
                imageWidth,
                trimmedWidth,
                left,
                imageWidth - right,
            ),
        )

    def _BuildOverlappedTileRange(
        self,
        dimensionSize: int,
        maxTileSize: int,
        coreStart: int,
        coreEnd: int,
    ) -> Tuple[int, int]:
        tileStart = max(
            0,
            coreStart - (self._tileOverlapPixels if coreStart else 0),
        )
        tileEnd = min(
            dimensionSize,
            coreEnd + (self._tileOverlapPixels if coreEnd < dimensionSize else 0),
        )
        if tileEnd - tileStart <= maxTileSize:
            return tileStart, tileEnd

        overflowPixels = (tileEnd - tileStart) - maxTileSize
        leadingOverlapPixels = coreStart - tileStart
        trailingOverlapPixels = tileEnd - coreEnd

        leadingTrimPixels = min(leadingOverlapPixels, (overflowPixels + 1) // 2)
        tileStart += leadingTrimPixels
        overflowPixels -= leadingTrimPixels

        trailingTrimPixels = min(trailingOverlapPixels, overflowPixels)
        tileEnd -= trailingTrimPixels
        overflowPixels -= trailingTrimPixels

        if overflowPixels <= 0:
            return tileStart, tileEnd

        tileEnd = min(dimensionSize, tileStart + maxTileSize)
        tileStart = max(0, tileEnd - maxTileSize)
        return tileStart, tileEnd

    def _FindHorizontalLowActivityBands(self, image: Any) -> List[Tuple[int, int]]:
        return self._FindLowActivityBands(image=image, axis=1)

    def _FindVerticalLowActivityBands(self, image: Any) -> List[Tuple[int, int]]:
        return self._FindLowActivityBands(image=image, axis=0)

    def _FindLowActivityBands(
        self,
        image: Any,
        axis: int,
    ) -> List[Tuple[int, int]]:
        densityProfiles = self._BuildDensityProfiles(image=image, axis=axis)
        if densityProfiles is None:
            return []
        inkDensity, edgeDensity = densityProfiles

        whitespaceThreshold = 0.01
        edgeThreshold = 0.002
        minimumBandSize = 10
        lowActivityBands: List[Tuple[int, int]] = []
        bandStart: Optional[int] = None
        for index, density in enumerate(inkDensity):
            if (
                float(density) <= whitespaceThreshold
                or float(edgeDensity[index]) <= edgeThreshold
            ):
                if bandStart is None:
                    bandStart = index
                continue
            if bandStart is not None and index - bandStart >= minimumBandSize:
                lowActivityBands.append((bandStart, index))
            bandStart = None

        if bandStart is not None and len(inkDensity) - bandStart >= minimumBandSize:
            lowActivityBands.append((bandStart, len(inkDensity)))
        return lowActivityBands

    def _BuildDensityProfiles(
        self,
        image: Any,
        axis: int,
    ) -> Optional[Tuple[Any, Any]]:
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None

        grayImage = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edgeImage = cv2.Canny(grayImage, 50, 150)
        _, binaryImage = cv2.threshold(
            grayImage,
            0,
            255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
        )
        return (
            np.mean(binaryImage > 0, axis=axis),
            np.mean(edgeImage > 0, axis=axis),
        )

    def _BuildProjectionCutPoints(
        self,
        lowActivityBands: List[Tuple[int, int]],
        dimensionSize: int,
        maxTileSize: int,
    ) -> List[int]:
        cutPoints: List[int] = []
        currentStart = 0
        targetCoreSize = max(
            256,
            maxTileSize - (self._tileOverlapPixels * 2),
        )
        minimumTileSize = max(256, targetCoreSize // 3)
        searchWindow = max(
            480,
            self._tileOverlapPixels * 2,
            targetCoreSize // 2,
        )
        while currentStart + targetCoreSize < dimensionSize:
            targetCut = currentStart + targetCoreSize
            maximumCoreCut = min(
                dimensionSize - 1,
                currentStart + maxTileSize,
            )
            candidateCut = self._FindNearestWhitespaceCutPoint(
                lowActivityBands,
                targetCut=targetCut,
                minimumCut=currentStart + minimumTileSize,
                maximumCut=maximumCoreCut,
                searchWindow=searchWindow,
            )
            if candidateCut is None or candidateCut <= currentStart:
                return []
            cutPoints.append(candidateCut)
            currentStart = candidateCut
        return cutPoints

    def _FindNearestWhitespaceCutPoint(
        self,
        lowActivityBands: List[Tuple[int, int]],
        targetCut: int,
        minimumCut: int,
        maximumCut: int,
        searchWindow: int,
    ) -> Optional[int]:
        candidates = [
            (
                self._BuildWhitespaceBandCutPoint(
                    bandStart=bandStart,
                    bandEnd=bandEnd,
                    targetCut=targetCut,
                ),
                bandEnd - bandStart,
            )
            for bandStart, bandEnd in lowActivityBands
            if self._IsWhitespaceBandUsable(
                bandStart=bandStart,
                bandEnd=bandEnd,
                targetCut=targetCut,
                minimumCut=minimumCut,
                maximumCut=maximumCut,
                searchWindow=searchWindow,
            )
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda candidate: (
                abs(candidate[0] - targetCut),
                -candidate[1],
            ),
        )[0]

    def _IsWhitespaceBandUsable(
        self,
        bandStart: int,
        bandEnd: int,
        targetCut: int,
        minimumCut: int,
        maximumCut: int,
        searchWindow: int,
    ) -> bool:
        bandMinimum = max(bandStart, minimumCut, targetCut - searchWindow)
        bandMaximum = min(bandEnd, maximumCut, targetCut + searchWindow)
        return bandMinimum <= bandMaximum

    def _BuildWhitespaceBandCutPoint(
        self,
        bandStart: int,
        bandEnd: int,
        targetCut: int,
    ) -> int:
        return min(max(targetCut, bandStart), bandEnd)

    def _SliceImage(self, image: Any, axisName: str, start: int, end: int) -> Any:
        if axisName == "height":
            return image[start:end, :]
        return image[:, start:end]

    def _ReadImageSize(self, image: Any) -> Tuple[int, int]:
        imageShape = getattr(image, "shape", [0, 0])
        return int(imageShape[0] or 0), int(imageShape[1] or 0)
