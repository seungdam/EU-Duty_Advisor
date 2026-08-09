"""KurlyMarket 상품 페이지 parser/OCR fallback runtime smoke."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT_PATH = Path(__file__).resolve().parent

from bussiness_logic.app_config import LlmProfileName, LoadAppConfig  # noqa: E402
from bussiness_logic.artifact_paths import ExtractProductIdFromUrl  # noqa: E402
from bussiness_logic.utils.json_types import JsonMapping, JsonObject  # noqa: E402
from bussiness_logic.bridge.factory import RuntimeAdapterBuildError  # noqa: E402
from bussiness_logic.bridge.runtime_adapter import (  # noqa: E402
    BuildOptionalPipelineRuntimeAdapter,
    BuildPipelineRuntimeAdapter,
)
from bussiness_logic.bridge.selector import UnsupportedLlmRuntimeError  # noqa: E402
from bussiness_logic.bridge.schema import LlmGenerationOptions, LlmRequest  # noqa: E402
from bussiness_logic.input_process.reconstruction import (  # noqa: E402
    ProductInputReconstructionService,
)
from bussiness_logic.product.ocr.paddle_ocr import (  # noqa: E402
    PaddleOcrEngine,
    PaddleOcrVlEngine,
)
from bussiness_logic.product.ocr.ocr_fallback import (  # noqa: E402
    ProductOcrImageDownloader,
    ProductOcrImageResult,
)
from bussiness_logic.product.ocr.download_execution import (  # noqa: E402
    DownloadExecutionCoordinator,
)
from bussiness_logic.product.ocr.ocr_execution import (  # noqa: E402
    GetSharedOcrExecutionCoordinator,
)
from bussiness_logic.product.pipeline.kurly_url_intake_pipeline import (  # noqa: E402
    KurlyUrlIntakePipeline,
)
from bussiness_logic.product.pipeline.kurly_url_intake_schema import (  # noqa: E402
    KurlyUrlIntakeInput,
)
from bussiness_logic.product.web_parser.kurly_domestic import (  # noqa: E402
    KurlyDomesticPageParser,
)
from bussiness_logic.product.web_parser.kurly_global import KurlyGlobalPageParser  # noqa: E402
from bussiness_logic.product.web_parser.kurly_market_collector import (  # noqa: E402
    GetSharedKurlyCrawlerGate,
    KurlyPageCollector,
)
from bussiness_logic.product.web_parser.kurly_market_schema import (  # noqa: E402
    KurlyCollectionResult,
)
from bussiness_logic.product.web_parser.kurly_page_adapter import (  # noqa: E402
    KurlyPageAdapter,
)
from backend.pipeline_projection import InputProcessingViewProjector  # noqa: E402
from db.db_session_manager import DbSessionManager  # noqa: E402


VLM_SOURCE_TYPES = {"vlm_table", "pp_table"}
NUTRITION_MARKERS = (
    "영양",
    "열량",
    "나트",
    "탄수",
    "당류",
    "지방",
    "트랜스",
    "포화",
    "콜레스",
    "단백",
    "kcal",
    "mg",
)
INGREDIENT_MARKERS = ("원재료", "원료", "원제", "함량", "함유", "ingredients")
LOGGER = logging.getLogger("kurly_market_smoke")
RECALL_LEVELS = (("hs2", 2), ("hs4", 4), ("hs6", 6), ("cn8", 8))
SMOKE_LOG_CONTEXT_PATTERN = re.compile(
    r"\s*(?:pipeline|pipeline_step|step_index|step_total|component|input_dto|output_dto)=([^\s]+)"
)
SMOKE_LOG_STEP_PATTERN = re.compile(r"(?:^|\s)pipeline_step=([^\s]+)")
SMOKE_LOG_PIPELINE_PATTERN = re.compile(r"(?:^|\s)pipeline=([^\s]+)")
SMOKE_LOG_STEP_INDEX_PATTERN = re.compile(r"(?:^|\s)step_index=([^\s]+)")
SMOKE_LOG_STEP_TOTAL_PATTERN = re.compile(r"(?:^|\s)step_total=([^\s]+)")
SMOKE_LOG_COMPONENT_PATTERN = re.compile(r"(?:^|\s)component=([^\s]+)")
SMOKE_LOG_INPUT_DTO_PATTERN = re.compile(r"(?:^|\s)input_dto=([^\s]+)")
SMOKE_LOG_OUTPUT_DTO_PATTERN = re.compile(r"(?:^|\s)output_dto=([^\s]+)")


@dataclass(frozen=True, slots=True)
class AnswerRecord:
    url: str
    productId: str
    taric10: str


class _BoundLogger:
    _currentSectionKey: tuple[str, ...] | None = None
    _currentPipelineKey: tuple[str, str] | None = None

    def __init__(self, logger: logging.Logger, className: str, functionName: str) -> None:
        self._logger = logger
        self._className = className
        self._functionName = functionName

    def info(self, message: str, *args: object) -> None:
        self._log(logging.INFO, message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._log(logging.WARNING, message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log(logging.ERROR, message, *args)

    def _log(self, level: int, message: str, *args: object) -> None:
        try:
            renderedMessage = message.format(*args)
        except Exception:
            renderedMessage = " ".join([message, *[str(arg) for arg in args]])
        hasStepContext = self._HasStepContext(renderedMessage)
        self._emitSectionHeaderIfNeeded(level, renderedMessage)
        renderedMessage = self._StripStepComponentContext(renderedMessage)
        if not renderedMessage:
            return
        if hasStepContext:
            renderedMessage = f"  {renderedMessage}"
        self._logger.log(level, "%s", renderedMessage)

    def _emitSectionHeaderIfNeeded(self, level: int, message: str) -> None:
        if self._emitPipelineHeaderIfNeeded(level, message):
            return
        stepMatch = SMOKE_LOG_STEP_PATTERN.search(message)
        if stepMatch is None:
            return
        componentMatch = SMOKE_LOG_COMPONENT_PATTERN.search(message)
        stepName = stepMatch.group(1)
        componentName = (
            componentMatch.group(1)
            if componentMatch is not None
            else self._functionName.strip("_") or self._className
        )
        pipelineName = self._InferPipelineName(stepName, componentName)
        inputDtoMatch = SMOKE_LOG_INPUT_DTO_PATTERN.search(message)
        outputDtoMatch = SMOKE_LOG_OUTPUT_DTO_PATTERN.search(message)
        explicitInputDto = (
            inputDtoMatch.group(1) if inputDtoMatch is not None else ""
        )
        explicitOutputDto = (
            outputDtoMatch.group(1) if outputDtoMatch is not None else ""
        )
        inputDto, outputDto = self._InferStepDtos(
            pipelineName=pipelineName,
            stepName=stepName,
            componentName=componentName,
            explicitInputDto=explicitInputDto,
            explicitOutputDto=explicitOutputDto,
        )
        stepTotal = self._InferPipelineStepTotal(pipelineName, "")
        stepIndex = self._InferLegacyStepIndex(
            pipelineName=pipelineName,
            stepName=stepName,
            outputDto=outputDto,
        )
        pipelineKey = (pipelineName, stepTotal)
        if pipelineKey != _BoundLogger._currentPipelineKey:
            _BoundLogger._currentPipelineKey = pipelineKey
            self._logger.log(
                level,
                "%s",
                f"==========[Pipeline: {pipelineName} | Total Step = {stepTotal}]==========",
            )
        sectionKey = (pipelineName, stepIndex, componentName, inputDto, outputDto)
        if sectionKey == _BoundLogger._currentSectionKey:
            return
        _BoundLogger._currentSectionKey = sectionKey
        self._logger.log(
            level,
            "%s",
            (
                f"[Step {stepIndex}/{stepTotal} Component: {componentName} "
                f"Input: {inputDto} Output: {outputDto}]"
            ),
        )

    def _emitPipelineHeaderIfNeeded(self, level: int, message: str) -> bool:
        pipelineMatch = SMOKE_LOG_PIPELINE_PATTERN.search(message)
        if pipelineMatch is None:
            return False
        stepIndexMatch = SMOKE_LOG_STEP_INDEX_PATTERN.search(message)
        stepTotalMatch = SMOKE_LOG_STEP_TOTAL_PATTERN.search(message)
        componentMatch = SMOKE_LOG_COMPONENT_PATTERN.search(message)
        inputDtoMatch = SMOKE_LOG_INPUT_DTO_PATTERN.search(message)
        outputDtoMatch = SMOKE_LOG_OUTPUT_DTO_PATTERN.search(message)
        pipelineName = pipelineMatch.group(1)
        explicitStepTotal = stepTotalMatch.group(1) if stepTotalMatch is not None else ""
        stepTotal = self._InferPipelineStepTotal(pipelineName, explicitStepTotal)
        pipelineKey = (pipelineName, stepTotal)
        if pipelineKey != _BoundLogger._currentPipelineKey:
            _BoundLogger._currentPipelineKey = pipelineKey
            self._logger.log(
                level,
                "%s",
                f"==========[Pipeline: {pipelineName} | Total Step = {stepTotal}]==========",
            )
        if stepIndexMatch is None:
            return True
        stepIndex = stepIndexMatch.group(1)
        componentName = (
            componentMatch.group(1)
            if componentMatch is not None
            else self._functionName.strip("_") or self._className
        )
        explicitInputDto = inputDtoMatch.group(1) if inputDtoMatch is not None else ""
        explicitOutputDto = (
            outputDtoMatch.group(1) if outputDtoMatch is not None else ""
        )
        inputDto, outputDto = self._InferStepDtos(
            pipelineName=pipelineName,
            stepName=stepIndex,
            componentName=componentName,
            explicitInputDto=explicitInputDto,
            explicitOutputDto=explicitOutputDto,
        )
        sectionKey = (pipelineName, stepIndex, componentName, inputDto, outputDto)
        if sectionKey == _BoundLogger._currentSectionKey:
            return True
        _BoundLogger._currentSectionKey = sectionKey
        self._logger.log(
            level,
            "%s",
            (
                f"[Step {stepIndex}/{stepTotal} "
                f"Component: {componentName} Input: {inputDto} Output: {outputDto}]"
            ),
        )
        return True

    @staticmethod
    def _HasStepContext(message: str) -> bool:
        return (
            SMOKE_LOG_PIPELINE_PATTERN.search(message) is not None
            or SMOKE_LOG_STEP_PATTERN.search(message) is not None
        )

    @staticmethod
    def _InferPipelineName(stepName: str, componentName: str) -> str:
        if stepName in {
            "preflight",
            "product_collection",
            "smoke_boot",
            "summary",
            "evaluation",
        }:
            return "KurlyMarketSmokePipeline"
        if componentName.endswith("Pipeline"):
            return componentName
        if stepName in {"collection_ocr", "llm_reconstruction"}:
            return "KurlyUrlIntakePipeline"
        return f"{stepName}_pipeline"

    @staticmethod
    def _InferPipelineStepTotal(pipelineName: str, explicitStepTotal: str) -> str:
        if explicitStepTotal and explicitStepTotal != "0":
            return explicitStepTotal
        return {
            "KurlyMarketSmokePipeline": "4",
            "KurlyUrlIntakePipeline": "6",
            "HsCodeClassificationPipeline": "5",
            "UserInputPreparationPipeline": "1",
            "KurlyProductCollectionPipeline": "1",
            "DocumentRecommendationPipeline": "1",
        }.get(pipelineName, "1")

    @staticmethod
    def _InferLegacyStepIndex(
        *,
        pipelineName: str,
        stepName: str,
        outputDto: str,
    ) -> str:
        if pipelineName == "KurlyUrlIntakePipeline":
            if stepName == "llm_reconstruction":
                return "5"
            if stepName == "collection_ocr":
                if outputDto == "KurlyCollectionResult":
                    return "3"
                return "6"
        return {
            ("KurlyMarketSmokePipeline", "smoke_boot"): "1",
            ("KurlyMarketSmokePipeline", "preflight"): "2",
            ("KurlyMarketSmokePipeline", "product_collection"): "3",
            ("KurlyMarketSmokePipeline", "summary"): "4",
            ("KurlyMarketSmokePipeline", "evaluation"): "4",
            ("HsCodeClassificationPipeline", "build_raw_input"): "1",
            ("HsCodeClassificationPipeline", "evidence_intake"): "2",
            ("HsCodeClassificationPipeline", "product_understanding"): "3",
            ("HsCodeClassificationPipeline", "identity_distillation"): "3",
            ("HsCodeClassificationPipeline", "identity_hinting"): "3",
            ("HsCodeClassificationPipeline", "llm_reconstruction_projection"): "3",
            ("HsCodeClassificationPipeline", "composition_lane"): "3",
            ("HsCodeClassificationPipeline", "domain_routing"): "4",
            ("HsCodeClassificationPipeline", "beam_classification"): "5",
            ("HsCodeClassificationPipeline", "llm_validation"): "5",
            ("HsCodeClassificationPipeline", "evaluation"): "5",
        }.get((pipelineName, stepName), "1")

    @staticmethod
    def _InferStepDtos(
        *,
        pipelineName: str,
        stepName: str,
        componentName: str,
        explicitInputDto: str,
        explicitOutputDto: str,
    ) -> tuple[str, str]:
        outputDto = explicitOutputDto or {
            ("KurlyMarketSmokePipeline", "smoke_boot"): "SmokeRunConfig",
            ("KurlyMarketSmokePipeline", "preflight"): "PreflightStatus",
            ("KurlyMarketSmokePipeline", "product_collection"): (
                "KurlySmokeCollectionBatch"
            ),
            ("KurlyMarketSmokePipeline", "summary"): "RuntimeSmokeSummary",
            ("KurlyMarketSmokePipeline", "evaluation"): "AnswerRecallSummary",
            ("KurlyUrlIntakePipeline", "collection_ocr"): "KurlyUrlIntakeResult",
            ("KurlyUrlIntakePipeline", "llm_reconstruction"): (
                "InputReconstructionResult"
            ),
        }.get((pipelineName, stepName), "-")
        inputDto = explicitInputDto or _BoundLogger._InferInputDto(
            pipelineName=pipelineName,
            stepName=stepName,
            componentName=componentName,
            outputDto=outputDto,
        )
        return inputDto, outputDto

    @staticmethod
    def _InferInputDto(
        *,
        pipelineName: str,
        stepName: str,
        componentName: str,
        outputDto: str,
    ) -> str:
        if pipelineName == "KurlyUrlIntakePipeline":
            if stepName == "collection_ocr":
                if outputDto == "KurlyCollectionResult":
                    return "RenderedPageEvidence"
                return "KurlyUrlIntakeInput"
            if stepName == "llm_reconstruction":
                if outputDto == "RuntimeAdapter":
                    return "LlmRuntimeConfig"
                if outputDto in {"ClassificationFact", "ReconstructionTable"}:
                    return "InputReconstructionResult"
                return "InputEvidencePackage"
        if pipelineName == "KurlyMarketSmokePipeline":
            if stepName == "smoke_boot":
                return "SmokeCliArguments"
            if stepName == "preflight":
                return "SmokeRunConfig"
            if stepName == "product_collection":
                return "SmokeRunConfig"
            if stepName == "summary":
                return "SmokeRunResult"
            if stepName == "evaluation":
                return "CandidateCodeSet"
        if pipelineName == "HsCodeClassificationPipeline":
            return {
                "build_raw_input": "PreparedProductFacts",
                "evidence_intake": "RawProductInput",
                "product_understanding": "InputEvidenceState",
                "identity_distillation": "ProductUnderstandingPackage",
                "identity_hinting": "DistilledIdentityFacts",
                "llm_reconstruction_projection": "InputEvidenceState",
                "composition_lane": "ReconstructionProjection",
                "domain_routing": "ProductUnderstandingPackage",
                "beam_classification": "Hs2RoutingDecision",
                "llm_validation": "CandidateCodeSet",
                "evaluation": "CandidateCodeSet",
            }.get(stepName, "-")
        if componentName.endswith("Pipeline"):
            return f"{pipelineName}Input"
        return "-"

    @staticmethod
    def _StripStepComponentContext(message: str) -> str:
        cleanedMessage = SMOKE_LOG_CONTEXT_PATTERN.sub("", message)
        cleanedMessage = re.sub(r"\s{2,}", " ", cleanedMessage)
        return cleanedMessage.strip()


def ParseArguments(arguments: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kurly web scroll/OCR/LLM reconstruction smoke CLI",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="브라우저를 열어 appconfig URL의 스크롤 과정을 표시합니다.",
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="product_urls",
        help=(
            "검사할 상품 URL입니다. 지정하면 .appconfig 목록 대신 이 URL만 "
            "사용하며 여러 번 지정할 수 있습니다."
        ),
    )
    parser.add_argument(
        "--compare-ocr",
        action="store_true",
        help="동일 이미지의 raw OCR, PP-StructureV3, PaddleOCR-VL 결과를 비교합니다.",
    )
    parser.add_argument(
        "--compare-max-images",
        type=int,
        default=1,
        metavar="N",
        help="상품별 비교 이미지 수입니다. 기본 1, 0이면 전체입니다.",
    )
    parser.add_argument(
        "--check-ui-binding",
        action="store_true",
        help="현재 React Classification 입력 복원 DTO 바인딩을 함께 검사합니다.",
    )
    parser.add_argument(
        "--reuse-ocr-image-artifacts",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "기존 ocr-fallback-image 파일이 있으면 이미지 다운로드만 건너뛰고 "
            "OCR/VLM부터 다시 실행합니다. 기본값은 재사용입니다."
        ),
    )
    parser.add_argument(
        "--use-cached-product-input",
        action="store_true",
        help=(
            "저장된 product-input.json(직전 런의 스크롤/OCR/LLM 재구성 완성본)을 "
            "그대로 입력으로 재사용합니다. 수집·재구성을 전부 건너뛰어 분류 "
            "층만 재실행합니다 (지각 고정 A/B용). 캐시 없는 상품은 실패 처리."
        ),
    )
    parser.add_argument(
        "--classify-reconstruction",
        action="store_true",
        help=(
            "LLM reconstruction 결과를 입력으로 ProductUnderstanding, "
            "DomainRouter, Classification 후보와 계층 recall을 함께 검사합니다."
        ),
    )
    parser.add_argument(
        "--write-summary-artifact",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="smoke summary JSON artifact 저장 여부를 강제합니다.",
    )
    parser.add_argument(
        "--stage1-review-mode",
        choices=("compact", "full"),
        default="compact",
        help=(
            "Classification LLM 검증 모드입니다. 기본 compact, "
            "full은 후보별 evidence/EBTI 검토 JSON을 직접 요청합니다."
        ),
    )
    parser.add_argument(
        "--load-dotenv",
        action="store_true",
        help=(
            "프로젝트 .env 값을 명시적으로 로드합니다. 기본값은 로드하지 않고 "
            "현재 셸에 export된 환경변수만 사용합니다."
        ),
    )
    parsedArguments = parser.parse_args(arguments)
    if parsedArguments.compare_max_images < 0:
        parser.error("--compare-max-images must be greater than or equal to 0")
    return parsedArguments


def _ShortText(value: object, limit: int = 700) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _CompactText(value: object) -> str:
    return str(value or "").replace(" ", "").lower()


def _ContainsMarker(value: object, markers: Sequence[str]) -> bool:
    text = _CompactText(value)
    return any(marker.replace(" ", "").lower() in text for marker in markers)


def _LoadDotEnvDefaults(envFilePath: Path) -> None:
    if not envFilePath.exists():
        return
    for line in envFilePath.read_text(encoding="utf-8").splitlines():
        strippedLine = line.strip()
        if strippedLine == "" or strippedLine.startswith("#"):
            continue
        if strippedLine.startswith("export "):
            strippedLine = strippedLine[len("export ") :].strip()
        if "=" not in strippedLine:
            continue
        envName, rawValue = strippedLine.split("=", 1)
        normalizedName = envName.strip()
        normalizedValue = rawValue.strip()
        if (
            len(normalizedValue) >= 2
            and normalizedValue[0] == normalizedValue[-1]
            and normalizedValue[0] in {"'", '"'}
        ):
            normalizedValue = normalizedValue[1:-1].strip()
        if normalizedName and normalizedValue:
            os.environ.setdefault(normalizedName, normalizedValue)


def _EvidenceId(row: JsonMapping) -> str:
    return str(row.get("evidence_id") or row.get("id") or "").strip()


def _SourceRefsForTable(table: JsonMapping) -> list[str]:
    refs = [
        str(ref).strip()
        for ref in table.get("source_refs") or []
        if str(ref).strip()
    ]
    for row in table.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        refs.extend(
            str(ref).strip()
            for ref in row.get("source_refs") or []
            if str(ref).strip()
        )
    return list(dict.fromkeys(refs))


def _RowText(table: JsonMapping, row: JsonMapping) -> str:
    return " ".join(
        str(value or "")
        for value in (
            table.get("table_name"),
            row.get("field_name"),
            row.get("normalized_value"),
            row.get("unit"),
            row.get("daily_value_percent"),
        )
    )


def _ProductContextForTable(
    table: JsonMapping,
    evidenceRows: list[object],
) -> str:
    evidenceById = {
        evidenceId: row
        for row in evidenceRows
        if isinstance(row, Mapping) and (evidenceId := _EvidenceId(row))
    }
    optionLabels = {
        str(row.get("option_key") or "").strip(): _ShortText(
            row.get("text") or row.get("source_label") or row.get("option_key"),
            limit=120,
        )
        for row in evidenceRows
        if (
            isinstance(row, Mapping)
            and str(row.get("source_type") or "") == "notice_option"
            and str(row.get("option_key") or "").strip()
        )
    }
    optionKeys = []
    for sourceRef in _SourceRefsForTable(table):
        row = evidenceById.get(sourceRef)
        if row is None:
            continue
        optionKey = str(row.get("option_key") or "").strip()
        if optionKey:
            optionKeys.append(optionKey)
    return ", ".join(
        optionLabels.get(optionKey, optionKey)
        for optionKey in dict.fromkeys(optionKeys)
    )


def _CompactReconstructedTables(
    tables: list[object],
    evidenceRows: list[object],
) -> list[JsonObject]:
    compactTables: list[JsonObject] = []
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        rows = []
        for row in table.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            rows.append({
                "field_name": str(row.get("field_name") or ""),
                "normalized_value": _ShortText(row.get("normalized_value")),
                "unit": str(row.get("unit") or ""),
                "daily_value_percent": str(row.get("daily_value_percent") or ""),
                "source_refs": [
                    str(ref)
                    for ref in (row.get("source_refs") or [])
                    if str(ref).strip()
                ],
                "validation_status": str(row.get("validation_status") or ""),
            })
        compactTables.append({
            "table_name": str(table.get("table_name") or ""),
            "product_context": _ProductContextForTable(table, evidenceRows),
            "source_refs": [
                str(ref)
                for ref in (table.get("source_refs") or [])
                if str(ref).strip()
            ],
            "rows": rows,
        })
    return compactTables


def _CompactFacts(facts: list[object]) -> list[JsonObject]:
    compactFacts: list[JsonObject] = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        compactFacts.append({
            "field_name": str(fact.get("field_name") or ""),
            "normalized_value": _ShortText(fact.get("normalized_value")),
            "source_refs": [
                str(ref)
                for ref in (fact.get("source_refs") or [])
                if str(ref).strip()
            ],
            "validation_status": str(fact.get("validation_status") or ""),
            "correction_type": str(fact.get("correction_type") or ""),
        })
    return compactFacts


def _IngredientFacts(facts: list[object]) -> list[JsonObject]:
    return [
        dict(fact)
        for fact in facts
        if (
            isinstance(fact, Mapping)
            and _ContainsMarker(fact.get("field_name"), INGREDIENT_MARKERS)
        )
    ]


def _BuildCurrentReconstructionDrawerBinding(
    inputProcessingView: JsonMapping,
) -> JsonObject:
    reconstructedTables = inputProcessingView.get("reconstructed_detail_tables") or []
    evidenceRows = inputProcessingView.get("detail_evidence_rows") or []
    productFacts = inputProcessingView.get("reconstructed_product_facts") or []
    factTexts = inputProcessingView.get("reconstructed_fact_texts") or []
    unresolvedFacts = inputProcessingView.get("unresolved_product_facts") or []
    evidenceTraces = inputProcessingView.get("reconstruction_evidence_traces") or []
    missingFactReasons = inputProcessingView.get("missing_fact_reasons") or []
    conflicts = inputProcessingView.get("product_fact_conflicts") or []
    return {
        "render_mode": "reconstructed_tables",
        "status_table": inputProcessingView.get("reconstruction_status") or {},
        "reconstructed_tables": _CompactReconstructedTables(
            reconstructedTables if isinstance(reconstructedTables, list) else [],
            evidenceRows if isinstance(evidenceRows, list) else [],
        ),
        "reconstructed_product_facts": _CompactFacts(
            productFacts if isinstance(productFacts, list) else [],
        ),
        "reconstructed_fact_texts": [
            _ShortText(text) for text in factTexts
        ] if isinstance(factTexts, list) else [],
        "unresolved_product_facts": _CompactFacts(
            unresolvedFacts if isinstance(unresolvedFacts, list) else [],
        ),
        "reconstruction_evidence_traces": (
            evidenceTraces[:80] if isinstance(evidenceTraces, list) else []
        ),
        "missing_fact_reasons": (
            missingFactReasons[:40] if isinstance(missingFactReasons, list) else []
        ),
        "product_fact_conflicts": [
            _ShortText(conflict) for conflict in conflicts
        ] if isinstance(conflicts, list) else [],
    }


def _BuildPipelineChecks(
    facts: JsonMapping,
    inputProcessingView: JsonMapping,
) -> JsonObject:
    urlIntake = facts.get("url_intake") or {}
    if not isinstance(urlIntake, Mapping):
        urlIntake = {}
    ocrSummary = urlIntake.get("ocr") or {}
    if not isinstance(ocrSummary, Mapping):
        ocrSummary = {}
    status = inputProcessingView.get("reconstruction_status") or {}
    if not isinstance(status, Mapping):
        status = {}
    evidenceRows = inputProcessingView.get("detail_evidence_rows") or []
    if not isinstance(evidenceRows, list):
        evidenceRows = []
    sourceTypes = [
        str(record.get("source_type") or "")
        for record in evidenceRows
        if isinstance(record, Mapping)
    ]
    binding = _BuildCurrentReconstructionDrawerBinding(inputProcessingView)
    hasBoundFacts = bool(
        binding.get("reconstructed_product_facts")
        or binding.get("unresolved_product_facts")
    )
    return {
        "url_collection": bool(urlIntake),
        "ocr_image_count": (
            ocrSummary.get("image_result_count")
            or urlIntake.get("ocr_image_count")
            or 0
        ),
        "structured_table_count": ocrSummary.get("structured_table_count") or 0,
        "has_raw_ocr_evidence": "raw_ocr_tile" in sourceTypes,
        "has_vlm_evidence": any(sourceType in VLM_SOURCE_TYPES for sourceType in sourceTypes),
        "used_llm_reconstruction": bool(status.get("used_llm_reconstruction")),
        "ui_binding_ready": bool(binding.get("reconstructed_tables") or hasBoundFacts),
    }


def _BuildUiBindingDiagnostics(
    inputProcessingView: JsonMapping,
) -> JsonObject:
    reconstructedTables = inputProcessingView.get("reconstructed_detail_tables") or []
    evidenceRows = inputProcessingView.get("detail_evidence_rows") or []
    productFacts = inputProcessingView.get("reconstructed_product_facts") or []
    unresolvedFacts = inputProcessingView.get("unresolved_product_facts") or []
    if not isinstance(reconstructedTables, list):
        reconstructedTables = []
    if not isinstance(evidenceRows, list):
        evidenceRows = []
    if not isinstance(productFacts, list):
        productFacts = []
    if not isinstance(unresolvedFacts, list):
        unresolvedFacts = []

    sourceTypeCounts = Counter(
        str(record.get("source_type") or "")
        for record in evidenceRows
        if isinstance(record, Mapping)
    )
    blankNormalizedRows = []
    skeletonRows = []
    nutritionTables = []
    ingredientTables = []
    perTableSummary = []
    tableCount = 0
    rowCount = 0
    nutritionRowCount = 0
    ingredientRowCount = 0
    ingredientFacts = _IngredientFacts([*productFacts, *unresolvedFacts])

    for table in reconstructedTables:
        if not isinstance(table, Mapping):
            continue
        tableCount += 1
        tableRows = [
            row for row in table.get("rows") or [] if isinstance(row, Mapping)
        ]
        rowCount += len(tableRows)
        tableNutritionRows = []
        tableIngredientRows = []
        for row in tableRows:
            rowSummary = {
                "table_name": table.get("table_name") or "",
                "field_name": row.get("field_name") or "",
                "normalized_value": _ShortText(row.get("normalized_value"), limit=220),
                "validation_status": row.get("validation_status") or "",
            }
            if not str(row.get("normalized_value") or "").strip():
                blankNormalizedRows.append(rowSummary)
            if row.get("validation_status") == "vlm_skeleton":
                skeletonRows.append(rowSummary)
            text = _RowText(table, row)
            if _ContainsMarker(text, NUTRITION_MARKERS):
                tableNutritionRows.append(rowSummary)
            if _ContainsMarker(text, INGREDIENT_MARKERS):
                tableIngredientRows.append(rowSummary)
        if tableNutritionRows:
            nutritionRowCount += len(tableNutritionRows)
            nutritionTables.append({
                "table_name": table.get("table_name") or "",
                "row_count": len(tableNutritionRows),
                "sample_rows": tableNutritionRows[:5],
            })
        if tableIngredientRows:
            ingredientRowCount += len(tableIngredientRows)
            ingredientTables.append({
                "table_name": table.get("table_name") or "",
                "row_count": len(tableIngredientRows),
                "sample_rows": tableIngredientRows[:3],
            })
        perTableSummary.append({
            "table_name": table.get("table_name") or "",
            "row_count": len(tableRows),
            "nutrition_row_count": len(tableNutritionRows),
            "ingredient_row_count": len(tableIngredientRows),
        })

    issues = []
    if tableCount == 0:
        issues.append("no_reconstructed_tables")
    if nutritionRowCount == 0:
        issues.append("no_nutrition_rows_in_reconstructed_tables")
    if ingredientRowCount == 0 and not ingredientFacts:
        issues.append("no_ingredient_binding_rows")
    if blankNormalizedRows:
        issues.append("blank_normalized_rows")
    if skeletonRows:
        issues.append("vlm_skeleton_rows_present")

    return {
        "source_type_counts": dict(sourceTypeCounts),
        "reconstructed_table_count": tableCount,
        "reconstructed_row_count": rowCount,
        "nutrition_table_count": len(nutritionTables),
        "nutrition_row_count": nutritionRowCount,
        "ingredient_table_count": len(ingredientTables),
        "ingredient_row_count": ingredientRowCount,
        "ingredient_fact_count": len(ingredientFacts),
        "per_table_summary": perTableSummary,
        "nutrition_tables": nutritionTables,
        "ingredient_tables": ingredientTables,
        "ingredient_facts": _CompactFacts(ingredientFacts)[:8],
        "blank_normalized_rows": blankNormalizedRows,
        "vlm_skeleton_rows": skeletonRows,
        "issues": issues,
    }


def BuildUiBindingSmoke(
    facts: JsonMapping,
    *,
    sourceLabel: str,
) -> JsonObject:
    inputProcessingView = InputProcessingViewProjector().BuildInputProcessingViewFromFacts(
        facts,
    )
    return {
        "source": sourceLabel,
        "product_id": facts.get("product_id") or "",
        "input_processing_view_keys": list(inputProcessingView.keys()),
        "reconstruction_status": inputProcessingView.get("reconstruction_status") or {},
        "pipeline_checks": _BuildPipelineChecks(facts, inputProcessingView),
        "current_ui_reconstruction_drawer_binding": (
            _BuildCurrentReconstructionDrawerBinding(inputProcessingView)
        ),
        "diagnostics": _BuildUiBindingDiagnostics(inputProcessingView),
    }


class KurlyMarketSmokeRunner:
    """실제 KurlyMarket URL에서 parser와 선택적 OCR fallback을 확인한다."""

    def __init__(
        self,
        *,
        showBrowser: bool = False,
        compareOcr: bool = False,
        compareMaxImages: int = 1,
        checkUiBinding: bool = False,
        classifyReconstruction: bool = False,
        stage1ReviewMode: str = "compact",
        writeSummaryArtifact: bool | None = None,
        reuseOcrImageArtifacts: bool = True,
        useCachedProductInput: bool = False,
        loadDotEnv: bool = False,
        productUrls: Sequence[str] | None = None,
    ) -> None:
        if loadDotEnv:
            _LoadDotEnvDefaults(PROJECT_ROOT_PATH / ".env")
        appConfig = LoadAppConfig(PROJECT_ROOT_PATH)
        pathConfig = appConfig.paths
        smokeConfig = appConfig.kurly_smoke

        self._productUrls = list(productUrls or smokeConfig.product_urls)
        self._timeoutSeconds = smokeConfig.timeout_seconds
        self._scrollCount = smokeConfig.scroll_count
        self._crawlerConcurrencyGate = GetSharedKurlyCrawlerGate(
            smokeConfig.crawl_concurrency,
        )
        self._headless = False if showBrowser else smokeConfig.headless
        self._compareOcr = compareOcr
        self._compareMaxImages = compareMaxImages
        self._checkUiBinding = checkUiBinding
        self._classifyReconstruction = classifyReconstruction
        self._stage1ReviewMode = stage1ReviewMode
        self._runOcrFallback = smokeConfig.run_ocr_fallback
        self._useStructuredOcr = smokeConfig.use_structured_ocr
        self._maxOcrImageCount = smokeConfig.max_ocr_image_count
        self._downloadExecutionCoordinator = DownloadExecutionCoordinator(
            maxWorkers=smokeConfig.download_workers,
            maxQueuedDownloads=smokeConfig.max_queued_downloads,
        )
        self._maxPendingOcrImages = smokeConfig.max_pending_ocr_images
        self._ocrExecutionCoordinator = GetSharedOcrExecutionCoordinator(
            smokeConfig.ocr_workers,
        )
        self._structuredOcrMaxTileHeightPixels = (
            smokeConfig.structured_ocr_max_tile_height_pixels
        )
        self._structuredOcrMaxTileSidePixels = (
            smokeConfig.structured_ocr_max_tile_side_pixels
        )
        self._structuredOcrTileOverlapPixels = (
            smokeConfig.structured_ocr_tile_overlap_pixels
        )
        self._structuredOcrUseProjectionTiling = (
            smokeConfig.structured_ocr_use_projection_tiling
        )
        self._structuredOcrAllowHardCutFallback = (
            smokeConfig.structured_ocr_allow_hard_cut_fallback
        )
        self._structuredOcrVlExtraOptions = (
            smokeConfig.BuildStructuredOcrVlExtraOptions()
        )
        self._useInputReconstruction = smokeConfig.use_input_reconstruction
        self._useLlmInputReconstruction = smokeConfig.use_llm_input_reconstruction
        self._llmInputReconstructionMaxTokens = (
            smokeConfig.llm_input_reconstruction_max_tokens
        )
        self._inputDictionaryFuzzyMinRatio = (
            smokeConfig.input_dictionary_fuzzy_min_ratio
        )
        self._writeSummaryArtifact = (
            smokeConfig.write_summary_artifact
            if writeSummaryArtifact is None
            else writeSummaryArtifact
        )
        self._logFullResult = smokeConfig.log_full_result
        self._artifactRootPath = pathConfig.ResolvePath(
            PROJECT_ROOT_PATH,
            pathConfig.kurly_smoke_artifact_root,
        )
        self._summaryArtifactPath = pathConfig.ResolvePath(
            PROJECT_ROOT_PATH,
            pathConfig.kurly_smoke_summary_artifact,
        )
        self._answerByUrl, self._answerByProductId = self._LoadAnswerRecords()
        self._maxLoggedNoticeOptions = smokeConfig.max_logged_notice_options
        self._maxLoggedFieldsPerOption = smokeConfig.max_logged_fields_per_option
        self._maxLoggedOcrCandidateUrls = smokeConfig.max_logged_ocr_candidate_urls
        self._fieldValuePreviewCharacters = smokeConfig.field_value_preview_characters
        self._ocrTextPreviewCharacters = smokeConfig.ocr_text_preview_characters
        self._reuseOcrImageArtifacts = reuseOcrImageArtifacts
        self._useCachedProductInput = useCachedProductInput
        self._pipelineOcrEngine: object = None
        self._pipelineRawOcrEngine: object = None
        self._kurlyUrlIntakePipeline: KurlyUrlIntakePipeline | None = None

    @staticmethod
    def _LoadAnswerRecords(
    ) -> tuple[dict[str, AnswerRecord], dict[str, AnswerRecord]]:
        from db.db_session_manager import DbSessionManager

        manager = DbSessionManager.GetInstance()
        if not manager.TableExists("classification_smoke_case"):
            raise RuntimeError(
                "Required smoke table is missing: classification_smoke_case"
            )
        byUrl: dict[str, AnswerRecord] = {}
        byProductId: dict[str, AnswerRecord] = {}
        rows = manager.FetchRows(
            """
            SELECT product_url, expected_code
            FROM classification_smoke_case
            ORDER BY case_id
            """
        )
        if not rows:
            raise RuntimeError(
                "Required smoke table is empty: classification_smoke_case"
            )
        for row in rows:
            url = str(row.get("product_url") or "").strip()
            taric10 = KurlyMarketSmokeRunner._NormalizeTaric10(
                row.get("expected_code")
            )
            if not url or not taric10:
                continue
            productId = ExtractProductIdFromUrl(url)
            record = AnswerRecord(url=url, productId=productId, taric10=taric10)
            normalizedUrl = KurlyMarketSmokeRunner._NormalizeAnswerUrl(url)
            if normalizedUrl:
                byUrl[normalizedUrl] = record
            if productId:
                byProductId[productId] = record
        return byUrl, byProductId

    @staticmethod
    def _NormalizeTaric10(value: object) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        if 7 <= len(digits) <= 9:
            digits = digits.zfill(10)
        return digits[:10] if len(digits) >= 10 else ""

    @staticmethod
    def _NormalizeAnswerUrl(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        parts = urlsplit(text)
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/"),
                "",
                "",
            )
        )

    def _FindAnswerRecord(self, productUrl: str, productId: str) -> AnswerRecord | None:
        return (
            self._answerByUrl.get(self._NormalizeAnswerUrl(productUrl))
            or self._answerByProductId.get(productId)
        )

    def Run(self) -> None:
        self._ConfigureLogger()
        runLogger = self._Logger("Run")
        runLogger.info(
            (
                "pipeline_step=smoke_boot component=KurlyMarketSmokeRunner "
                "output_dto=SmokeRunConfig url_count={} run_ocr_fallback={} "
                "browser_mode={} compare_ocr={} classify_reconstruction={} "
                "stage1_review_mode={} reuse_ocr_image_artifacts={}"
            ),
            len(self._productUrls),
            self._runOcrFallback,
            "headless" if self._headless else "headed",
            self._compareOcr,
            self._classifyReconstruction,
            self._stage1ReviewMode,
            self._reuseOcrImageArtifacts,
        )
        if not self._productUrls:
            runLogger.warning(
                (
                    "실행할 상품 URL이 없습니다. .appconfig의 "
                    "[kurly_smoke].product_urls에 국내/해외 Kurly 상품 링크를 "
                    "넣어주세요."
                )
            )
            if self._writeSummaryArtifact:
                self._WriteSummaryArtifact([])
            return

        if self._classifyReconstruction:
            self._RunClassificationPreflight(runLogger)

        runLogger.info(
            (
                "pipeline_step=product_collection component=KurlyMarketSmokeRunner "
                "input_dto=SmokeRunConfig output_dto=KurlySmokeCollectionBatch "
                "action=run url_count={} pipeline_build=on_cache_miss"
            ),
            len(self._productUrls),
        )
        results: List[Dict[str]] = []
        for productIndex, productUrl in enumerate(self._productUrls, start=1):
            runLogger.info(
                (
                    "pipeline_step=product_collection component=KurlyMarketSmokeRunner "
                    "input_dto=SmokeRunConfig output_dto=KurlySmokeCollectionBatch "
                    "product_index={}/{}"
                ),
                productIndex,
                len(self._productUrls),
            )
            runLogger.info(
                (
                    "pipeline_step=product_collection component=KurlyMarketSmokeRunner "
                    "input_dto=SmokeRunConfig output_dto=KurlySmokeCollectionBatch "
                    "index={}/{} url={}"
                ),
                productIndex,
                len(self._productUrls),
                productUrl,
            )
            resultData = self._RunOne(productUrl)
            results.append(resultData)
            self._LogOne(resultData)

        runLogger.info(
            "pipeline_step=summary component=KurlyMarketSmokeRunner output_dto=RuntimeSmokeSummary action=aggregate"
        )
        self._LogSummary(results)
        if self._writeSummaryArtifact:
            runLogger.info(
                "pipeline_step=summary component=KurlyMarketSmokeRunner output_dto=RuntimeSmokeSummary action=write_artifact"
            )
            self._WriteSummaryArtifact(results)
        else:
            runLogger.info(
                "pipeline_step=summary component=KurlyMarketSmokeRunner output_dto=RuntimeSmokeSummary action=skip_artifact"
            )

    def _RunClassificationPreflight(self, runLogger: _BoundLogger) -> None:
        runLogger.info(
            "pipeline_step=preflight component=DbSessionManager/RuntimeAdapter output_dto=PreflightStatus action=check_db_and_llm"
        )
        try:
            chapterRowCount = self._RequireTableRows("cn_chapter_index")
            cnTableName, cnTableRowCount = self._RequireFirstAvailableTableRows(
                ("cn_table_index", "cn_table"),
            )
            llmRuntimeLabel = self._RequireLlmRuntime()
        except Exception as exc:
            runLogger.error(
                "pipeline_step=preflight component=DbSessionManager/RuntimeAdapter output_dto=PreflightStatus status=failed error={}",
                exc,
            )
            raise SystemExit(2) from exc

        runLogger.info(
            (
                "pipeline_step=preflight component=DbSessionManager "
                "output_dto=cn_chapter_index/cn_table_index status=ok "
                "cn_chapter_index_rows={} {}_rows={}"
            ),
            chapterRowCount,
            cnTableName,
            cnTableRowCount,
        )
        runLogger.info(
            (
                "pipeline_step=preflight component=RuntimeAdapter "
                "agent=LLMHealthCheckAgent output_dto=LlmResponse "
                "llm_status=request_response_ok runtime={}"
            ),
            llmRuntimeLabel,
        )

    @staticmethod
    def _RequireTableRows(tableName: str) -> int:
        manager = DbSessionManager.GetInstance()
        if not manager.TableExists(tableName):
            raise RuntimeError(f"required DB table not found: {tableName}")
        rowCount = manager.FetchOne(f"SELECT count(*) FROM {tableName}")
        if not isinstance(rowCount, int):
            raise RuntimeError(f"DB table row count query failed: {tableName}")
        if rowCount <= 0:
            raise RuntimeError(f"required DB table is empty: {tableName}")
        return rowCount

    @classmethod
    def _RequireFirstAvailableTableRows(
        cls,
        tableNames: Sequence[str],
    ) -> tuple[str, int]:
        errors: list[str] = []
        for tableName in tableNames:
            try:
                return tableName, cls._RequireTableRows(tableName)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{tableName}: {exc}")
        raise RuntimeError("required CN table lookup failed; " + "; ".join(errors))

    @staticmethod
    def _RequireLlmRuntime() -> str:
        runtimeAdapter = BuildPipelineRuntimeAdapter()
        runtimeConfig = runtimeAdapter.RuntimeConfig()
        response = runtimeAdapter.Generate(
            LlmRequest(
                system_prompt="You are a connection health check.",
                user_prompt="Reply with exactly: ok",
                generation_options=LlmGenerationOptions(
                    temperature=0.0,
                    # reasoning 모델은 max_tokens(→max_completion_tokens)를
                    # 추론에 먼저 소비한다 — 8이면 본문이 항상 비어 헬스체크가
                    # 구조적으로 실패(실측: empty response로 preflight 전멸).
                    # 여유분 확보; 비-reasoning 모델에도 무해(짧게 답하고 끝).
                    max_tokens=256,
                ),
            ),
        )
        if not response.generatedText.strip():
            raise RuntimeError("LLM runtime returned an empty response")
        return "{0}:{1}".format(
            runtimeConfig.runtimeKind.value,
            runtimeConfig.modelName or "default",
        )

    def _BuildKurlyUrlIntakePipeline(self) -> KurlyUrlIntakePipeline:
        pageAdapter = KurlyPageAdapter(
            domesticParser=KurlyDomesticPageParser(),
            globalParser=KurlyGlobalPageParser(),
        )
        collector = KurlyPageCollector(
            parser=pageAdapter,
            headless=self._headless,
            timeoutMilliseconds=self._timeoutSeconds * 1000,
            scrollCount=self._scrollCount,
            concurrencyGate=self._crawlerConcurrencyGate,
        )
        inputReconstructionService = self._BuildInputReconstructionService()
        ocrEngine = None
        screeningOcrEngine = None
        if self._runOcrFallback:
            if self._useStructuredOcr:
                screeningOcrEngine = PaddleOcrEngine()
            ocrEngine = (
                PaddleOcrVlEngine(
                    vlExtraOptions=self._structuredOcrVlExtraOptions,
                    useProjectionTiling=self._structuredOcrUseProjectionTiling,
                    maxTileHeightPixels=self._structuredOcrMaxTileHeightPixels,
                    maxTileSidePixels=self._structuredOcrMaxTileSidePixels,
                    tileOverlapPixels=self._structuredOcrTileOverlapPixels,
                    allowHardCutFallback=self._structuredOcrAllowHardCutFallback,
                )
                if self._useStructuredOcr
                else PaddleOcrEngine()
            )
        self._pipelineOcrEngine = ocrEngine
        self._pipelineRawOcrEngine = screeningOcrEngine
        return KurlyUrlIntakePipeline(
            collector=collector,
            ocrEngine=ocrEngine,
            screeningOcrEngine=screeningOcrEngine,
            inputReconstructionService=inputReconstructionService,
            downloadExecutionCoordinator=self._downloadExecutionCoordinator,
            ocrExecutionCoordinator=self._ocrExecutionCoordinator,
            maxPendingOcrImages=self._maxPendingOcrImages,
        )

    def _BuildInputReconstructionService(self) -> ProductInputReconstructionService | None:
        if not self._useInputReconstruction:
            return None

        runtimeAdapter = None
        if self._useLlmInputReconstruction:
            reconstructionLogger = self._Logger("_BuildInputReconstructionService")
            try:
                runtimeAdapter = BuildPipelineRuntimeAdapter(
                    LlmProfileName.INPUT_RECONSTRUCTION,
                )
                reconstructionLogger.info(
                    "pipeline_step=llm_reconstruction component=ProductInputReconstructionService agent=ProductFactReconstructionAgent output_dto=RuntimeAdapter llm_status=adapter_ready"
                )
            except (RuntimeAdapterBuildError, UnsupportedLlmRuntimeError) as error:
                reconstructionLogger.warning(
                    "pipeline_step=llm_reconstruction component=ProductInputReconstructionService agent=ProductFactReconstructionAgent output_dto=RuntimeAdapter llm_status=unavailable error={}",
                    error,
                )

        return ProductInputReconstructionService(
            runtimeAdapter=runtimeAdapter,
            fuzzyMinRatio=self._inputDictionaryFuzzyMinRatio,
            llmMaxTokens=self._llmInputReconstructionMaxTokens,
            llmArtifactRootPath=self._artifactRootPath,
        )

    def _RunOne(
        self,
        productUrl: str,
    ) -> Dict[str]:
        runLogger = self._Logger("_RunOne")
        if self._useCachedProductInput:
            return self._RunOneFromCachedInput(productUrl, runLogger)
        try:
            runLogger.info(
                (
                    "pipeline_step=product_collection component=KurlyUrlIntakePipeline "
                    "input_dto=KurlyUrlIntakeInput output_dto=KurlyUrlIntakeResult "
                    "url={} use_web_scroll={} reuse_ocr_image_artifacts={}"
                ),
                productUrl,
                True,
                self._reuseOcrImageArtifacts,
            )
            if self._kurlyUrlIntakePipeline is None:
                self._kurlyUrlIntakePipeline = self._BuildKurlyUrlIntakePipeline()
            pipelineResult = self._kurlyUrlIntakePipeline.Run(
                KurlyUrlIntakeInput(
                    productPageUrl=productUrl,
                    runOcrFallback=self._runOcrFallback,
                    artifactRootPath=self._artifactRootPath,
                    maxOcrImageCount=self._maxOcrImageCount,
                    reuseOcrImageArtifacts=self._reuseOcrImageArtifacts,
                )
            )
            resultData = self._BuildResult(
                productUrl,
                pipelineResult.model_dump(mode="json", by_alias=True),
            )
            uiFacts = (
                self._BuildDashFactsFromPipelineResult(productUrl, pipelineResult)
                if (self._checkUiBinding or self._classifyReconstruction)
                else {}
            )
            if self._checkUiBinding:
                resultData["ui_binding_smoke"] = BuildUiBindingSmoke(
                    uiFacts,
                    sourceLabel=productUrl,
                )
            if self._classifyReconstruction:
                resultData["classification_smoke"] = self._RunClassificationSmoke(
                    productUrl,
                    uiFacts,
                )
            if self._compareOcr:
                resultData["ocr_comparison"] = self._RunOcrComparison(
                    productUrl,
                    list(pipelineResult.collectionResult.ocrCandidateImageUrls),
                    pipelineResult.ocrImageResults,
                )
            return resultData
        except Exception as error:
            return {
                "product_page_url": productUrl,
                "status": {
                    "is_parse_ok": False,
                    "is_ocr_fallback_ok": False,
                    "runtime_error": str(error),
                },
            }

    def _RunOneFromCachedInput(
        self,
        productUrl: str,
        runLogger: _BoundLogger,
    ) -> Dict[str]:
        """캐시된 product-input.json(직전 런 재구성 완성본)으로 분류만 재실행.

        수집·OCR·LLM 재구성 0회 — 지각을 직전 풀런에 고정한 채 술어·매칭
        층의 변화만 측정한다 (재구성 모델 비결정성 잡음 차단).
        """
        try:
            from bussiness_logic.product.pipeline.kurly_url_facts import (
                LoadCachedProductInputFacts,
            )

            productId = ExtractProductIdFromUrl(productUrl)
            uiFacts = LoadCachedProductInputFacts(productUrl)
        except Exception as error:  # noqa: BLE001 — 캐시 부재 = 해당 상품 실패
            return {
                "product_page_url": productUrl,
                "status": {
                    "is_parse_ok": False,
                    "is_ocr_fallback_ok": False,
                    "runtime_error": str(error),
                },
            }
        runLogger.info(
            (
                "pipeline_step=product_collection component=KurlyMarketSmokeRunner "
                "input_dto=CachedProductInput output_dto=KurlyUrlFacts "
                "url={} product_name={} source=product-input.json "
                "fact_count={} fact_text_count={}"
            ),
            productUrl,
            uiFacts.get("product_name") or "",
            len(uiFacts.get("reconstructed_product_facts") or []),
            len(uiFacts.get("reconstructed_fact_texts") or []),
        )
        resultData: Dict[str] = {
            "product_page_url": productUrl,
            "status": {
                "is_parse_ok": True,
                "is_ocr_fallback_ok": True,
                "cached_product_input": True,
            },
            "product": {
                "product_name": uiFacts.get("product_name") or "",
                "product_domain": uiFacts.get("product_domain") or "unknown",
            },
        }
        if self._checkUiBinding:
            resultData["ui_binding_smoke"] = BuildUiBindingSmoke(
                uiFacts,
                sourceLabel=productUrl,
            )
        if self._classifyReconstruction:
            resultData["classification_smoke"] = self._RunClassificationSmoke(
                productUrl,
                uiFacts,
            )
        return resultData

    def _BuildDashFactsFromPipelineResult(
        self,
        productUrl: str,
        pipelineResult: object,
    ) -> Dict[str]:
        from bussiness_logic.product.pipeline.kurly_url_facts import (
            BuildKurlyUrlFactsFromPipelineResult,
        )

        return BuildKurlyUrlFactsFromPipelineResult(
            productUrl,
            pipelineResult,
            artifact_root=self._artifactRootPath,
            write_product_input_artifact=False,
        )

    def _RunClassificationSmoke(
        self,
        productUrl: str,
        uiFacts: JsonMapping,
    ) -> Dict[str]:
        from bussiness_logic.pipeline.blackboard import BlackboardStore
        from bussiness_logic.classification.pipeline.hs_code_classification_pipeline import (
            HsCodeClassificationPipeline,
        )
        from bussiness_logic.classification.pipeline.raw_input import (
            BuildRawInputFromUi,
        )
        from bussiness_logic.pipeline.pipeline_context import PipelineContext

        rawInput = BuildRawInputFromUi(
            query=str(uiFacts.get("product_name") or productUrl),
            facts=dict(uiFacts),
        )
        productId = str(uiFacts.get("product_id") or ExtractProductIdFromUrl(productUrl))
        runDirectory = (
            self._artifactRootPath
            / productId
            / "classification-smoke"
            / datetime.now().strftime("%Y%m%dT%H%M%S%f")
        )
        store = BlackboardStore.create(
            runtime_mode="smoke",
            run_id="run_001",
            run_dir=runDirectory,
            validate_on_write=False,
        )
        componentResults = []
        previousReviewMode = os.environ.get("ASAP_STAGE1_REVIEW_MODE")
        os.environ["ASAP_STAGE1_REVIEW_MODE"] = self._stage1ReviewMode
        try:
            context = PipelineContext(
                query=str(rawInput.get("product_name") or productUrl),
                facts=rawInput,
                store=store,
            )
            HsCodeClassificationPipeline(
                identityHintRuntimeAdapter=BuildOptionalPipelineRuntimeAdapter(
                    LlmProfileName.IDENTITY_HINT,
                ),
                routingRuntimeAdapter=BuildOptionalPipelineRuntimeAdapter(
                    LlmProfileName.HS2_ROUTER,
                ),
                validationRuntimeAdapter=BuildOptionalPipelineRuntimeAdapter(
                    LlmProfileName.CLASSIFICATION_VALIDATOR,
                ),
            ).Run(context)
            stepResults = list(context.stepResults)
            componentResults = list(context.componentResults)
            rawInput = context.rawInput
        finally:
            if previousReviewMode is None:
                os.environ.pop("ASAP_STAGE1_REVIEW_MODE", None)
            else:
                os.environ["ASAP_STAGE1_REVIEW_MODE"] = previousReviewMode

        blackboard = store.load()
        productEvidenceState = blackboard.get("product_evidence_state") or {}
        observedFacts = productEvidenceState.get("observed_facts") or {}
        productUnderstanding = blackboard.get("product_understanding") or {}
        if not isinstance(productUnderstanding, Mapping):
            productUnderstanding = {}
        routingContext = blackboard.get("routing_context") or {}
        if not isinstance(routingContext, Mapping):
            routingContext = {}
        candidateCodeSet = (blackboard.get("candidate_code_sets") or [None])[-1]
        if not isinstance(candidateCodeSet, Mapping):
            candidateCodeSet = {}
        trace = candidateCodeSet.get("classification_trace") or {}
        if not trace:
            resolverDebug = candidateCodeSet.get("resolver_debug") or {}
            if isinstance(resolverDebug, Mapping):
                trace = resolverDebug.get("classification_trace") or {}
        if not isinstance(trace, Mapping):
            trace = {}
        candidates = self._BuildClassificationCandidateSmokeRows(candidateCodeSet)
        zeroScoreCodes = [
            candidate["cn8"]
            for candidate in candidates
            if float(candidate.get("score") or 0) <= 0
        ]
        classificationComponentResult = self._FindComponentResult(
            componentResults,
            "Classification_Component",
        )
        classificationPaths = candidateCodeSet.get("classification_paths") or []
        if not isinstance(classificationPaths, list):
            classificationPaths = []
        stageTrace = trace.get("stages") or []
        if not isinstance(stageTrace, list):
            stageTrace = []
        answerRecord = self._FindAnswerRecord(productUrl, productId)
        answerRecall = self._BuildAnswerRecallSmoke(
            answerRecord,
            candidates,
            routingContext=routingContext,
            stageTrace=stageTrace,
        )
        llmValidationRecommendation = self._BuildLlmValidationRecommendationSmoke(
            candidates,
        )
        return {
            "source": productUrl,
            "dash_equivalence": {
                "scope": (
                    "Integrated smoke path after merge: reconstruction facts feed "
                    "ProductUnderstanding, DomainRouter, and Beam Classification."
                ),
                "path": [
                    "KurlyUrlIntakePipeline.Run",
                    "BuildKurlyUrlFactsFromPipelineResult",
                    "BuildRawInputFromUi",
                    "HsCodeClassificationPipeline.Run",
                    *[
                        self._FormatStepPathItem(stepResult)
                        for stepResult in stepResults
                    ],
                ],
                "raw_input_matches_evidence_intake": (
                    self._DoesRawInputMatchObservedFacts(rawInput, observedFacts)
                ),
                "run_dir": str(runDirectory),
                "blackboard_path": str(store.bb_path),
            },
            "input": {
                "product_name": observedFacts.get("product_name") or "",
                "classification_fact_count": (
                    len(observedFacts.get("reconstructed_product_facts") or [])
                    if isinstance(
                        observedFacts.get("reconstructed_product_facts"),
                        list,
                    )
                    else 0
                ),
                "classification_text_line_count": (
                    len(observedFacts.get("reconstructed_fact_texts") or [])
                    if isinstance(
                        observedFacts.get("reconstructed_fact_texts"),
                        list,
                    )
                    else 0
                ),
                "unresolved_fact_count": (
                    len(observedFacts.get("unresolved_product_facts") or [])
                    if isinstance(observedFacts.get("unresolved_product_facts"), list)
                    else 0
                ),
                "reconstructed_fact_texts": list(
                    observedFacts.get("reconstructed_fact_texts") or [],
                )
                if isinstance(observedFacts.get("reconstructed_fact_texts"), list)
                else [],
            },
            "status": {
                "error": classificationComponentResult.get("error"),
                "component_success": bool(classificationComponentResult.get("success")),
                "llm_model": self._FindComponentRunModel(store, "Classification_Component"),
                "stage1_review_mode": self._stage1ReviewMode,
                "candidate_count": len(candidates),
                "zero_score_candidate_codes": zeroScoreCodes,
                "answer_found": answerRecall.get("answer_found"),
            },
            "product_understanding": self._BuildProductUnderstandingSmoke(
                productUnderstanding,
            ),
            "domain_routing": self._BuildRoutingSmoke(
                routingContext,
            ),
            "candidates": candidates,
            "candidate_code_set": {
                "candidate_set_id": candidateCodeSet.get("candidate_set_id"),
                "product_id": candidateCodeSet.get("product_id"),
                "resolver_debug": candidateCodeSet.get("resolver_debug") or {},
                "classification_paths": classificationPaths,
                "selected_path": candidateCodeSet.get("selected_path") or {},
                "staged_trace": {
                    "mode": trace.get("mode"),
                    "stage_count": len(stageTrace),
                    "stages": stageTrace,
                },
            },
            "decision": {
                "classification_status": candidateCodeSet.get("classification_status"),
                "failure_reason": candidateCodeSet.get("failure_reason"),
                "decision_status": trace.get("decision_status"),
                "backtracking_recommended": trace.get("backtracking_recommended"),
            },
            "traversal": {
                "traversal_status": trace.get("traversal_status"),
                "next_action": trace.get("next_action"),
                "backtracking_target_level": trace.get("backtracking_target_level"),
                "backtracking_reason": trace.get("backtracking_reason"),
            },
            "traversal_history": list(trace.get("traversal_history") or []),
            "llm_validation_recommendation": llmValidationRecommendation,
            "answer_recall": answerRecall,
            "step_results": stepResults,
            "component_results": componentResults,
            "component_runs": list(store.iter_component_runs()),
        }

    @staticmethod
    def _FormatStepPathItem(stepResult: JsonMapping) -> str:
        stepName = str(stepResult.get("step_name") or "unknown_step")
        runnerName = str(stepResult.get("runner_name") or "unknown_runner")
        return f"{stepName} -> {runnerName}"

    @staticmethod
    def _FindComponentResult(
        componentResults: Sequence[JsonMapping],
        componentName: str,
    ) -> JsonMapping:
        for componentResult in componentResults:
            if componentResult.get("component_name") == componentName:
                return componentResult
        return {
            "success": False,
            "error": f"{componentName}_not_executed",
        }

    @staticmethod
    def _BuildAnswerRecallSmoke(
        answerRecord: AnswerRecord | None,
        candidates: Sequence[JsonMapping],
        *,
        routingContext: JsonMapping | None = None,
        stageTrace: Sequence[JsonMapping] | None = None,
    ) -> Dict[str]:
        if answerRecord is None:
            return {
                "answer_found": False,
                "reason": "answer_record_not_found",
            }

        expectedByLevel = {
            level: answerRecord.taric10[:codeLength]
            for level, codeLength in RECALL_LEVELS
        }
        llmRecommendedCandidate = next(
            (candidate for candidate in candidates if candidate.get("llm_recommended")),
            None,
        )
        finalCandidateCodesByLevel = {
            level: [
                code
                for candidate in candidates[:5]
                for code in (
                    KurlyMarketSmokeRunner._CandidateCodeAtLevel(
                        candidate,
                        codeLength,
                    ),
                )
                if code
            ]
            for level, codeLength in RECALL_LEVELS
        }
        authoritativeCodesByLevel: dict[str, JsonObject] = {}
        route = routingContext if isinstance(routingContext, Mapping) else {}
        selectedHs2 = KurlyMarketSmokeRunner._NormalizeHierarchyCode(
            route.get("selected_hs2"),
            2,
        )
        allowedHs2 = route.get("allowed_hs2") or []
        alternativeHs2 = route.get("alternative_hs2") or []
        hs2Pool = KurlyMarketSmokeRunner._UniqueHierarchyCodes(
            [
                selectedHs2,
                *(
                    alternativeHs2
                    if isinstance(alternativeHs2, list)
                    else []
                ),
                *(allowedHs2 if isinstance(allowedHs2, list) else []),
            ],
            2,
        )
        authoritativeCodesByLevel["hs2"] = {
            "top1_code": selectedHs2 or (hs2Pool[0] if hs2Pool else ""),
            "top5_codes": hs2Pool[:5],
            "selection_source": "semantic_router" if hs2Pool else "none",
            "stage_status": "resolved" if selectedHs2 else "unresolved",
        }

        stages = stageTrace if isinstance(stageTrace, Sequence) else []
        for level, codeLength in RECALL_LEVELS[1:]:
            stage = next(
                (
                    item
                    for item in stages
                    if isinstance(item, Mapping)
                    and str(item.get("stage") or item.get("level") or "") == level
                ),
                None,
            )
            if stage is None:
                authoritativeCodesByLevel[level] = {
                    "top1_code": "",
                    "top5_codes": [],
                    "selection_source": "none",
                    "stage_status": "not_reached",
                }
                continue
            selectedCodes = KurlyMarketSmokeRunner._UniqueHierarchyCodes(
                stage.get("selected_codes") or [],
                codeLength,
            )
            considered = stage.get("candidates_considered") or []
            consideredCodes = KurlyMarketSmokeRunner._UniqueHierarchyCodes(
                [
                    item.get("code")
                    for item in considered
                    if isinstance(item, Mapping)
                    and str(item.get("decision") or "") != "violated"
                ],
                codeLength,
            )
            top5Codes = KurlyMarketSmokeRunner._UniqueHierarchyCodes(
                [*selectedCodes, *consideredCodes],
                codeLength,
            )[:5]
            authoritativeCodesByLevel[level] = {
                "top1_code": selectedCodes[0] if selectedCodes else "",
                "top5_codes": top5Codes,
                "selection_source": "staged_trace",
                "stage_status": str(stage.get("status") or ""),
            }

        levels: dict[str, JsonObject] = {}
        for level, codeLength in RECALL_LEVELS:
            expectedCode = expectedByLevel[level]
            authority = authoritativeCodesByLevel.get(level) or {}
            top5Codes = list(authority.get("top5_codes") or [])
            top1Code = str(authority.get("top1_code") or "")
            if not top1Code and not top5Codes and candidates:
                top5Codes = finalCandidateCodesByLevel[level]
                top1Code = top5Codes[0] if top5Codes else ""
                authority = {
                    "selection_source": "final_candidate_fallback",
                    "stage_status": "legacy_trace_missing",
                }
            llmRecommendedCode = (
                KurlyMarketSmokeRunner._CandidateCodeAtLevel(
                    llmRecommendedCandidate,
                    codeLength,
                )
                if llmRecommendedCandidate is not None
                else ""
            )
            levels[level] = {
                "expected": expectedCode,
                "top1_code": top1Code,
                "top5_codes": top5Codes,
                "top1_match": bool(expectedCode and top1Code == expectedCode),
                "top5_match": bool(expectedCode and expectedCode in top5Codes),
                "selection_source": authority.get("selection_source") or "none",
                "stage_status": authority.get("stage_status") or "",
                "llm_recommended_code": llmRecommendedCode,
                "llm_recommended_match": bool(
                    expectedCode and llmRecommendedCode == expectedCode,
                ),
            }

        return {
            "answer_found": True,
            "answer": {
                "url": answerRecord.url,
                "product_id": answerRecord.productId,
                "taric10": answerRecord.taric10,
            },
            "levels": levels,
        }

    @staticmethod
    def _NormalizeHierarchyCode(value: object, codeLength: int) -> str:
        code = re.sub(r"\D", "", str(value or ""))
        return code[:codeLength] if len(code) >= codeLength else ""

    @staticmethod
    def _UniqueHierarchyCodes(
        values: Sequence[object],
        codeLength: int,
    ) -> list[str]:
        out: list[str] = []
        for value in values:
            code = KurlyMarketSmokeRunner._NormalizeHierarchyCode(
                value,
                codeLength,
            )
            if code and code not in out:
                out.append(code)
        return out

    @staticmethod
    def _CandidateCodeAtLevel(
        candidate: JsonMapping,
        codeLength: int,
    ) -> str:
        cn8 = re.sub(r"\D", "", str(candidate.get("cn8") or ""))
        if len(cn8) >= codeLength:
            return cn8[:codeLength]
        hs6 = re.sub(r"\D", "", str(candidate.get("hs6") or ""))
        if len(hs6) >= codeLength:
            return hs6[:codeLength]
        return ""

    @staticmethod
    def _BuildLlmValidationRecommendationSmoke(
        candidates: Sequence[JsonMapping],
    ) -> Dict[str]:
        recommendedCandidate = next(
            (candidate for candidate in candidates if candidate.get("llm_recommended")),
            None,
        )
        if recommendedCandidate is None:
            return {
                "recommended": False,
                "reason": "llm_validation_recommendation_not_found",
            }
        hierarchy = recommendedCandidate.get("hierarchy") or {}
        if not isinstance(hierarchy, Mapping):
            hierarchy = {}
        classificationBasis = recommendedCandidate.get("classification_basis") or []
        if not isinstance(classificationBasis, list):
            classificationBasis = []
        evidenceRefs = recommendedCandidate.get("classification_evidence_refs") or []
        if not isinstance(evidenceRefs, list):
            evidenceRefs = []
        supportingFacts = recommendedCandidate.get("supporting_product_facts") or []
        if not isinstance(supportingFacts, list):
            supportingFacts = []
        reason = str(classificationBasis[0]).strip() if classificationBasis else ""
        return {
            "recommended": True,
            "rank": recommendedCandidate.get("rank"),
            "hs2": hierarchy.get("hs2") or "",
            "hs4": hierarchy.get("hs4") or "",
            "hs6": hierarchy.get("hs6") or recommendedCandidate.get("hs6"),
            "cn8": hierarchy.get("cn8") or recommendedCandidate.get("cn8"),
            "taric10": recommendedCandidate.get("taric10"),
            "hard_condition_status": recommendedCandidate.get(
                "hard_condition_status",
            ),
            "reason": reason,
            "classification_evidence_refs": evidenceRefs,
            "supporting_product_facts": supportingFacts,
        }

    @staticmethod
    def _BuildProductUnderstandingSmoke(
        productUnderstanding: JsonMapping,
    ) -> Dict[str]:
        identity = productUnderstanding.get("identity_hints") or {}
        if not isinstance(identity, Mapping):
            identity = {}
        distilledIdentity = productUnderstanding.get("distilled_identity") or {}
        if not isinstance(distilledIdentity, Mapping):
            distilledIdentity = {}
        coiEvidence = productUnderstanding.get("coi_evidence") or {}
        if not isinstance(coiEvidence, Mapping):
            coiEvidence = {}
        encyclopediaEvidence = productUnderstanding.get("encyclopedia_evidence") or {}
        if not isinstance(encyclopediaEvidence, Mapping):
            encyclopediaEvidence = {}
        routingTerms = productUnderstanding.get("routing_terms") or []
        if not isinstance(routingTerms, list):
            routingTerms = []
        reconstructedFacts = productUnderstanding.get("reconstructed_product_facts")
        if not isinstance(reconstructedFacts, list):
            reconstructedFacts = []
        reconstructedFactTexts = productUnderstanding.get("reconstructed_fact_texts")
        if not isinstance(reconstructedFactTexts, list):
            reconstructedFactTexts = []
        compositionFacts = productUnderstanding.get("composition_facts") or {}
        if not isinstance(compositionFacts, Mapping):
            compositionFacts = {}
        compositionTerms = compositionFacts.get("composition_terms") or []
        if not isinstance(compositionTerms, list):
            compositionTerms = []
        ingredientPercentages = compositionFacts.get("ingredient_percentages") or []
        if not isinstance(ingredientPercentages, list):
            ingredientPercentages = []
        extractionTraces = compositionFacts.get("extraction_traces") or []
        if not isinstance(extractionTraces, list):
            extractionTraces = []
        compactExtractionTraces = [
            {
                "output_field": trace.get("output_field"),
                "source_field_name": trace.get("source_field_name"),
                "selected_span": _ShortText(trace.get("selected_span"), 160),
                "normalized_value": _ShortText(trace.get("normalized_value"), 160),
                "extraction_method": trace.get("extraction_method"),
                "decision_reason": trace.get("decision_reason"),
                "unresolved_reason": trace.get("unresolved_reason"),
            }
            for trace in extractionTraces
            if isinstance(trace, Mapping)
        ]
        unresolvedTraceCount = sum(
            1
            for trace in compactExtractionTraces
            if trace.get("unresolved_reason")
        )
        unresolvedExtractionTraces = [
            trace
            for trace in compactExtractionTraces
            if trace.get("unresolved_reason")
        ]
        excludedExtractionTraces = [
            trace
            for trace in compactExtractionTraces
            if not trace.get("unresolved_reason")
            and trace.get("extraction_method") == "regex_percent_excluded"
        ]
        return {
            "understanding_id": productUnderstanding.get("understanding_id"),
            "product_id": productUnderstanding.get("product_id"),
            "product_name": productUnderstanding.get("product_name"),
            "fact_count": len(reconstructedFacts),
            "fact_text_count": len(reconstructedFactTexts),
            "llm_reconstruction_projection": {
                "facts": _CompactFacts(reconstructedFacts),
                "fact_texts": [
                    _ShortText(text) for text in reconstructedFactTexts
                ],
            },
            "identity": {
                "commercial_identity": identity.get("commercial_identity"),
                "translated_product_name": identity.get("translated_product_name"),
                "normalized_tariff_description": identity.get(
                    "normalized_tariff_description",
                ),
                "product_form_terms": identity.get("product_form_terms") or [],
                # [관측창 수정 07-22] 결정론 전사 실물(식품유형→fish cake 등)이
                # identity_terms에 병기되는데 뷰가 안 실어 "전사 미작동"으로
                # 오판됐던 것의 처방 — DTO 검증은 이 두 필드가 실물.
                "identity_terms": identity.get("identity_terms") or [],
                "preservation_state": identity.get("preservation_state"),
                "domain_hints": identity.get("domain_hints") or [],
                "chapter_hint_terms": identity.get("chapter_hint_terms") or [],
                "chapter_hint_source_terms": identity.get(
                    "chapter_hint_source_terms",
                )
                or [],
                "chapter_hint_basis": identity.get("chapter_hint_basis"),
                "chapter_hint_status": identity.get("chapter_hint_status"),
                "understanding_mode": identity.get("understanding_mode"),
                "needs_review": identity.get("needs_review"),
                "confidence": identity.get("confidence"),
            },
            "distilled_identity": {
                "commercial_identity": distilledIdentity.get("commercial_identity"),
                "normalized_description": distilledIdentity.get("normalized_description"),
                "identity_terms": distilledIdentity.get("identity_terms") or [],
                "product_form_signal_terms": distilledIdentity.get(
                    "product_form_signal_terms",
                )
                or [],
                "processing_signal_terms": distilledIdentity.get(
                    "processing_signal_terms",
                )
                or [],
                "source_titles": distilledIdentity.get("source_titles") or [],
                "source_links": distilledIdentity.get("source_links") or [],
                "quality_status": distilledIdentity.get("quality_status"),
            },
            "composition": {
                "processing_state": compositionFacts.get("processing_state"),
                "principal_ingredient": compositionFacts.get("principal_ingredient"),
                "principal_ingredient_status": compositionFacts.get(
                    "principal_ingredient_status",
                ),
                "principal_ingredient_candidates": (
                    compositionFacts.get("principal_ingredient_candidates") or []
                )[:10],
                "ingredient_classes": compositionFacts.get("ingredient_classes") or [],
                "ingredient_entries": (
                    compositionFacts.get("ingredient_entries") or []
                )[:20],
                "ingredient_entry_count": len(
                    compositionFacts.get("ingredient_entries") or [],
                ),
                "ingredient_percentages": ingredientPercentages,
                "ingredient_percentage_count": len(ingredientPercentages),
                "component_compositions": (
                    compositionFacts.get("component_compositions") or []
                )[:10],
                "component_composition_count": len(
                    compositionFacts.get("component_compositions") or [],
                ),
                "composition_terms": [
                    _ShortText(text) for text in compositionTerms
                ],
                "composition_term_count": len(compositionTerms),
                "composition_basis": compositionFacts.get("composition_basis"),
                "contains_wrapper_or_dough": compositionFacts.get(
                    "contains_wrapper_or_dough",
                ),
                "contains_sauce_or_broth": compositionFacts.get(
                    "contains_sauce_or_broth",
                ),
                "allergen_terms_excluded": compositionFacts.get(
                    "allergen_terms_excluded",
                )
                or [],
                "missing_composition_facts": compositionFacts.get(
                    "missing_composition_facts",
                )
                or [],
                "extraction_trace_count": len(extractionTraces),
                "unresolved_trace_count": unresolvedTraceCount,
                "extraction_trace_samples": (
                    unresolvedExtractionTraces
                    + excludedExtractionTraces
                    + compactExtractionTraces
                )[:3],
            },
            "coi": {
                "matched_documents": coiEvidence.get("matched_documents") or [],
                "error": coiEvidence.get("error") or "",
            },
            "encyclopedia": {
                "configured": encyclopediaEvidence.get("configured"),
                "quality_status": encyclopediaEvidence.get("quality_status"),
                "entry_count": len(encyclopediaEvidence.get("entries") or []),
                "error": encyclopediaEvidence.get("error") or "",
            },
            "routing_terms_preview": routingTerms[:12],
            "unknowns": productUnderstanding.get("unknowns") or [],
        }

    @staticmethod
    def _BuildRoutingSmoke(
        routingContext: JsonMapping,
    ) -> Dict[str]:
        return {
            "routing_decision_id": routingContext.get("routing_decision_id"),
            "selected_hs2": routingContext.get("selected_hs2") or "",
            "alternative_hs2": routingContext.get("alternative_hs2") or [],
            "allowed_hs2": routingContext.get("allowed_hs2") or [],
            "blocked_hs2": routingContext.get("blocked_hs2") or [],
            "enforce_hs2_boundary": routingContext.get("enforce_hs2_boundary"),
            "fallback_allowed": routingContext.get("fallback_allowed"),
            "domain_scopes": routingContext.get("domain_scopes") or [],
            "pre_gate_domains": routingContext.get("pre_gate_domains") or [],
            "routing_basis": routingContext.get("routing_basis") or {},
            "candidate_chapter_details": (
                routingContext.get("candidate_chapter_details") or []
            ),
            "missing_facts": routingContext.get("missing_facts") or [],
        }

    @staticmethod
    def _BuildClassificationCandidateSmokeRows(
        candidateCodeSet: JsonMapping,
    ) -> List[Dict[str]]:
        out: List[Dict[str]] = []
        candidates = candidateCodeSet.get("candidates") or []
        if not isinstance(candidates, list):
            return out
        stageScoresByCn8 = (
            KurlyMarketSmokeRunner._BuildStageScoresByCn8(candidateCodeSet)
        )
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            staticTree = candidate.get("candidate_static_tree") or {}
            if not isinstance(staticTree, Mapping):
                staticTree = {}
            hierarchy = KurlyMarketSmokeRunner._BuildCandidateHierarchySmoke(
                candidate,
            )
            stageScores = staticTree.get("level_scores") or {}
            if not isinstance(stageScores, Mapping):
                stageScores = {}
            if not stageScores:
                stageScores = stageScoresByCn8.get(hierarchy.get("cn8") or "") or {}
            out.append({
                "rank": candidate.get("rank"),
                "cn8": hierarchy.get("cn8") or candidate.get("cn8"),
                "hs6": hierarchy.get("hs6") or candidate.get("hs6"),
                "hierarchy": hierarchy,
                "taric10": candidate.get("taric10"),
                "score": staticTree.get("total_score"),
                "stage_scores": dict(stageScores),
                "llm_recommended": candidate.get("llm_recommended"),
                "hard_condition_status": candidate.get("hard_condition_status"),
                "taric10_branch_count": candidate.get("taric10_branch_count"),
                "retrieval_sources": staticTree.get("retrieval_sources") or [],
                "matched_keywords": staticTree.get("matched_keywords") or [],
                "score_breakdown": staticTree.get("score_breakdown") or {},
                "classification_basis": candidate.get("classification_basis") or [],
                "supporting_product_facts": (
                    candidate.get("supporting_product_facts") or []
                ),
                "classification_evidence_refs": (
                    candidate.get("classification_evidence_refs") or []
                ),
                "similar_ebti_cases": candidate.get("similar_ebti_cases") or [],
            })
        return out

    @staticmethod
    def _BuildStageScoresByCn8(candidateCodeSet: JsonMapping) -> Dict[str, Dict[str, object]]:
        out: Dict[str, Dict[str, object]] = {}
        paths = candidateCodeSet.get("classification_paths") or []
        if not isinstance(paths, list):
            return out
        for path in paths:
            if not isinstance(path, Mapping):
                continue
            cn8 = re.sub(r"\D", "", str(path.get("cn8") or ""))[:8]
            scores = path.get("level_scores") or {}
            if cn8 and isinstance(scores, Mapping):
                out[cn8] = dict(scores)
        return out

    @staticmethod
    def _BuildCandidateHierarchySmoke(
        candidate: JsonMapping,
    ) -> Dict[str, str]:
        cn8 = re.sub(r"\D", "", str(candidate.get("cn8") or ""))
        hs6 = re.sub(r"\D", "", str(candidate.get("hs6") or ""))
        sourceCode = cn8 or hs6
        return {
            levelName: sourceCode[:codeLength]
            for levelName, codeLength in RECALL_LEVELS
            if len(sourceCode) >= codeLength
        }

    @staticmethod
    def _DoesRawInputMatchObservedFacts(
        rawInput: JsonMapping,
        observedFacts: JsonMapping,
    ) -> bool:
        keys = (
            "product_name",
            "description",
            "reconstructed_product_facts",
            "reconstructed_fact_texts",
            "unresolved_product_facts",
            "product_fact_conflicts",
            "ocr_text",
            "source_urls",
            "origin_country",
            "intended_use",
            "warnings",
            "input_reconstruction",
        )
        return all(rawInput.get(key) == observedFacts.get(key) for key in keys)

    @staticmethod
    def _FindComponentRunModel(store: object, componentName: str) -> str | None:
        for componentRun in store.iter_component_runs():
            if componentRun.get("component_name") == componentName:
                return componentRun.get("llm_model")
        return None

    def _BuildResult(
        self,
        productUrl: str,
        pipelineResultData: Dict[str],
    ) -> Dict[str]:
        collectionResult = pipelineResultData.get("collection_result")
        if not isinstance(collectionResult, dict):
            collectionResult = pipelineResultData.get("collection_summary", {})
        parsedProductPage = pipelineResultData.get("parsed_product_page")
        if not isinstance(parsedProductPage, dict):
            parsedProductPage = collectionResult["parsed_product_page"]
        ocrSummary = pipelineResultData.get("ocr_summary", {})
        if not isinstance(ocrSummary, dict):
            ocrSummary = {}
        inputReconstruction = pipelineResultData.get("input_reconstruction", {})
        if not isinstance(inputReconstruction, dict):
            inputReconstruction = ocrSummary.get("input_reconstruction", {})
        if not isinstance(inputReconstruction, dict):
            inputReconstruction = {}
        combinedOcrText = pipelineResultData["combined_ocr_text"]
        requiresOcrFallback = parsedProductPage["requires_ocr_fallback"]
        productNoticeFieldCount = parsedProductPage.get(
            "product_notice_field_count",
        )
        if not isinstance(productNoticeFieldCount, int):
            productNoticeFieldCount = self._CountNoticeOptionFields(
                parsedProductPage.get("product_notice_options", []),
            )

        successfulOcrImageCount = int(
            ocrSummary.get("successful_image_count", 0),
        )
        isOcrFallbackOk = (
            not requiresOcrFallback
            or (
                self._runOcrFallback
                and successfulOcrImageCount > 0
            )
        )

        noticeData = {
            "line_count": collectionResult["product_notice_text_line_count"],
            "option_count": len(parsedProductPage["product_notice_options"]),
            "field_count": productNoticeFieldCount,
            "option_names": parsedProductPage["product_notice_option_names"],
            "options_preview": self._BuildOptionPreview(
                parsedProductPage["product_notice_options"],
            ),
            "requires_ocr_fallback": requiresOcrFallback,
            "image_reference_detected": parsedProductPage[
                "image_reference_detected"
            ],
        }
        ocrEvidenceData = {
            "product_detail_image_url_count": collectionResult.get(
                "product_detail_image_url_count",
                len(collectionResult.get("product_detail_image_urls", [])),
            ),
            "candidate_image_url_count": collectionResult.get(
                "ocr_candidate_image_url_count",
                len(collectionResult.get("ocr_candidate_image_urls", [])),
            ),
            "candidate_image_urls_preview": collectionResult.get(
                "ocr_candidate_image_urls",
                [],
            )[:self._maxLoggedOcrCandidateUrls],
            "image_result_count": ocrSummary.get(
                "image_result_count",
                0,
            ),
            "successful_image_count": ocrSummary.get(
                "successful_image_count",
                0,
            ),
            "skipped_image_count": ocrSummary.get(
                "skipped_image_count",
                0,
            ),
            "failed_image_count": ocrSummary.get(
                "failed_image_count",
                0,
            ),
            "structured_table_image_count": ocrSummary.get(
                "structured_table_image_count",
                0,
            ),
            "structured_table_count": ocrSummary.get(
                "structured_table_count",
                0,
            ),
            "raw_tile_text_count": ocrSummary.get(
                "raw_tile_text_count",
                0,
            ),
            "raw_text_length": ocrSummary.get(
                "raw_text_length",
                0,
            ),
            "image_artifacts": ocrSummary.get("image_artifacts", []),
            "combined_text_length": len(combinedOcrText),
            "combined_ocr_text": combinedOcrText,
        }
        return {
            "product_page_url": productUrl,
            "status": {
                "is_parse_ok": self._IsParseOk(
                    collectionResult,
                    parsedProductPage,
                    productNoticeFieldCount,
                ),
                "is_ocr_fallback_ok": isOcrFallbackOk,
            },
            "product": {
                "product_name": parsedProductPage["product_name"],
                "product_domain": parsedProductPage["product_domain"],
                "short_description": parsedProductPage["short_description"],
                "brand_name": parsedProductPage["brand_name"],
                "package_type": parsedProductPage["package_type"],
                "sale_unit": parsedProductPage["sale_unit"],
            },
            "raw_collection": {
                "page_collection": collectionResult,
                "rendered_page_evidence": pipelineResultData.get(
                    "rendered_page_evidence_summary",
                ),
                "parsed_product_page": parsedProductPage,
                "notice": noticeData,
                "ocr_evidence": ocrEvidenceData,
            },
            "llm_reconstruction": self._BuildInputReconstructionView(
                inputReconstruction,
            ),
            "pipeline_steps": pipelineResultData["steps"],
            "warnings": collectionResult["warnings"],
            "errors": pipelineResultData["errors"],
        }

    def _RunOcrComparison(
        self,
        productUrl: str,
        imageUrls: List[str],
        pipelineOcrImageResults: List[ProductOcrImageResult],
    ) -> Dict[str]:
        selectedImageUrls = (
            imageUrls
            if self._compareMaxImages == 0
            else imageUrls[:self._compareMaxImages]
        )
        reusablePipelineResults = {
            result.imageUrl: result
            for result in pipelineOcrImageResults
            if result.error is None
        }
        engines: Dict[str] = {}
        if selectedImageUrls:
            from paddleocr import PPStructureV3

            engines = {
                "only_raw_ocr": (
                    self._pipelineRawOcrEngine
                    if isinstance(self._pipelineRawOcrEngine, PaddleOcrEngine)
                    else self._pipelineOcrEngine
                    if isinstance(self._pipelineOcrEngine, PaddleOcrEngine)
                    else PaddleOcrEngine()
                ),
                "only_pp_structure": PPStructureV3(
                    lang="korean",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    use_seal_recognition=False,
                    use_table_recognition=True,
                    use_formula_recognition=False,
                    use_chart_recognition=False,
                    use_region_detection=False,
                    format_block_content=False,
                ),
                "only_vlm": (
                    self._pipelineOcrEngine
                    if isinstance(self._pipelineOcrEngine, PaddleOcrVlEngine)
                    else PaddleOcrVlEngine(
                        vlExtraOptions=self._structuredOcrVlExtraOptions,
                        useProjectionTiling=(
                            self._structuredOcrUseProjectionTiling
                        ),
                        maxTileHeightPixels=(
                            self._structuredOcrMaxTileHeightPixels
                        ),
                        maxTileSidePixels=(
                            self._structuredOcrMaxTileSidePixels
                        ),
                        tileOverlapPixels=self._structuredOcrTileOverlapPixels,
                        allowHardCutFallback=(
                            self._structuredOcrAllowHardCutFallback
                        ),
                    )
                ),
            }
        downloader = ProductOcrImageDownloader()
        imageResults: List[Dict[str]] = []
        for imageIndex, imageUrl in enumerate(selectedImageUrls, start=1):
            try:
                imageBytes = downloader.Download(
                    imageUrl,
                    self._timeoutSeconds,
                )
            except Exception as error:
                imageResults.append(
                    {
                        "index": imageIndex,
                        "image_url": imageUrl,
                        "download_error": str(error),
                        "engines": {},
                    }
                )
                continue
            engineResults = {
                engineName: self._CompareOcrEngine(
                    engineName,
                    engines[engineName],
                    imageBytes,
                )
                for engineName in (
                    "only_raw_ocr",
                    "only_pp_structure",
                    "only_vlm",
                )
            }
            engineResults["production_hybrid"] = (
                self._BuildProductionHybridComparison(
                    reusablePipelineResults[imageUrl]
                )
                if imageUrl in reusablePipelineResults
                else self._BuildSkippedOcrComparison(
                    "pipeline_result_unavailable",
                )
            )
            imageResults.append(
                {
                    "index": imageIndex,
                    "image_url": imageUrl,
                    "image_byte_count": len(imageBytes),
                    "engines": engineResults,
                }
            )

        artifactPath = (
            self._artifactRootPath
            / ExtractProductIdFromUrl(productUrl)
            / "ocr-comparison.json"
        )
        engineTotals: Dict[str, Dict[str]] = {}
        for imageResult in imageResults:
            for engineName, engineResult in (
                imageResult.get("engines", {}) or {}
            ).items():
                total = engineTotals.setdefault(
                    engineName,
                    {
                        "ok_count": 0,
                        "error_count": 0,
                        "skipped_count": 0,
                        "elapsed_seconds": 0.0,
                    },
                )
                status = engineResult.get("status")
                if status == "ok":
                    total["ok_count"] += 1
                    total["elapsed_seconds"] = round(
                        total["elapsed_seconds"]
                        + float(engineResult.get("elapsed_seconds") or 0.0),
                        3,
                    )
                elif status == "skipped":
                    total["skipped_count"] += 1
                else:
                    total["error_count"] += 1
        comparisonData = {
            "product_page_url": productUrl,
            "generated_at": datetime.now().astimezone().isoformat(),
            "comparison_modes": [
                "only_raw_ocr",
                "only_pp_structure",
                "only_vlm",
                "production_hybrid",
            ],
            "vl_backend": self._structuredOcrVlExtraOptions.get("vl_rec_backend"),
            "candidate_image_count": len(imageUrls),
            "image_count": len(imageResults),
            "engine_totals": engineTotals,
            "artifact_path": str(artifactPath),
            "images": imageResults,
        }
        artifactPath.parent.mkdir(parents=True, exist_ok=True)
        artifactPath.write_text(
            json.dumps(comparisonData, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        comparisonLogger = self._Logger("_RunOcrComparison")
        comparisonLogger.info(
            "ocr_comparison_artifact={} engine_totals={}",
            artifactPath,
            engineTotals,
        )
        return {
            "image_count": len(imageResults),
            "engine_totals": engineTotals,
            "artifact_path": str(artifactPath),
        }

    def _BuildProductionHybridComparison(
        self,
        imageResult: ProductOcrImageResult,
    ) -> Dict[str]:
        if imageResult.skippedReason is not None:
            return self._BuildSkippedOcrComparison(imageResult.skippedReason)
        structuredResult = imageResult.structuredOcr
        text = structuredResult.text
        tableTexts = [table.plainText for table in structuredResult.tables]
        stageTimes = {
            key: value
            for key, value in imageResult.processingTimes.items()
            if key != "download"
        }
        return {
            "status": "ok",
            "elapsed_seconds": round(sum(stageTimes.values()), 3),
            "pipeline_stage_times": stageTimes,
            "text_length": len(text),
            "line_count": len(
                [line for line in text.splitlines() if line.strip()]
            ),
            "table_count": len(tableTexts),
            "text_preview": self._BuildTextPreview(
                text,
                self._ocrTextPreviewCharacters,
            ),
            "text": text,
            "table_texts": tableTexts,
            "warnings": list(structuredResult.warnings),
            "fallback_reason": structuredResult.fallbackReason,
            "table_sources": [
                table.sourceName
                for table in structuredResult.tables
            ],
            "reused_pipeline_result": True,
            "error": None,
        }

    @staticmethod
    def _BuildSkippedOcrComparison(reason: str) -> Dict[str]:
        return {
            "status": "skipped",
            "elapsed_seconds": 0.0,
            "text_length": 0,
            "line_count": 0,
            "table_count": 0,
            "text_preview": "",
            "text": "",
            "table_texts": [],
            "warnings": [],
            "error": reason,
        }

    def _CompareOcrEngine(
        self,
        engineName: str,
        engine: object,
        imageBytes: bytes,
    ) -> Dict[str]:
        startedAt = perf_counter()
        text = ""
        tableTexts: List[str] = []
        warnings: List[str] = []
        extra: Dict[str] = {}
        try:
            if engineName == "only_raw_ocr":
                text = engine.ExtractTextFromImage(imageBytes)
            elif engineName == "only_vlm":
                result = engine.ExtractStructuredTextFromImage(imageBytes)
                text = result.text
                tableTexts = [table.plainText for table in result.tables]
                warnings = list(result.warnings)
                extra = {
                    "fallback_reason": result.fallbackReason,
                    "table_sources": [table.sourceName for table in result.tables],
                }
            else:
                import cv2
                import numpy as np

                image = cv2.imdecode(
                    np.frombuffer(imageBytes, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if image is None:
                    raise ValueError("failed to decode comparison image")
                textParts: List[str] = []
                for result in engine.predict(image):
                    payload = self._ReadPaddleResultPayload(result)
                    markdownText = self._ReadPaddleMarkdownText(result)
                    if markdownText:
                        textParts.append(markdownText)
                    for block in payload.get("parsing_res_list", []) or []:
                        if not isinstance(block, dict):
                            continue
                        content = block.get("block_content")
                        if not isinstance(content, str) or not content.strip():
                            continue
                        if not markdownText:
                            textParts.append(content.strip())
                        if block.get("block_label") == "table":
                            tableTexts.append(content.strip())
                text = "\n\n".join(textParts)
        except Exception as error:
            return {
                "status": "error",
                "elapsed_seconds": round(perf_counter() - startedAt, 3),
                "text_length": 0,
                "table_count": 0,
                "text_preview": "",
                "text": "",
                "table_texts": [],
                "warnings": [],
                "error": str(error),
            }
        comparisonResult = {
            "status": "ok",
            "elapsed_seconds": round(perf_counter() - startedAt, 3),
            "text_length": len(text),
            "line_count": len([line for line in text.splitlines() if line.strip()]),
            "table_count": len(tableTexts),
            "text_preview": self._BuildTextPreview(
                text,
                self._ocrTextPreviewCharacters,
            ),
            "text": text,
            "table_texts": tableTexts,
            "warnings": warnings,
            "error": None,
        }
        comparisonResult.update(extra)
        return comparisonResult

    @staticmethod
    def _ReadPaddleResultPayload(result: object) -> Dict[str]:
        jsonPayload = getattr(result, "json", None)
        payload = jsonPayload if isinstance(jsonPayload, dict) else result
        if not isinstance(payload, dict):
            return {}
        nestedPayload = payload.get("res")
        return nestedPayload if isinstance(nestedPayload, dict) else payload

    @staticmethod
    def _ReadPaddleMarkdownText(result: object) -> str:
        markdown = getattr(result, "markdown", None)
        if not isinstance(markdown, dict):
            return ""
        markdownText = markdown.get("markdown_texts")
        return markdownText.strip() if isinstance(markdownText, str) else ""

    @staticmethod
    def _IsParseOk(
        collectionResult: Dict[str],
        parsedProductPage: Dict[str],
        productNoticeFieldCount: int,
    ) -> bool:
        return (
            parsedProductPage["product_name"] is not None
            and collectionResult["product_notice_text_line_count"] > 0
            and productNoticeFieldCount > 0
        )

    def _LogOne(self, resultData: Dict[str]) -> None:
        smokeLogger = self._Logger("_LogOne")
        statusData = resultData["status"]
        if "runtime_error" in statusData:
            smokeLogger.error(
                (
                    "pipeline_step=product_collection component=KurlyUrlIntakePipeline "
                    "input_dto=KurlyUrlIntakeInput output_dto=KurlyUrlIntakeResult "
                    "url={} runtime_error={}"
                ),
                resultData["product_page_url"],
                statusData["runtime_error"],
            )
            return

        if statusData.get("cached_product_input"):
            smokeLogger.info(
                (
                    "pipeline_step=product_collection component=KurlyMarketSmokeRunner "
                    "input_dto=CachedProductInput url={} product_name={} "
                    "collection=skipped reason=cached_product_input"
                ),
                resultData["product_page_url"],
                resultData["product"]["product_name"],
            )
            self._LogClassificationSmoke(resultData)
            return

        self._LogPipelineSteps(resultData)
        productData = resultData["product"]
        noticeData = resultData["raw_collection"]["notice"]
        smokeLogger.info(
            (
                "pipeline_step=product_collection component=KurlyUrlIntakePipeline "
                "input_dto=KurlyUrlIntakeInput output_dto=KurlyUrlIntakeResult "
                "url={} product_name={} domain={} parse_ok={} ocr_fallback_ok={}"
            ),
            resultData["product_page_url"],
            productData["product_name"],
            productData["product_domain"],
            statusData["is_parse_ok"],
            statusData["is_ocr_fallback_ok"],
        )
        smokeLogger.info(
            "pipeline_step=collection_ocr component=KurlyUrlIntakePipeline output_dto=KurlyCollectionResult brand_name={} package_type={} sale_unit={}",
            productData["brand_name"],
            productData["package_type"],
            productData["sale_unit"],
        )
        smokeLogger.info(
            (
                "pipeline_step=collection_ocr component=KurlyUrlIntakePipeline output_dto=KurlyCollectionResult "
                "notice_lines={} notice_options={} "
                "notice_fields={} requires_ocr_fallback={} "
                "image_reference_detected={}"
            ),
            noticeData["line_count"],
            noticeData["option_count"],
            noticeData["field_count"],
            noticeData["requires_ocr_fallback"],
            noticeData["image_reference_detected"],
        )
        self._LogNoticeOptions(resultData)
        self._LogOcrSummary(resultData)
        self._LogInputReconstruction(resultData)
        self._LogWarningsAndErrors(resultData)
        self._LogClassificationSmoke(resultData)

    def _LogPipelineSteps(self, resultData: Dict[str]) -> None:
        stepLogger = self._Logger("_LogPipelineSteps")
        pipelineSteps = resultData["pipeline_steps"]
        stepTotal = len(pipelineSteps)
        for stepIndex, pipelineStep in enumerate(pipelineSteps, start=1):
            inputDto, outputDto = self._KurlyUrlIntakeStepDtos(
                str(pipelineStep["step_name"]),
            )
            stepLogger.info(
                (
                    "pipeline=KurlyUrlIntakePipeline step_index={} step_total={} "
                    "component=KurlyUrlIntakePipeline input_dto={} output_dto={} "
                    "name={} succeeded={} message={}"
                ),
                stepIndex,
                stepTotal,
                inputDto,
                outputDto,
                pipelineStep["step_name"],
                pipelineStep["succeeded"],
                pipelineStep["message"],
            )

    @staticmethod
    def _KurlyUrlIntakeStepDtos(stepName: str) -> tuple[str, str]:
        return {
            "validate_product_page_url": (
                "KurlyUrlIntakeInput",
                "ValidatedProductPageUrl",
            ),
            "collect_rendered_page_evidence": (
                "ProductPageUrl",
                "RenderedPageEvidence",
            ),
            "parse_product_page_evidence": (
                "RenderedPageEvidence",
                "KurlyCollectionResult",
            ),
            "ocr_fallback": (
                "KurlyCollectionResult",
                "ProductOcrImageResult[]",
            ),
            "reconstruct_product_input": (
                "KurlyCollectionResult/ProductOcrImageResult[]",
                "InputReconstructionResult",
            ),
            "build_classification_fact_texts": (
                "InputReconstructionResult",
                "ProductOcrFactNormalizationResult",
            ),
        }.get(stepName, ("-", "-"))

    def _LogNoticeOptions(self, resultData: Dict[str]) -> None:
        noticeLogger = self._Logger("_LogNoticeOptions")
        noticeData = resultData["raw_collection"]["notice"]
        optionNames = noticeData["option_names"]
        if optionNames:
            noticeLogger.info("notice_option_names={}", optionNames)

        for noticeOption in noticeData["options_preview"]:
            noticeLogger.info(
                "notice_option index={} option_name={} field_count={}",
                noticeOption["index"],
                noticeOption["option_name"],
                noticeOption["field_count"],
            )
            for fieldRecord in noticeOption["fields_preview"]:
                noticeLogger.info(
                    (
                        "notice_option_field index={} name={} value={} "
                        "requires_ocr_fallback={}"
                    ),
                    noticeOption["index"],
                    fieldRecord["field_name"],
                    fieldRecord["field_value"],
                    fieldRecord["requires_ocr_fallback"],
                )

    def _LogInputReconstruction(self, resultData: Dict[str]) -> None:
        reconstructionLogger = self._Logger("_LogInputReconstruction")
        inputReconstruction = resultData.get("llm_reconstruction", {})
        if not isinstance(inputReconstruction, dict) or not inputReconstruction:
            return
        llmStatus = self._BuildLlmReconstructionArtifactStatus(
            resultData,
            inputReconstruction,
        )
        reconstructionLogger.info(
            (
                "pipeline_step=llm_reconstruction component=ProductInputReconstructionService "
                "agent=ProductFactReconstructionAgent output_dto=InputReconstructionResult "
                "method={} facts={} tables={} unresolved={} evidence_traces={} "
                "missing_fact_reasons={} conflicts={} used_llm={} "
                "fallback_reason={} llm_status={} llm_request_artifact={} "
                "llm_response_artifact={} llm_error_artifact={}"
            ),
            inputReconstruction.get("method"),
            len(inputReconstruction.get("facts", []) or []),
            len(inputReconstruction.get("reconstructed_tables", []) or []),
            len(inputReconstruction.get("unresolved_facts", []) or []),
            len(inputReconstruction.get("evidence_traces", []) or []),
            len(inputReconstruction.get("missing_fact_reasons", []) or []),
            len(inputReconstruction.get("conflicts", []) or []),
            inputReconstruction.get("used_llm_reconstruction"),
            inputReconstruction.get("fallback_reason"),
            llmStatus["status"],
            llmStatus["request_exists"],
            llmStatus["response_exists"],
            llmStatus["error_exists"],
        )
        for factRecord in inputReconstruction.get("facts", []) or []:
            reconstructedValue = str(factRecord.get("reconstructed_value") or "")
            reconstructionLogger.info(
                (
                    "pipeline_step=llm_reconstruction component=ProductInputReconstructionService "
                    "output_dto=ClassificationFact field={} reconstructed_value={} "
                    "value_chars={} source_ref_count={} status={}"
                ),
                factRecord.get("field_name"),
                reconstructedValue,
                len(reconstructedValue),
                len(factRecord.get("source_refs", []) or []),
                factRecord.get("validation_status"),
            )
        for tableRecord in inputReconstruction.get("reconstructed_tables", []) or []:
            reconstructionLogger.info(
                (
                    "pipeline_step=llm_reconstruction component=ProductInputReconstructionService "
                    "output_dto=ReconstructionTable table_name={} row_count={} source_ref_count={}"
                ),
                tableRecord.get("table_name"),
                len(tableRecord.get("rows", []) or []),
                len(tableRecord.get("source_refs", []) or []),
            )

    def _BuildLlmReconstructionArtifactStatus(
        self,
        resultData: Dict[str],
        inputReconstruction: Dict[str],
    ) -> Dict[str, object]:
        if not self._useLlmInputReconstruction:
            return {
                "status": "not_configured",
                "request_exists": False,
                "response_exists": False,
                "error_exists": False,
            }
        productUrl = str(resultData.get("product_page_url") or "")
        artifactDirectory = self._artifactRootPath / ExtractProductIdFromUrl(productUrl)
        requestExists = (
            artifactDirectory / "llm-input-reconstruction-request.json"
        ).exists()
        responseExists = (
            artifactDirectory / "llm-input-reconstruction-response.json"
        ).exists()
        errorExists = (
            artifactDirectory / "llm-input-reconstruction-error.json"
        ).exists()
        if requestExists and responseExists and inputReconstruction.get(
            "used_llm_reconstruction",
        ):
            status = "request_response_ok"
        elif requestExists and errorExists:
            status = "request_error"
        elif requestExists:
            status = "request_written_response_missing"
        else:
            status = "not_written"
        return {
            "status": status,
            "request_exists": requestExists,
            "response_exists": responseExists,
            "error_exists": errorExists,
        }

    def _LogOcrSummary(self, resultData: Dict[str]) -> None:
        ocrLogger = self._Logger("_LogOcrSummary")
        ocrData = resultData["raw_collection"]["ocr_evidence"]
        ocrLogger.info(
            (
                "detail_image_count={} ocr_candidate_count={} "
                "ocr_result_count={} successful_ocr_count={} "
                "skipped_ocr_count={} failed_ocr_count={} "
                "structured_table_image_count={} structured_table_count={} "
                "raw_tile_text_count={} raw_text_length={} "
                "combined_ocr_text_length={}"
            ),
            ocrData["product_detail_image_url_count"],
            ocrData["candidate_image_url_count"],
            ocrData["image_result_count"],
            ocrData["successful_image_count"],
            ocrData.get("skipped_image_count", 0),
            ocrData.get("failed_image_count", 0),
            ocrData["structured_table_image_count"],
            ocrData["structured_table_count"],
            ocrData["raw_tile_text_count"],
            ocrData["raw_text_length"],
            ocrData["combined_text_length"],
        )

        for imageUrl in ocrData["candidate_image_urls_preview"]:
            ocrLogger.info("ocr_candidate_image_url={}", imageUrl)

        for imageResult in ocrData["image_artifacts"]:
            ocrLogger.info(
                (
                    "ocr_image index={} status={} image_path={} "
                    "image_path_count={} text_length={} "
                    "used_structured_tables={} structured_table_count={} "
                    "raw_tile_text_count={} raw_text_length={} "
                    "merge_mode={} fallback_reason={} warning_count={} "
                    "skipped_reason={} error={}"
                ),
                imageResult["index"],
                imageResult.get("status", "ok"),
                imageResult["image_path"],
                len(imageResult.get("image_paths", []) or []),
                imageResult["text_length"],
                imageResult.get("used_structured_tables", False),
                imageResult.get("structured_table_count", 0),
                imageResult.get("raw_tile_text_count", 0),
                imageResult.get("raw_text_length", 0),
                imageResult.get("text_merge_mode"),
                imageResult.get("structured_fallback_reason"),
                len(imageResult.get("structured_warnings", []) or []),
                imageResult.get("skipped_reason"),
                imageResult["error"],
            )
            for warning in imageResult.get("structured_warnings", []) or []:
                ocrLogger.info(
                    "ocr_image index={} structured_warning={}",
                    imageResult["index"],
                    warning,
                )
    def _LogWarningsAndErrors(self, resultData: Dict[str]) -> None:
        eventLogger = self._Logger("_LogWarningsAndErrors")
        for warning in resultData["warnings"]:
            eventLogger.warning("warning={}", warning)
        for error in resultData["errors"]:
            eventLogger.error("error={}", error)
        uiBinding = resultData.get("ui_binding_smoke")
        if isinstance(uiBinding, dict):
            diagnostics = uiBinding.get("diagnostics") or {}
            eventLogger.info(
                "ui_binding_ready={} tables={} ingredient_rows={} nutrition_rows={} issues={}",
                (uiBinding.get("pipeline_checks") or {}).get("ui_binding_ready"),
                diagnostics.get("reconstructed_table_count", 0),
                diagnostics.get("ingredient_row_count", 0),
                diagnostics.get("nutrition_row_count", 0),
                diagnostics.get("issues", []),
            )

    def _LogClassificationSmoke(self, resultData: Dict[str]) -> None:
        classificationData = resultData.get("classification_smoke")
        if not isinstance(classificationData, dict):
            return
        classificationLogger = self._Logger("_LogClassificationSmoke")
        status = classificationData.get("status") or {}
        productUnderstanding = classificationData.get("product_understanding") or {}
        identity = productUnderstanding.get("identity") or {}
        if not isinstance(identity, Mapping):
            identity = {}
        distilledIdentity = productUnderstanding.get("distilled_identity") or {}
        if not isinstance(distilledIdentity, Mapping):
            distilledIdentity = {}
        composition = productUnderstanding.get("composition") or {}
        if not isinstance(composition, Mapping):
            composition = {}
        reconstructionProjection = (
            productUnderstanding.get("llm_reconstruction_projection") or {}
        )
        if not isinstance(reconstructionProjection, Mapping):
            reconstructionProjection = {}
        domainRouting = classificationData.get("domain_routing") or {}
        decision = classificationData.get("decision") or {}
        traversal = classificationData.get("traversal") or {}
        llmValidationRecommendation = (
            classificationData.get("llm_validation_recommendation") or {}
        )
        answerRecall = classificationData.get("answer_recall") or {}
        candidateCodeSet = classificationData.get("candidate_code_set") or {}
        if not isinstance(candidateCodeSet, Mapping):
            candidateCodeSet = {}
        stepResults = classificationData.get("step_results") or []
        stepTotal = len(stepResults) if isinstance(stepResults, list) else 0
        for stepIndex, stepResult in enumerate(stepResults, start=1):
            if not isinstance(stepResult, Mapping):
                continue
            inputDto, outputDto = self._ClassificationStepDtos(
                str(stepResult.get("step_name") or ""),
            )
            classificationLogger.info(
                (
                    "pipeline=HsCodeClassificationPipeline step_index={} step_total={} "
                    "component={} input_dto={} output_dto={} step_name={} "
                    "status={} outputs={}"
                ),
                stepIndex,
                stepTotal,
                stepResult.get("runner_name"),
                inputDto,
                outputDto,
                stepResult.get("step_name"),
                stepResult.get("status"),
                stepResult.get("outputs_written") or [],
            )
        classificationLogger.info(
            (
                "pipeline=HsCodeClassificationPipeline step_index=3 step_total={} "
                "component=ProductUnderstandingComponent input_dto=InputEvidenceState "
                "output_dto=DistilledIdentityFacts lane=identity_distillation "
                "service=IdentityDistillerService "
                "commercial_identity={} quality={} identity_terms={} "
                "form_signals={} processing_signals={} sources={}"
            ),
            stepTotal,
            distilledIdentity.get("commercial_identity"),
            distilledIdentity.get("quality_status"),
            self._CompactTextList(distilledIdentity.get("identity_terms")),
            self._CompactTextList(
                distilledIdentity.get("product_form_signal_terms"),
            ),
            self._CompactTextList(
                distilledIdentity.get("processing_signal_terms"),
            ),
            self._CompactTextList(distilledIdentity.get("source_titles")),
        )
        classificationLogger.info(
            (
                "pipeline=HsCodeClassificationPipeline step_index=3 step_total={} "
                "component=ProductUnderstandingComponent input_dto=DistilledIdentityFacts "
                "output_dto=IdentityHintSet lane=identity_hinting "
                "agent=IdentityHintAgent id={} "
                "product={} facts={} fact_texts={} identity_mode={} form_terms={} "
                "chapter_hints={} coi_docs={} encyclopedia={}"
            ),
            stepTotal,
            productUnderstanding.get("understanding_id"),
            productUnderstanding.get("product_name"),
            productUnderstanding.get("fact_count"),
            productUnderstanding.get("fact_text_count"),
            identity.get("understanding_mode"),
            self._CompactTextList(identity.get("product_form_terms")),
            self._CompactTextList(identity.get("chapter_hint_terms")),
            len((productUnderstanding.get("coi") or {}).get("matched_documents") or []),
            (productUnderstanding.get("encyclopedia") or {}).get("quality_status"),
        )
        classificationLogger.info(
            (
                "pipeline=HsCodeClassificationPipeline step_index=3 step_total={} "
                "component=ProductUnderstandingComponent input_dto=InputEvidenceState "
                "output_dto=ReconstructionProjection lane=llm_reconstruction_projection "
                "projected_reconstruction_facts={} projected_reconstruction_texts={}"
            ),
            stepTotal,
            len(reconstructionProjection.get("facts") or []),
            len(reconstructionProjection.get("fact_texts") or []),
        )
        classificationLogger.info(
            (
                "pipeline=HsCodeClassificationPipeline step_index=3 step_total={} "
                "component=ProductUnderstandingComponent input_dto=ReconstructionProjection "
                "output_dto=CompositionFactSet lane=composition_lane composition_terms={} "
                "ingredient_percentages={} composition_basis={} wrapper={} sauce={} "
                "principal_status={} principal_candidates={} ingredient_entries={} "
                "component_compositions={} extraction_traces={} unresolved_traces={} "
                "missing_composition_facts={}"
            ),
            stepTotal,
            composition.get("composition_term_count"),
            composition.get("ingredient_percentage_count"),
            composition.get("composition_basis"),
            composition.get("contains_wrapper_or_dough"),
            composition.get("contains_sauce_or_broth"),
            composition.get("principal_ingredient_status"),
            len(composition.get("principal_ingredient_candidates") or []),
            composition.get("ingredient_entry_count"),
            composition.get("component_composition_count"),
            composition.get("extraction_trace_count"),
            composition.get("unresolved_trace_count"),
            composition.get("missing_composition_facts"),
        )
        classificationLogger.info(
            (
                "pipeline=HsCodeClassificationPipeline step_index=4 step_total={} "
                "component=Hs2RoutingComponent input_dto=ProductUnderstandingPackage "
                "output_dto=Hs2RoutingDecision routing_context_id={} allowed_hs2={} "
                "blocked_hs2={} enforce_hs2_boundary={} fallback_allowed={} "
                "candidate_chapter_top={} boundary_applied={} fallback_used={} "
                "missing_facts={}"
            ),
            stepTotal,
            domainRouting.get("routing_context_id"),
            domainRouting.get("allowed_hs2"),
            domainRouting.get("blocked_hs2"),
            domainRouting.get("enforce_hs2_boundary"),
            domainRouting.get("fallback_allowed"),
            self._CompactChapterDetails(
                domainRouting.get("candidate_chapter_details"),
            ),
            (domainRouting.get("classification_boundary") or {}).get(
                "boundary_applied",
            ),
            (domainRouting.get("classification_boundary") or {}).get("fallback_used"),
            domainRouting.get("missing_facts"),
        )
        classificationLogger.info(
            (
                "pipeline=HsCodeClassificationPipeline step_index=5 step_total={} "
                "component=ClassificationComponent input_dto=Hs2RoutingDecision "
                "output_dto=CandidateCodeSet error={} candidates={} no_evidence_codes={} "
                "stage_count={} selected_path={} decision_trace={} decision={} backtracking={} "
                "traversal={} raw_input_match={} answer_found={}"
            ),
            stepTotal,
            status.get("error"),
            status.get("candidate_count"),
            status.get("zero_score_candidate_codes"),
            (candidateCodeSet.get("staged_trace") or {}).get("stage_count"),
            self._CompactSelectedPath(candidateCodeSet.get("selected_path")),
            self._DecisionTrace(candidateCodeSet),
            decision.get("decision_status"),
            decision.get("backtracking_recommended"),
            traversal.get("traversal_status"),
            (classificationData.get("dash_equivalence") or {}).get(
                "raw_input_matches_evidence_intake",
            ),
            answerRecall.get("answer_found"),
        )
        classificationLogger.info(
            (
                "pipeline=HsCodeClassificationPipeline step_index=5 step_total={} "
                "component=ClassificationComponent input_dto=CandidateCodeSet "
                "output_dto=LlmValidationRecommendation agent=CandidateValidationAgent "
                "llm_status={} recommended={} rank={} cn8={} hs6={} taric10={} "
                "hard_condition={} reason={}"
            ),
            stepTotal,
            (
                "recommendation_ok"
                if llmValidationRecommendation.get("recommended")
                else "recommendation_missing"
            ),
            llmValidationRecommendation.get("recommended"),
            llmValidationRecommendation.get("rank"),
            llmValidationRecommendation.get("cn8"),
            llmValidationRecommendation.get("hs6"),
            llmValidationRecommendation.get("taric10"),
            llmValidationRecommendation.get("hard_condition_status"),
            llmValidationRecommendation.get("reason"),
        )
        self._LogAnswerRecall(classificationLogger, answerRecall)
        for candidate in classificationData.get("candidates") or []:
            classificationLogger.info(
                (
                    "pipeline=HsCodeClassificationPipeline step_index=5 step_total={} "
                    "component=ClassificationComponent input_dto=CandidateCodeSet "
                    "output_dto=ClassificationCandidate rank={} cn8={} hs6={} "
                    "decided_by={} recommended={} taric_branches={} "
                    "evidence_refs={} ebti_cases={}"
                ),
                stepTotal,
                candidate.get("rank"),
                candidate.get("cn8"),
                candidate.get("hs6"),
                self._CandidateDecidedBy(candidate),
                candidate.get("llm_recommended"),
                candidate.get("taric10_branch_count"),
                len(candidate.get("classification_evidence_refs") or []),
                len(candidate.get("similar_ebti_cases") or []),
            )

    @staticmethod
    def _ClassificationStepDtos(stepName: str) -> tuple[str, str]:
        return {
            "build_raw_input": ("PreparedProductFacts", "RawProductInput"),
            "evidence_intake": ("RawProductInput", "InputEvidenceState"),
            "product_understanding": (
                "InputEvidenceState",
                "ProductUnderstandingPackage",
            ),
            "hs2_routing": (
                "ProductUnderstandingPackage",
                "Hs2RoutingDecision",
            ),
            "classification": ("Hs2RoutingDecision", "CandidateCodeSet"),
        }.get(stepName, ("-", "-"))

    @staticmethod
    def _CompactComponentResults(componentResults: Sequence[object]) -> List[str]:
        out: List[str] = []
        for componentResult in componentResults:
            if not isinstance(componentResult, Mapping):
                continue
            name = str(componentResult.get("component_name") or "unknown")
            status = "ok" if componentResult.get("success") else "fail"
            outputs = componentResult.get("outputs_written") or []
            outputCount = len(outputs) if isinstance(outputs, list) else 0
            error = str(componentResult.get("error") or "")
            text = f"{name}:{status}:outputs={outputCount}"
            if error:
                text = f"{text}:error={error[:80]}"
            out.append(text)
        return out

    @staticmethod
    def _CompactStepResults(stepResults: Sequence[object]) -> List[str]:
        out: List[str] = []
        for stepResult in stepResults:
            if not isinstance(stepResult, Mapping):
                continue
            name = str(stepResult.get("step_name") or "unknown_step")
            runner = str(stepResult.get("runner_name") or "unknown_runner")
            status = str(stepResult.get("status") or "unknown")
            outputs = stepResult.get("outputs_written") or []
            outputCount = len(outputs) if isinstance(outputs, list) else 0
            error = str(stepResult.get("error") or "")
            text = f"{name}->{runner}:{status}:outputs={outputCount}"
            if error:
                text = f"{text}:error={error[:80]}"
            out.append(text)
        return out

    @staticmethod
    def _CompactTextList(value: object, *, limit: int = 5) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item)[:60] for item in value[:limit] if str(item).strip()]

    @staticmethod
    def _CompactChapterDetails(value: object, *, limit: int = 5) -> List[Dict[str, object]]:
        if not isinstance(value, list):
            return []
        out: List[Dict[str, object]] = []
        for item in value[:limit]:
            if not isinstance(item, Mapping):
                continue
            matchedTerms = item.get("matched_terms") or []
            out.append({
                "chapter": item.get("chapter"),
                "score": item.get("score"),
                "matched_terms": (
                    [str(term)[:40] for term in matchedTerms[:3]]
                    if isinstance(matchedTerms, list)
                    else []
                ),
            })
        return out

    @staticmethod
    def _CompactSelectedPath(value: object) -> str:
        if not isinstance(value, Mapping):
            return ""
        parts = [
            str(value.get(level) or "")
            for level in ("hs2", "hs4", "hs6", "cn8")
            if str(value.get(level) or "")
        ]
        return "/".join(parts)

    @staticmethod
    def _SelectedPathScores(value: object) -> Dict[str, object]:
        if not isinstance(value, Mapping):
            return {}
        levelScores = value.get("level_scores") or {}
        return dict(levelScores) if isinstance(levelScores, Mapping) else {}

    @staticmethod
    def _DecisionTrace(candidateCodeSet: object) -> Dict[str, object]:
        """레벨별 '어떤 분기·어떤 이항검사 code를 통과했는지' — 스코어 미출력.
        각 레벨: 선택코드 + by(결정 근거) + O(통과조건 cond)·X(위반조건)·
        SILENT(값 없는 축). by 라벨 의미:
          confirmed = 전 조건 true(이산 O 확정) · residual = 형제 부정(Other)
          pass_through = 단일자식 · gri3:* = 다중확정 이산서열
          partial = 일부 조건 true인데 미확정 · lexical(미해결) = **점수 폴백**
        즉 by=lexical 인 레벨이 아직 스코어가 결정하는 자리(질문/다중O 대상)."""
        if not isinstance(candidateCodeSet, Mapping):
            return {}
        trace = candidateCodeSet.get("staged_trace") or {}
        stages = trace.get("stages") if isinstance(trace, Mapping) else None
        if not isinstance(stages, list):
            return {}
        out: Dict[str, object] = {}
        for stage in stages:
            if not isinstance(stage, Mapping):
                continue
            level = str(stage.get("stage") or "")
            if level not in ("hs4", "hs6", "cn8"):
                continue
            selectedCodes = stage.get("selected_codes") or []
            selected = str(selectedCodes[0]) if selectedCodes else ""
            status = str(stage.get("status") or "")
            cand: Mapping = {}
            for c in stage.get("candidates_considered") or []:
                if isinstance(c, Mapping) and str(c.get("code")) == selected:
                    cand = c
                    break
            detail = cand.get("decision_detail") or []
            preds = cand.get("predicate_results") or []
            passedO = sorted({
                str(d.get("cond") or "") for d in detail
                if isinstance(d, Mapping) and str(d.get("verdict")) == "true"
            } - {""})
            violatedX = sorted({
                str(d.get("cond") or "") for d in detail
                if isinstance(d, Mapping) and str(d.get("verdict")) == "false"
            } - {""})
            predTrue = sum(
                1 for p in preds
                if isinstance(p, Mapping) and str(p.get("verdict")) == "true"
            )
            silent = [str(a) for a in (stage.get("missing_facts") or [])]
            decision = str(cand.get("decision") or "")
            gri3 = cand.get("gri3")
            if status == "pass_through":
                by = "pass_through"
            elif decision == "confirmed":
                by = "confirmed"
            elif cand.get("residual"):
                by = "residual"
            elif gri3:
                by = f"gri3:{gri3}"
            elif passedO or predTrue:
                by = "partial"
            else:
                by = "lexical(미해결)"
            out[level] = {
                "code": selected,
                "by": by,
                "O": passedO[:6],
                "X": violatedX[:4],
                "SILENT": silent[:4],
                "predicate_true": predTrue,
            }
        return out

    @staticmethod
    def _CandidateDecidedBy(candidate: object) -> str:
        """후보 단위 결정 근거 요약 — 스코어 미출력, classification_basis 압축."""
        if not isinstance(candidate, Mapping):
            return ""
        basis = candidate.get("classification_basis") or []
        if isinstance(basis, list) and basis:
            return "; ".join(str(b)[:48] for b in basis[:3])
        return str(candidate.get("hard_condition_status") or "")

    def _LogAnswerRecall(
        self,
        logger: _BoundLogger,
        answerRecall: JsonMapping,
    ) -> None:
        if not answerRecall.get("answer_found"):
            logger.info(
                "pipeline_step=evaluation component=KurlyMarketSmokeRunner output_dto=AnswerRecallSummary status=skipped reason={}",
                answerRecall.get("reason"),
            )
            return
        answer = answerRecall.get("answer") or {}
        if not isinstance(answer, Mapping):
            answer = {}
        logger.info(
            "pipeline_step=evaluation component=KurlyMarketSmokeRunner output_dto=AnswerRecallSummary expected_taric10={} answer_product_id={}",
            answer.get("taric10"),
            answer.get("product_id"),
        )
        levels = answerRecall.get("levels") or {}
        if not isinstance(levels, Mapping):
            return
        for levelName, levelData in levels.items():
            if not isinstance(levelData, Mapping):
                continue
            logger.info(
                (
                    "pipeline_step=evaluation component=KurlyMarketSmokeRunner "
                    "output_dto=AnswerRecallLevel level={} expected={} top1={} top1_match={} "
                    "top5_match={} llm_recommended={} "
                    "llm_recommended_match={} top5_codes={}"
                ),
                levelName,
                levelData.get("expected"),
                levelData.get("top1_code"),
                levelData.get("top1_match"),
                levelData.get("top5_match"),
                levelData.get("llm_recommended_code"),
                levelData.get("llm_recommended_match"),
                levelData.get("top5_codes"),
            )

    def _LogSummary(self, results: List[Dict[str]]) -> None:
        summaryLogger = self._Logger("_LogSummary")
        parseOkCount = sum(
            1 for result in results if result["status"].get("is_parse_ok")
        )
        ocrOkCount = sum(
            1 for result in results if result["status"].get("is_ocr_fallback_ok")
        )
        summaryLogger.info(
            "summary parse_ok={}/{} ocr_fallback_ok={}/{}",
            parseOkCount,
            len(results),
            ocrOkCount,
            len(results),
        )
        recallSummary = self._BuildAnswerRecallSummary(results)
        if recallSummary["evaluated_count"]:
            for levelName, levelSummary in recallSummary["levels"].items():
                summaryLogger.info(
                    (
                        "classification_recall level={} evaluated={} "
                        "top1={}/{} ({}) top5={}/{} ({}) "
                        "final_recommended={}/{} ({})"
                    ),
                    levelName,
                    levelSummary["evaluated_count"],
                    levelSummary["top1_match_count"],
                    levelSummary["evaluated_count"],
                    levelSummary["top1_recall"],
                    levelSummary["top5_match_count"],
                    levelSummary["evaluated_count"],
                    levelSummary["top5_recall"],
                    levelSummary["llm_recommended_match_count"],
                    levelSummary["llm_recommended_evaluated_count"],
                    levelSummary["llm_recommended_recall"],
                )
        elif any("classification_smoke" in result for result in results):
            summaryLogger.info(
                "classification_recall skipped answer_source={} matched_rows=0",
                "classification_smoke_case",
            )
        if self._logFullResult:
            summaryLogger.info(
                "\n{}",
                json.dumps(results, ensure_ascii=False, indent=2),
            )

    @staticmethod
    def _BuildAnswerRecallSummary(
        results: Sequence[JsonMapping],
    ) -> Dict[str]:
        levelTotals = {
            levelName: {
                "evaluated_count": 0,
                "top1_match_count": 0,
                "top5_match_count": 0,
                "llm_recommended_evaluated_count": 0,
                "llm_recommended_match_count": 0,
            }
            for levelName, _ in RECALL_LEVELS
        }
        for result in results:
            classificationSmoke = result.get("classification_smoke") or {}
            if not isinstance(classificationSmoke, Mapping):
                continue
            answerRecall = classificationSmoke.get("answer_recall") or {}
            if not isinstance(answerRecall, Mapping) or not answerRecall.get(
                "answer_found"
            ):
                continue
            levels = answerRecall.get("levels") or {}
            if not isinstance(levels, Mapping):
                continue
            for levelName, _ in RECALL_LEVELS:
                levelData = levels.get(levelName) or {}
                if not isinstance(levelData, Mapping):
                    continue
                levelTotals[levelName]["evaluated_count"] += 1
                if levelData.get("top1_match"):
                    levelTotals[levelName]["top1_match_count"] += 1
                if levelData.get("top5_match"):
                    levelTotals[levelName]["top5_match_count"] += 1
                if levelData.get("llm_recommended_code"):
                    levelTotals[levelName][
                        "llm_recommended_evaluated_count"
                    ] += 1
                if levelData.get("llm_recommended_match"):
                    levelTotals[levelName]["llm_recommended_match_count"] += 1

        levelsOut: dict[str, JsonObject] = {}
        evaluatedCounts: list[int] = []
        for levelName, totals in levelTotals.items():
            evaluatedCount = totals["evaluated_count"]
            evaluatedCounts.append(evaluatedCount)
            top1Count = totals["top1_match_count"]
            top5Count = totals["top5_match_count"]
            llmRecommendedEvaluatedCount = totals[
                "llm_recommended_evaluated_count"
            ]
            llmRecommendedCount = totals["llm_recommended_match_count"]
            levelsOut[levelName] = {
                **totals,
                "top1_recall": (
                    round(top1Count / evaluatedCount, 4)
                    if evaluatedCount
                    else 0.0
                ),
                "top5_recall": (
                    round(top5Count / evaluatedCount, 4)
                    if evaluatedCount
                    else 0.0
                ),
                "llm_recommended_recall": (
                    round(
                        llmRecommendedCount / llmRecommendedEvaluatedCount,
                        4,
                    )
                    if llmRecommendedEvaluatedCount
                    else 0.0
                ),
            }
        return {
            "evaluated_count": max(evaluatedCounts) if evaluatedCounts else 0,
            "levels": levelsOut,
        }

    def _WriteSummaryArtifact(self, results: List[Dict[str]]) -> None:
        self._summaryArtifactPath.parent.mkdir(parents=True, exist_ok=True)
        self._summaryArtifactPath.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._Logger("_WriteSummaryArtifact").info(
            "summary_artifact_path={}",
            self._summaryArtifactPath,
        )

    def _BuildInputReconstructionView(
        self,
        inputReconstruction: Dict[str],
    ) -> Dict[str]:
        facts = [
            self._BuildReconstructedFactView(factRecord)
            for factRecord in inputReconstruction.get("product_facts", []) or []
            if isinstance(factRecord, dict)
        ]
        unresolvedFacts = [
            self._BuildReconstructedFactView(factRecord)
            for factRecord in inputReconstruction.get("unresolved_facts", []) or []
            if isinstance(factRecord, dict)
        ]
        usedLlmReconstruction = bool(
            inputReconstruction.get("used_llm_reconstruction"),
        )
        if usedLlmReconstruction:
            method = "llm"
        elif facts:
            method = "deterministic"
        else:
            method = "none"

        return {
            "method": method,
            "used_llm_reconstruction": usedLlmReconstruction,
            "fallback_reason": inputReconstruction.get("fallback_reason"),
            "facts": facts,
            "reconstructed_tables": inputReconstruction.get(
                "reconstructed_tables",
                [],
            )
            or [],
            "unresolved_facts": unresolvedFacts,
            "evidence_traces": inputReconstruction.get("evidence_traces", []) or [],
            "missing_fact_reasons": inputReconstruction.get(
                "missing_fact_reasons",
                [],
            )
            or [],
            "conflicts": inputReconstruction.get("conflicts", []) or [],
            "fact_texts_for_classification": inputReconstruction.get(
                "normalized_fact_texts",
                [],
            )
            or [],
            "warnings": inputReconstruction.get("warnings", []) or [],
        }

    @staticmethod
    def _BuildReconstructedFactView(
        factRecord: Dict[str],
    ) -> Dict[str]:
        reconstructedValue = factRecord.get("normalized_value") or ""
        return {
            "field_name": factRecord.get("field_name"),
            "reconstructed_value": reconstructedValue,
            "source_refs": factRecord.get("source_refs", []) or [],
            "reconstruction_type": factRecord.get("correction_type"),
            "validation_status": factRecord.get("validation_status"),
        }

    @staticmethod
    def _BuildTextPreview(text: object, maxCharacters: int) -> object:
        if not isinstance(text, str):
            return text
        if len(text) <= maxCharacters:
            return text
        return "{0}...".format(text[:maxCharacters])

    def _BuildOptionPreview(
        self,
        noticeOptions: List[Dict[str]],
    ) -> List[Dict[str]]:
        optionPreview: List[Dict[str]] = []
        for optionIndex, noticeOption in enumerate(
            noticeOptions[:self._maxLoggedNoticeOptions],
            start=1,
        ):
            optionPreview.append(
                {
                    "index": optionIndex,
                    "option_name": noticeOption["option_name"],
                    "field_count": len(noticeOption["fields"]),
                    "fields_preview": [
                        self._BuildFieldRecordPreview(fieldRecord)
                        for fieldRecord in noticeOption["fields"][
                            :self._maxLoggedFieldsPerOption
                        ]
                    ],
                }
            )
        return optionPreview

    @staticmethod
    def _CountNoticeOptionFields(
        noticeOptions: List[Dict[str]],
    ) -> int:
        seenFieldKeys = set()
        for noticeOption in noticeOptions:
            fields = noticeOption.get("fields", [])
            if not isinstance(fields, list):
                continue
            for fieldRecord in fields:
                if not isinstance(fieldRecord, dict):
                    continue
                seenFieldKeys.add(
                    (
                        fieldRecord.get("field_name"),
                        fieldRecord.get("field_value"),
                    )
                )
        return len(seenFieldKeys)

    def _BuildFieldRecordPreview(
        self,
        fieldRecord: Dict[str],
    ) -> Dict[str]:
        return {
            "field_name": fieldRecord["field_name"],
            "field_value": self._BuildTextPreview(
                fieldRecord["field_value"],
                self._fieldValuePreviewCharacters,
            ),
            "requires_ocr_fallback": fieldRecord["requires_ocr_fallback"],
        }

    @staticmethod
    def _ConfigureLogger() -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="[%(levelname)s] %(message)s",
            stream=sys.stderr,
            force=True,
        )

    def _Logger(self, functionName: str) -> _BoundLogger:
        return _BoundLogger(LOGGER, self.__class__.__name__, functionName)


if __name__ == "__main__":
    cliArguments = ParseArguments()
    KurlyMarketSmokeRunner(
        showBrowser=cliArguments.headed,
        compareOcr=cliArguments.compare_ocr,
        compareMaxImages=cliArguments.compare_max_images,
        checkUiBinding=cliArguments.check_ui_binding,
        classifyReconstruction=cliArguments.classify_reconstruction,
        stage1ReviewMode=cliArguments.stage1_review_mode,
        writeSummaryArtifact=cliArguments.write_summary_artifact,
        reuseOcrImageArtifacts=cliArguments.reuse_ocr_image_artifacts,
        useCachedProductInput=cliArguments.use_cached_product_input,
        loadDotEnv=cliArguments.load_dotenv,
        productUrls=cliArguments.product_urls,
    ).Run()
