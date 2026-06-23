"""상품 이미지 OCR adapter."""

from abc import ABC, abstractmethod
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, computed_field

from bussiness_logic.product.ocr.ocr_image_tiling import ProductOcrImageTilePlanner
from bussiness_logic.utils import NormalizeWhiteSpace


class ProductOcrError(RuntimeError):
    """OCR engine 초기화 또는 추론이 실패했을 때 사용한다."""


class ProductOcrTableResult(BaseModel):
    """PP-Structure 계열 OCR에서 추출한 단일 표 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    tableIndex: int = Field(alias="table_index")
    sourceName: str = Field(default="pp_structure_v3", alias="source_name")
    pageIndex: Optional[int] = Field(default=None, alias="page_index")
    tileIndex: Optional[int] = Field(default=None, alias="tile_index")
    html: str = ""
    cellTexts: List[str] = Field(default_factory=list, alias="cell_texts")
    plainText: str = Field(default="", alias="plain_text")


class ProductOcrTileTextResult(BaseModel):
    """타일 단위 raw OCR 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    tileIndex: Optional[int] = Field(default=None, alias="tile_index")
    text: str = ""


class ProductStructuredOcrResult(BaseModel):
    """표 우선 OCR 결과와 fallback 상태를 함께 보존한다."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    text: str = ""
    structuredText: str = Field(default="", alias="structured_text")
    rawText: str = Field(default="", alias="raw_text")
    textMergeMode: str = Field(default="raw_only", alias="text_merge_mode")
    rawTileTexts: List[ProductOcrTileTextResult] = Field(
        default_factory=list,
        alias="raw_tile_texts",
    )
    usedStructuredTables: bool = Field(
        default=False,
        alias="used_structured_tables",
    )
    fallbackReason: Optional[str] = Field(default=None, alias="fallback_reason")
    tables: List[ProductOcrTableResult] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    @computed_field(alias="raw_table_ocr")
    @property
    def rawTableOcr(self) -> str:
        return self.structuredText

    @computed_field(alias="raw_ocr")
    @property
    def rawOcr(self) -> str:
        return self.rawText


class ProductOcrEngine(ABC):
    """이미지 bytes에서 OCR 텍스트를 추출하는 adapter interface."""

    @abstractmethod
    def ExtractTextFromImage(self, imageBytes: bytes) -> str:
        raise NotImplementedError

    def BuildArtifactImageTiles(
        self,
        imageBytes: bytes,
    ) -> List[Tuple[Optional[int], bytes]]:
        return [(None, imageBytes)]

    def ExtractStructuredTextFromImage(
        self,
        imageBytes: bytes,
    ) -> ProductStructuredOcrResult:
        rawText = self.ExtractTextFromImage(imageBytes)
        return ProductStructuredOcrResult(
            text=rawText,
            rawText=rawText,
            textMergeMode="raw_only",
            rawTileTexts=[ProductOcrTileTextResult(text=rawText)] if rawText else [],
            fallbackReason="structured_ocr_not_supported",
        )


class PaddleOcrEngine(ProductOcrEngine):
    """PaddleOCR 기반 OCR adapter."""

    def __init__(
        self,
        lang: str = "korean",
        device: Optional[str] = None,
        useDocOrientationClassify: bool = False,
        useDocUnwarping: bool = False,
        useTextlineOrientation: bool = False,
        extraOptions: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._lang = lang
        self._device = device
        self._useDocOrientationClassify = useDocOrientationClassify
        self._useDocUnwarping = useDocUnwarping
        self._useTextlineOrientation = useTextlineOrientation
        self._extraOptions = dict(extraOptions or {})
        self._ocr: Any = None
        self.Initialize()

    def Initialize(self) -> None:
        """PaddleOCR 모델을 생성 시점에 한 번만 초기화한다."""

        if self._ocr is not None:
            return

        self._ocr = self._CreateOcr()

    def IsInitialized(self) -> bool:
        return self._ocr is not None

    def ExtractTextFromImage(self, imageBytes: bytes) -> str:
        image = self._DecodeImageBytes(imageBytes)
        return self._ExtractTextFromDecodedImage(image)

    def _ExtractTextFromDecodedImage(self, image: Any) -> str:
        ocr = self._ReadInitializedOcr()

        if hasattr(ocr, "predict"):
            result = ocr.predict(image)
        elif hasattr(ocr, "ocr"):
            result = ocr.ocr(image, cls=self._useTextlineOrientation)
        else:
            raise ProductOcrError("PaddleOCR object does not expose predict or ocr.")

        return "\n".join(self._ExtractResultTexts(result))

    def _EncodeImageBytes(self, image: Any, suffix: str = ".jpg") -> bytes:
        try:
            import cv2
        except ImportError as error:
            raise ProductOcrError(
                "PaddleOcrEngine requires cv2 to encode image tiles."
            ) from error

        normalizedSuffix = suffix if suffix.startswith(".") else ".jpg"
        success, encodedImage = cv2.imencode(normalizedSuffix, image)
        if not success:
            raise ProductOcrError("failed to encode OCR tile image.")
        return bytes(encodedImage)

    def _ReadInitializedOcr(self) -> Any:
        if self._ocr is None:
            raise ProductOcrError("PaddleOCR engine is not initialized.")

        return self._ocr

    def _CreateOcr(self) -> Any:
        try:
            from paddleocr import PaddleOCR
        except ImportError as error:
            raise ProductOcrError(
                "paddleocr package is required for PaddleOcrEngine."
            ) from error

        return self._CreatePaddleOcr(PaddleOCR)

    def _CreatePaddleOcr(self, paddleOcrClass: Any) -> Any:
        options: Dict[str, Any] = {
            "lang": self._lang,
            "use_doc_orientation_classify": self._useDocOrientationClassify,
            "use_doc_unwarping": self._useDocUnwarping,
            "use_textline_orientation": self._useTextlineOrientation,
            **self._extraOptions,
        }
        if self._device is not None:
            options["device"] = self._device

        try:
            return paddleOcrClass(**options)
        except TypeError:
            legacyOptions: Dict[str, Any] = {
                "lang": self._lang,
                "use_angle_cls": self._useTextlineOrientation,
                **self._extraOptions,
            }
            return paddleOcrClass(**legacyOptions)

    def _DecodeImageBytes(self, imageBytes: bytes) -> Any:
        try:
            import cv2
            import numpy as np
        except ImportError as error:
            raise ProductOcrError(
                "PaddleOcrEngine requires numpy and cv2 to decode image bytes."
            ) from error

        imageArray = np.frombuffer(imageBytes, dtype=np.uint8)
        image = cv2.imdecode(imageArray, cv2.IMREAD_COLOR)
        if image is None:
            raise ProductOcrError("failed to decode image bytes for OCR.")

        return image

    def _ExtractResultTexts(self, result: Any) -> List[str]:
        texts: List[str] = []
        self._CollectTextValues(result, texts)
        return [NormalizeWhiteSpace(text) for text in texts if NormalizeWhiteSpace(text)]

    def _CollectTextValues(self, value: Any, texts: List[str]) -> None:
        if value is None:
            return

        if isinstance(value, dict):
            self._CollectTextValuesFromDict(value, texts)
            return

        if isinstance(value, (list, tuple)):
            self._CollectTextValuesFromSequence(value, texts)
            return

        jsonValue = getattr(value, "json", None)
        if isinstance(jsonValue, dict):
            self._CollectTextValuesFromDict(jsonValue, texts)
            return

        if hasattr(value, "to_dict"):
            try:
                dictValue = value.to_dict()
            except Exception:
                dictValue = None
            if isinstance(dictValue, dict):
                self._CollectTextValuesFromDict(dictValue, texts)

    def _CollectTextValuesFromDict(
        self,
        value: Dict[str, Any],
        texts: List[str],
    ) -> None:
        for key in ["rec_texts", "texts"]:
            textValues = value.get(key)
            if isinstance(textValues, list):
                texts.extend(item for item in textValues if isinstance(item, str))

        textValue = value.get("text")
        if isinstance(textValue, str):
            texts.append(textValue)

        resultValue = value.get("res")
        if isinstance(resultValue, dict):
            self._CollectTextValuesFromDict(resultValue, texts)

    def _CollectTextValuesFromSequence(
        self,
        value: Any,
        texts: List[str],
    ) -> None:
        if self._LooksLegacyOcrLine(value):
            textValue = value[1][0]
            if isinstance(textValue, str):
                texts.append(textValue)
            return

        for item in value:
            self._CollectTextValues(item, texts)

    def _LooksLegacyOcrLine(self, value: Any) -> bool:
        return (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and isinstance(value[1], (list, tuple))
            and len(value[1]) >= 1
            and isinstance(value[1][0], str)
        )


class PaddleStructureOcrEngine(PaddleOcrEngine):
    """PP-StructureV3 표 추출을 먼저 시도하고 실패 시 일반 OCR로 fallback한다."""

    def __init__(
        self,
        lang: str = "korean",
        device: Optional[str] = None,
        useDocOrientationClassify: bool = False,
        useDocUnwarping: bool = False,
        useTextlineOrientation: bool = False,
        extraOptions: Optional[Dict[str, Any]] = None,
        structureExtraOptions: Optional[Dict[str, Any]] = None,
        useImageTiling: bool = True,
        useProjectionTiling: bool = True,
        maxTileHeightPixels: int = 2400,
        maxTileSidePixels: int = 4000,
        tileOverlapPixels: int = 240,
        allowHardCutFallback: bool = False,
    ) -> None:
        self._structureExtraOptions = dict(structureExtraOptions or {})
        self._tilePlanner = ProductOcrImageTilePlanner(
            useImageTiling=useImageTiling,
            useProjectionTiling=useProjectionTiling,
            maxTileHeightPixels=maxTileHeightPixels,
            maxTileSidePixels=maxTileSidePixels,
            tileOverlapPixels=tileOverlapPixels,
            allowHardCutFallback=allowHardCutFallback,
        )
        self._structurePipeline: Any = None
        super().__init__(
            lang=lang,
            device=device,
            useDocOrientationClassify=useDocOrientationClassify,
            useDocUnwarping=useDocUnwarping,
            useTextlineOrientation=useTextlineOrientation,
            extraOptions=extraOptions,
        )

    def ExtractStructuredTextFromImage(
        self,
        imageBytes: bytes,
    ) -> ProductStructuredOcrResult:
        image = self._DecodeImageBytes(imageBytes)
        tilePlan = self._tilePlanner.BuildTilePlan(image)
        warnings: List[str] = list(tilePlan.warnings)
        tileImages = tilePlan.AsTuples()
        rawTileTexts = self._ExtractRawTileTexts(tileImages)
        rawText = self._BuildRawTileText(rawTileTexts)
        try:
            tables = self._ExtractTablesFromTiles(tileImages)
        except Exception as error:
            warnings.append("pp_structure_failed: {0}".format(error))
            return self._BuildRawFallbackResult(
                rawText,
                rawTileTexts,
                fallbackReason="pp_structure_failed",
                warnings=warnings,
            )

        tableText = self._BuildStructuredTableText(tables)
        if tableText:
            mergedText = self._BuildMergedStructuredAndRawText(
                structuredText=tableText,
                rawText=rawText,
            )
            return ProductStructuredOcrResult(
                text=mergedText,
                structuredText=tableText,
                rawText=rawText,
                textMergeMode="structured_plus_raw",
                rawTileTexts=rawTileTexts,
                usedStructuredTables=True,
                tables=tables,
                warnings=warnings,
            )

        return self._BuildRawFallbackResult(
            rawText,
            rawTileTexts,
            fallbackReason="no_table_detected",
            warnings=warnings,
        )

    def BuildArtifactImageTiles(
        self,
        imageBytes: bytes,
    ) -> List[Tuple[Optional[int], bytes]]:
        image = self._DecodeImageBytes(imageBytes)
        tileImages = self._tilePlanner.BuildTiles(image)
        if len(tileImages) == 1 and tileImages[0][0] is None:
            return [(None, imageBytes)]
        return [
            (tileIndex, self._EncodeImageBytes(tileImage, ".jpg"))
            for tileIndex, tileImage in tileImages
        ]

    def _BuildRawFallbackResult(
        self,
        rawText: str,
        rawTileTexts: List[ProductOcrTileTextResult],
        fallbackReason: str,
        warnings: List[str],
    ) -> ProductStructuredOcrResult:
        return ProductStructuredOcrResult(
            text=rawText,
            rawText=rawText,
            textMergeMode="raw_only",
            rawTileTexts=rawTileTexts,
            fallbackReason=fallbackReason,
            warnings=list(warnings),
        )

    def _ReadInitializedStructurePipeline(self) -> Any:
        if self._structurePipeline is not None:
            return self._structurePipeline

        try:
            from paddleocr import PPStructureV3
        except ImportError as error:
            raise ProductOcrError(
                "paddleocr package with PPStructureV3 is required."
            ) from error

        options: Dict[str, Any] = {
            "lang": self._lang,
            "use_table_recognition": True,
            "use_formula_recognition": False,
            "use_chart_recognition": False,
            "use_seal_recognition": False,
            "use_doc_orientation_classify": self._useDocOrientationClassify,
            "use_doc_unwarping": self._useDocUnwarping,
            "use_textline_orientation": self._useTextlineOrientation,
            **self._structureExtraOptions,
        }
        if self._device is not None:
            options["device"] = self._device

        try:
            self._structurePipeline = PPStructureV3(**options)
        except TypeError:
            reducedOptions = {
                key: value
                for key, value in options.items()
                if key in {"lang", "use_table_recognition", "device"}
            }
            self._structurePipeline = PPStructureV3(**reducedOptions)
        return self._structurePipeline

    def _ExtractTablesFromTiles(
        self,
        tileImages: List[tuple[Optional[int], Any]],
    ) -> List[ProductOcrTableResult]:
        structurePipeline = self._ReadInitializedStructurePipeline()
        tables: List[ProductOcrTableResult] = []
        seenPlainTexts: set[str] = set()
        for tileIndex, tileImage in tileImages:
            output = structurePipeline.predict(input=tileImage)
            tablePayloads: List[Dict[str, Any]] = []
            self._CollectTablePayloads(output, tablePayloads)
            for tablePayload in tablePayloads:
                tableResult = self._BuildTableResult(
                    tablePayload,
                    tableIndex=len(tables) + 1,
                    tileIndex=tileIndex,
                )
                normalizedPlainText = NormalizeWhiteSpace(tableResult.plainText)
                if tableResult.plainText and normalizedPlainText not in seenPlainTexts:
                    tables.append(tableResult)
                    seenPlainTexts.add(normalizedPlainText)
        return tables

    def _ExtractRawTileTexts(
        self,
        tileImages: List[tuple[Optional[int], Any]],
    ) -> List[ProductOcrTileTextResult]:
        tileTextResults: List[ProductOcrTileTextResult] = []
        for tileIndex, tileImage in tileImages:
            tileText = self._ExtractTextFromDecodedImage(tileImage)
            if tileText.strip() == "":
                continue
            tileTextResults.append(
                ProductOcrTileTextResult(
                    tileIndex=tileIndex,
                    text=tileText,
                )
            )
        return tileTextResults

    def _BuildRawTileText(
        self,
        rawTileTexts: List[ProductOcrTileTextResult],
    ) -> str:
        return "\n\n".join(
            "[tile {0}]\n{1}".format(
                rawTileText.tileIndex if rawTileText.tileIndex is not None else 1,
                rawTileText.text,
            )
            for rawTileText in rawTileTexts
            if rawTileText.text.strip()
        )

    def _BuildMergedStructuredAndRawText(
        self,
        structuredText: str,
        rawText: str,
    ) -> str:
        if structuredText.strip() and rawText.strip():
            return "[structured_tables]\n{0}\n\n[raw_ocr_tiles]\n{1}".format(
                structuredText,
                rawText,
            )
        return structuredText or rawText

    def _CollectTablePayloads(
        self,
        value: Any,
        tablePayloads: List[Dict[str, Any]],
    ) -> None:
        if value is None:
            return

        if isinstance(value, dict):
            if "pred_html" in value or "table_ocr_pred" in value:
                tablePayloads.append(value)
                return
            for childValue in value.values():
                self._CollectTablePayloads(childValue, tablePayloads)
            return

        if isinstance(value, (list, tuple)):
            for item in value:
                self._CollectTablePayloads(item, tablePayloads)
            return

        jsonValue = getattr(value, "json", None)
        if isinstance(jsonValue, dict):
            self._CollectTablePayloads(jsonValue, tablePayloads)
            return

        if hasattr(value, "to_dict"):
            try:
                dictValue = value.to_dict()
            except Exception:
                dictValue = None
            if isinstance(dictValue, dict):
                self._CollectTablePayloads(dictValue, tablePayloads)

    def _BuildTableResult(
        self,
        tablePayload: Dict[str, Any],
        tableIndex: int,
        tileIndex: Optional[int],
    ) -> ProductOcrTableResult:
        html = tablePayload.get("pred_html")
        if not isinstance(html, str):
            html = ""

        tableOcrPayload = tablePayload.get("table_ocr_pred")
        cellTexts: List[str] = []
        if isinstance(tableOcrPayload, dict):
            rawCellTexts = tableOcrPayload.get("rec_texts")
            if isinstance(rawCellTexts, list):
                cellTexts.extend(
                    NormalizeWhiteSpace(cellText)
                    for cellText in rawCellTexts
                    if isinstance(cellText, str) and NormalizeWhiteSpace(cellText)
                )
        if not cellTexts and html:
            cellTexts.extend(self._ExtractTextsFromHtml(html))

        plainText = "\n".join(cellTexts)
        pageIndex = tablePayload.get("page_index")
        return ProductOcrTableResult(
            tableIndex=tableIndex,
            pageIndex=pageIndex if isinstance(pageIndex, int) else None,
            tileIndex=tileIndex,
            html=html,
            cellTexts=cellTexts,
            plainText=plainText,
        )

    def _ExtractTextsFromHtml(self, html: str) -> List[str]:
        parser = _HtmlTextExtractor()
        parser.feed(html)
        return [
            NormalizeWhiteSpace(text)
            for text in parser.texts
            if NormalizeWhiteSpace(text)
        ]

    def _BuildStructuredTableText(
        self,
        tables: List[ProductOcrTableResult],
    ) -> str:
        tableTexts = []
        for table in tables:
            if not table.plainText.strip():
                continue
            tableTexts.append(
                "[table {0}]\n{1}".format(
                    table.tableIndex,
                    table.plainText,
                )
            )
        return "\n\n".join(tableTexts)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: List[str] = []

    def handle_data(self, data: str) -> None:
        normalizedText = NormalizeWhiteSpace(data)
        if normalizedText:
            self.texts.append(normalizedText)
