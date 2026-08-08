"""Kurly URL product facts collection."""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from bussiness_logic.artifact_paths import ExtractProductIdFromUrl
from bussiness_logic.pipeline.run_paths import APP_CONFIG, PROJECT_ROOT
from bussiness_logic.product.ocr.ocr_execution import (
    SHARED_OCR_EXECUTION_COORDINATOR,
)
from bussiness_logic.utils.json_types import JsonObject

if TYPE_CHECKING:
    from bussiness_logic.input_process.reconstruction import (
        ProductInputReconstructionService,
    )


class KurlyUrlIntakePublicResult(Protocol):
    def BuildPublicResult(self) -> JsonObject:
        """Return the JSON-safe public pipeline result."""


class ReconstructionDumpable(Protocol):
    def model_dump(
        self,
        *,
        mode: str,
        by_alias: bool,
        include: set[str],
    ) -> JsonObject:
        """Return the JSON-safe reconstruction DTO payload."""


PRODUCT_INPUT_ARTIFACT_ROOT = APP_CONFIG.paths.ResolvePath(
    PROJECT_ROOT,
    APP_CONFIG.paths.product_input_artifact_root,
)
@lru_cache(maxsize=1)
def _BuildKurlyOcrEngines() -> tuple[object, object | None]:
    """프로세스 수명 동안 무거운 Paddle OCR 모델을 재사용한다."""

    from bussiness_logic.product.ocr.paddle_ocr import (
        PaddleOcrEngine,
        ProductStructuredOcrEngine,
    )
    from bussiness_logic.product.ocr.vlm_adapter import BuildProductVlmAdapter

    smokeConfig = APP_CONFIG.kurly_smoke
    if not smokeConfig.use_structured_ocr:
        return PaddleOcrEngine(), None
    return (
        ProductStructuredOcrEngine(
            vlExtraOptions=smokeConfig.BuildStructuredOcrVlExtraOptions(),
            vlPipeline=BuildProductVlmAdapter(
                smokeConfig.structured_ocr_provider,
            ),
            useProjectionTiling=(
                smokeConfig.structured_ocr_use_projection_tiling
            ),
            maxTileHeightPixels=(
                smokeConfig.structured_ocr_max_tile_height_pixels
            ),
            maxTileSidePixels=smokeConfig.structured_ocr_max_tile_side_pixels,
            tileOverlapPixels=smokeConfig.structured_ocr_tile_overlap_pixels,
            allowHardCutFallback=(
                smokeConfig.structured_ocr_allow_hard_cut_fallback
            ),
        ),
        PaddleOcrEngine(),
    )


def BuildKurlyUrlFactsFromPipelineResult(
    url: str,
    result: KurlyUrlIntakePublicResult,
    *,
    artifact_root: Path,
    warnings: list[str] | None = None,
    write_product_input_artifact: bool = True,
) -> JsonObject:
    """Project a Kurly pipeline result into the shared product facts shape."""

    from bussiness_logic.input_process.product_input_adapter import ProductInputAdapter

    product_input = ProductInputAdapter().BuildFromObject(result)
    public_result = result.BuildPublicResult()
    source_product_page = public_result.get("source_product_page") or {}
    collection_summary = public_result.get("collection") or {}
    ocr_summary = public_result.get("ocr") or {}
    image_evidence_items = public_result.get("image_evidence_items") or []
    pipeline_steps = public_result.get("pipeline_steps") or []
    input_reconstruction = public_result.get("input_reconstruction") or {}
    productId = ExtractProductIdFromUrl(url)
    productArtifactDirectory = Path(artifact_root) / productId
    if not isinstance(source_product_page, dict):
        source_product_page = {}
    if not isinstance(collection_summary, dict):
        collection_summary = {}
    if not isinstance(ocr_summary, dict):
        ocr_summary = {}
    if not isinstance(image_evidence_items, list):
        image_evidence_items = []
    if not isinstance(pipeline_steps, list):
        pipeline_steps = []
    if not isinstance(input_reconstruction, dict):
        input_reconstruction = {}

    mergedWarnings = list(warnings or [])
    mergedWarnings.extend(
        str(warning)
        for warning in public_result.get("warnings", [])
        if str(warning).strip()
    )
    mergedWarnings = list(dict.fromkeys(mergedWarnings))

    reconstructed_product_facts = (
        input_reconstruction.get("reconstructed_product_facts") or []
    )
    unresolved_product_facts = (
        input_reconstruction.get("unresolved_product_facts") or []
    )
    product_fact_conflicts = (
        input_reconstruction.get("product_fact_conflicts") or []
    )
    reconstructed_fact_texts = (
        input_reconstruction.get("reconstructed_fact_texts") or []
    )
    facts = {
        "url": url,
        "source_urls": [url],
        "product_id": productId,
        "product_name": product_input.productName or "",
        "description": product_input.shortDescription or product_input.productNoticeText or "",
        "short_description": product_input.shortDescription or "",
        "product_domain": product_input.productDomain or "unknown",
        "source_product_page": source_product_page,
        "ocr_text": [product_input.ocrText] if product_input.ocrText else [],
        "reconstructed_product_facts": reconstructed_product_facts,
        "unresolved_product_facts": unresolved_product_facts,
        "product_fact_conflicts": product_fact_conflicts,
        "reconstructed_fact_texts": reconstructed_fact_texts,
        "warnings": mergedWarnings,
        "url_intake": {
            "artifact_root": str(productArtifactDirectory),
            "pipeline_steps": pipeline_steps,
            "collection": collection_summary,
            "ocr": ocr_summary,
            "image_evidence_items": image_evidence_items,
            "ocr_image_count": ocr_summary.get("image_result_count", 0),
            "combined_ocr_text_length": ocr_summary.get("combined_text_length", 0),
            "parse_warning_count": collection_summary.get("warning_count", 0),
        },
        "input_reconstruction": input_reconstruction,
    }
    productInputArtifactPath = productArtifactDirectory / "product-input.json"
    if write_product_input_artifact:
        productArtifactDirectory.mkdir(parents=True, exist_ok=True)
        facts["url_intake"]["product_input_artifact"] = str(productInputArtifactPath)
        temporaryArtifactPath = productInputArtifactPath.with_suffix(".json.tmp")
        temporaryArtifactPath.write_text(
            json.dumps(facts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporaryArtifactPath.replace(productInputArtifactPath)
    return facts


def CollectKurlyUrlFacts(
    url: str,
    *,
    run_ocr: bool | None = None,
    headless: bool | None = None,
    timeout_seconds: int | None = None,
    scroll_count: int | None = None,
    max_ocr_images: int | None = None,
    imageStatusCallback: Callable[[list[JsonObject]], None] | None = None,
) -> JsonObject:
    """Collect product facts from a Kurly product URL.

    This keeps URL/OCR intake outside the web request handler and before
    Evidence_Intake_Component, so the Blackboard still starts from normalized
    product facts.
    """
    from bussiness_logic.product.pipeline.kurly_url_intake_pipeline import (
        KurlyUrlIntakePipeline,
    )
    from bussiness_logic.product.pipeline.kurly_url_intake_schema import (
        KurlyUrlIntakeInput,
    )
    from bussiness_logic.product.web_parser.kurly_domestic import KurlyDomesticPageParser
    from bussiness_logic.product.web_parser.kurly_global import KurlyGlobalPageParser
    from bussiness_logic.product.web_parser.kurly_market_collector import KurlyPageCollector
    from bussiness_logic.product.web_parser.kurly_page_adapter import KurlyPageAdapter

    warnings: list[str] = []
    smoke_config = APP_CONFIG.kurly_smoke
    run_ocr = smoke_config.run_ocr_fallback if run_ocr is None else run_ocr
    headless = smoke_config.headless if headless is None else headless
    timeout_seconds = (
        smoke_config.timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    scroll_count = smoke_config.scroll_count if scroll_count is None else scroll_count
    max_ocr_images = (
        smoke_config.max_ocr_image_count
        if max_ocr_images is None
        else max_ocr_images
    )

    pageAdapter = KurlyPageAdapter(
        domesticParser=KurlyDomesticPageParser(),
        globalParser=KurlyGlobalPageParser(),
    )
    collector = KurlyPageCollector(
        parser=pageAdapter,
        headless=headless,
        timeoutMilliseconds=timeout_seconds * 1000,
        scrollCount=scroll_count,
    )
    input_reconstruction_service = _BuildInputReconstructionService(warnings)

    if run_ocr:
        try:
            ocr_engine, screening_ocr_engine = (
                SHARED_OCR_EXECUTION_COORDINATOR.Execute(_BuildKurlyOcrEngines)
            )
            pipeline = KurlyUrlIntakePipeline(
                collector=collector,
                ocrEngine=ocr_engine,
                screeningOcrEngine=screening_ocr_engine,
                inputReconstructionService=input_reconstruction_service,
                imageStatusCallback=imageStatusCallback,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"ocr_engine_unavailable: {exc}")
            pipeline = KurlyUrlIntakePipeline(
                collector=collector,
                inputReconstructionService=input_reconstruction_service,
                imageStatusCallback=imageStatusCallback,
            )
            run_ocr = False
    else:
        pipeline = KurlyUrlIntakePipeline(
            collector=collector,
            inputReconstructionService=input_reconstruction_service,
            imageStatusCallback=imageStatusCallback,
        )

    artifact_root = PRODUCT_INPUT_ARTIFACT_ROOT
    artifact_root.mkdir(parents=True, exist_ok=True)
    pipelineInput = KurlyUrlIntakeInput(
        productPageUrl=url,
        runOcrFallback=run_ocr,
        artifactRootPath=artifact_root,
        maxOcrImageCount=max_ocr_images,
    )
    result = pipeline.Run(pipelineInput)
    return BuildKurlyUrlFactsFromPipelineResult(
        url,
        result,
        artifact_root=artifact_root,
        warnings=warnings,
    )


def RerunCachedInputReconstruction(product_identifier: str) -> JsonObject:
    """저장된 OCR evidence request를 재사용해 LLM reconstruction만 다시 실행한다."""

    from bussiness_logic.input_process.reconstruction import InputEvidencePackage

    warnings: list[str] = []
    productId = ExtractProductIdFromUrl(product_identifier)
    artifactDirectory = PRODUCT_INPUT_ARTIFACT_ROOT / productId
    requestArtifactPath = artifactDirectory / "llm-input-reconstruction-request.json"
    if not requestArtifactPath.exists():
        raise FileNotFoundError(
            "cached_llm_reconstruction_request_not_found: {0}".format(
                requestArtifactPath,
            )
        )

    requestArtifact = json.loads(requestArtifactPath.read_text(encoding="utf-8"))
    requestPayload = requestArtifact.get("request") or {}
    userPrompt = requestPayload.get("user_prompt") or requestPayload.get("userPrompt")
    if not isinstance(userPrompt, str) or not userPrompt.strip():
        raise ValueError("cached reconstruction request has no user_prompt.")
    contextPayload = json.loads(userPrompt.strip().splitlines()[-1])
    evidencePackage = InputEvidencePackage.model_validate(
        {
            "product_page_url": (
                requestArtifact.get("product_page_url")
                or product_identifier
            ),
            "records": contextPayload.get("evidence") or [],
        }
    )

    reconstructionService = _BuildInputReconstructionService(warnings)
    if reconstructionService is None:
        raise RuntimeError("input_reconstruction is disabled by app config.")
    reconstructionResult = reconstructionService.ReconstructFromEvidencePackage(
        evidencePackage,
    )
    cachedFacts = _ReadCachedProductInputFacts(artifactDirectory)
    inputReconstruction = _BuildPublicInputReconstruction(reconstructionResult)
    cachedFacts.update(
        {
            "url": (
                cachedFacts.get("url")
                or requestArtifact.get("product_page_url")
                or product_identifier
            ),
            "source_urls": cachedFacts.get("source_urls")
            or [requestArtifact.get("product_page_url") or product_identifier],
            "product_id": productId,
            "input_reconstruction": inputReconstruction,
            "reconstructed_product_facts": inputReconstruction[
                "reconstructed_product_facts"
            ],
            "unresolved_product_facts": inputReconstruction[
                "unresolved_product_facts"
            ],
            "product_fact_conflicts": inputReconstruction["product_fact_conflicts"],
            "reconstructed_fact_texts": inputReconstruction[
                "reconstructed_fact_texts"
            ],
            "warnings": list(
                dict.fromkeys(
                    [
                        *warnings,
                        *cachedFacts.get("warnings", []),
                        *inputReconstruction.get("warnings", []),
                    ]
                )
            ),
        }
    )
    return cachedFacts


def _BuildInputReconstructionService(
    warnings: list[str],
) -> "ProductInputReconstructionService | None":
    from bussiness_logic.app_config import LlmProfileName
    from bussiness_logic.bridge.factory import (
        RuntimeAdapterBuildError,
    )
    from bussiness_logic.bridge.runtime_adapter import (
        BuildPipelineRuntimeAdapter,
    )
    from bussiness_logic.bridge.selector import UnsupportedLlmRuntimeError
    from bussiness_logic.input_process.reconstruction import (
        ProductInputReconstructionService,
    )

    smoke_config = APP_CONFIG.kurly_smoke
    if not smoke_config.use_input_reconstruction:
        return None
    runtime_adapter = None
    if smoke_config.use_llm_input_reconstruction:
        try:
            runtime_adapter = BuildPipelineRuntimeAdapter(
                LlmProfileName.INPUT_RECONSTRUCTION,
            )
        except (RuntimeAdapterBuildError, UnsupportedLlmRuntimeError) as exc:
            warnings.append(f"llm_input_reconstruction_unavailable: {exc}")
    return ProductInputReconstructionService(
        runtimeAdapter=runtime_adapter,
        fuzzyMinRatio=smoke_config.input_dictionary_fuzzy_min_ratio,
        llmMaxTokens=smoke_config.llm_input_reconstruction_max_tokens,
        llmArtifactRootPath=PRODUCT_INPUT_ARTIFACT_ROOT,
    )


def _ReadCachedProductInputFacts(artifactDirectory: Path) -> JsonObject:
    artifactPath = artifactDirectory / "product-input.json"
    if not artifactPath.exists():
        return {}
    payload = json.loads(artifactPath.read_text(encoding="utf-8"))
    facts = dict(payload) if isinstance(payload, dict) else {}
    # 이전 수집기가 근거 없이 저장한 정확한 기본값만 재사용 시 제거한다.
    if facts.get("origin_country") == "KR":
        facts.pop("origin_country")
    if facts.get("intended_use") == "human consumption":
        facts.pop("intended_use")
    return facts


def LoadCachedProductInputFacts(product_identifier: str) -> JsonObject:
    """저장된 product-input.json을 읽어 downstream pipeline 입력으로 재사용한다."""

    productId = ExtractProductIdFromUrl(product_identifier)
    artifactDirectory = PRODUCT_INPUT_ARTIFACT_ROOT / productId
    facts = _ReadCachedProductInputFacts(artifactDirectory)
    if not facts:
        raise FileNotFoundError(
            "cached product-input.json not found: {0}".format(
                artifactDirectory / "product-input.json",
            )
        )
    facts.setdefault("product_id", productId)
    if product_identifier.startswith("http"):
        facts.setdefault("url", product_identifier)
        facts.setdefault("source_urls", [product_identifier])
    return facts


def _BuildPublicInputReconstruction(
    reconstructionResult: ReconstructionDumpable,
) -> JsonObject:
    reconstructionData = reconstructionResult.model_dump(
        mode="json",
        by_alias=True,
        include={
            "productFacts",
            "reconstructedTables",
            "unresolvedFacts",
            "evidenceTraces",
            "missingFactReasons",
            "conflicts",
            "normalizedFactTexts",
            "warnings",
            "usedLlmReconstruction",
            "fallbackReason",
            "sourceRefLabels",
            "sourceEvidencePreview",
        },
    )
    productFacts = list(reconstructionData.get("product_facts", []))
    reconstructedTables = list(reconstructionData.get("reconstructed_tables", []))
    unresolvedFacts = list(reconstructionData.get("unresolved_facts", []))
    evidenceTraces = list(reconstructionData.get("evidence_traces", []))
    missingFactReasons = list(reconstructionData.get("missing_fact_reasons", []))
    factTexts = list(reconstructionData.get("normalized_fact_texts", []))
    usedLlm = bool(reconstructionData.get("used_llm_reconstruction"))
    fallbackReason = reconstructionData.get("fallback_reason")
    return {
        "mode": (
            "llm_reconstruction"
            if usedLlm
            else "fallback_reconstruction"
            if productFacts or factTexts
            else "unavailable"
        ),
        "used_llm_reconstruction": usedLlm,
        "fallback_reason": fallbackReason,
        "error": (
            fallbackReason
            if fallbackReason
            and fallbackReason not in {"llm_reconstruction_not_used"}
            else None
        ),
        "fact_count": len(productFacts),
        "reconstructed_table_count": len(reconstructedTables),
        "unresolved_count": len(unresolvedFacts),
        "evidence_trace_count": len(evidenceTraces),
        "missing_fact_reason_count": len(missingFactReasons),
        "conflict_count": len(reconstructionData.get("conflicts", [])),
        "fact_text_count": len(factTexts),
        "reconstructed_product_facts": productFacts,
        "reconstructed_tables": reconstructedTables,
        "unresolved_product_facts": unresolvedFacts,
        "evidence_traces": evidenceTraces,
        "missing_fact_reasons": missingFactReasons,
        "product_fact_conflicts": list(reconstructionData.get("conflicts", [])),
        "reconstructed_fact_texts": factTexts,
        "source_ref_labels": dict(reconstructionData.get("source_ref_labels", {})),
        "source_evidence_preview": list(
            reconstructionData.get("source_evidence_preview", [])
        ),
        "warnings": list(reconstructionData.get("warnings", [])),
    }
