"""End-to-end UI pipeline: product input -> classifier -> documents.

This module is intentionally thin. It does not classify or recommend documents
itself; it only runs the existing Blackboard agents in order and returns the
Document_Agent output that the Dash UI can render.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(os.environ.get("ASAP_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()
SRC_ROOT = PROJECT_ROOT / "src"
for _path in (PROJECT_ROOT, SRC_ROOT):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agents.classification_agent import ClassificationAgent
from agents.document_agent import DocumentAgent
from agents.evidence_intake_agent import EvidenceIntakeAgent
from agents.orchestrator_agent import OrchestratorAgent
from agents.blackboard import BlackboardStore
from bussiness_logic.app_config import LoadAppConfig


APP_CONFIG = LoadAppConfig(PROJECT_ROOT)
URL_INTAKE_ARTIFACT_ROOT = APP_CONFIG.paths.ResolvePath(
    PROJECT_ROOT,
    APP_CONFIG.paths.dash_url_intake_artifact_root,
)


def _read_agent_runs(store: BlackboardStore) -> list[dict[str, Any]]:
    if not store.runs_path.exists():
        return []
    out: list[dict[str, Any]] = []
    with store.runs_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"raw": line, "parse_error": "invalid_jsonl"})
    return out


def collect_kurly_url_facts(
    url: str,
    *,
    run_ocr: bool | None = None,
    headless: bool | None = None,
    timeout_seconds: int | None = None,
    scroll_count: int | None = None,
    max_ocr_images: int | None = None,
) -> dict[str, Any]:
    """Collect product facts from a Kurly product URL.

    This keeps URL/OCR intake outside the Dash callback and before
    Evidence_Intake_Agent, so the Blackboard still starts from normalized
    product facts.
    """
    from bussiness_logic.product import (
        KurlyGlobalPageParser,
        KurlyPageAdapter,
        KurlyPageCollector,
        KurlyDomesticPageParser,
        KurlyPipelineInput,
        KurlyProductPipeline,
        PaddleOcrEngine,
        PaddleStructureOcrEngine,
    )
    from bussiness_logic.bridge import (
        BuildLlmRuntimeConfigFromEnv,
        BuildRuntimeAdapter,
        RuntimeAdapterBuildError,
    )
    from bussiness_logic.input_process import (
        ProductInputAdapter,
        ProductInputReconstructionService,
    )

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
    input_reconstruction_service = None
    if smoke_config.use_input_reconstruction:
        runtime_adapter = None
        if smoke_config.use_llm_input_reconstruction:
            try:
                runtime_adapter = BuildRuntimeAdapter(
                    BuildLlmRuntimeConfigFromEnv(projectRootPath=PROJECT_ROOT),
                    requireAvailable=True,
                )
            except RuntimeAdapterBuildError as exc:
                warnings.append(f"llm_input_reconstruction_unavailable: {exc}")
        input_reconstruction_service = ProductInputReconstructionService(
            dictionaryPath=(
                str(
                    APP_CONFIG.paths.ResolvePath(
                        PROJECT_ROOT,
                        smoke_config.input_dictionary_path,
                    )
                )
                if smoke_config.input_dictionary_path is not None
                else None
            ),
            runtimeAdapter=runtime_adapter,
            fuzzyMinRatio=smoke_config.input_dictionary_fuzzy_min_ratio,
            llmMaxTokens=smoke_config.llm_input_reconstruction_max_tokens,
            llmDebugArtifactRootPath=(
                URL_INTAKE_ARTIFACT_ROOT
                if (
                    runtime_adapter is not None
                    and smoke_config.write_llm_input_reconstruction_debug_artifacts
                )
                else None
            ),
        )

    if run_ocr:
        try:
            ocr_engine = (
                PaddleStructureOcrEngine(
                    useProjectionTiling=(
                        smoke_config.structured_ocr_use_projection_tiling
                    ),
                    maxTileHeightPixels=(
                        smoke_config.structured_ocr_max_tile_height_pixels
                    ),
                    maxTileSidePixels=smoke_config.structured_ocr_max_tile_side_pixels,
                    tileOverlapPixels=smoke_config.structured_ocr_tile_overlap_pixels,
                    allowHardCutFallback=(
                        smoke_config.structured_ocr_allow_hard_cut_fallback
                    ),
                )
                if smoke_config.use_structured_ocr
                else PaddleOcrEngine()
            )
            pipeline = KurlyProductPipeline(
                collector=collector,
                ocrEngine=ocr_engine,
                inputReconstructionService=input_reconstruction_service,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"ocr_engine_unavailable: {exc}")
            pipeline = KurlyProductPipeline(
                collector=collector,
                inputReconstructionService=input_reconstruction_service,
            )
            run_ocr = False
    else:
        pipeline = KurlyProductPipeline(
            collector=collector,
            inputReconstructionService=input_reconstruction_service,
        )

    artifact_root = URL_INTAKE_ARTIFACT_ROOT
    artifact_root.mkdir(parents=True, exist_ok=True)
    result = pipeline.Run(
        KurlyPipelineInput(
            productPageUrl=url,
            runOcrFallback=run_ocr,
            artifactRootPath=artifact_root,
            maxOcrImageCount=max_ocr_images,
        )
    )
    product_input = ProductInputAdapter().BuildFromObject(result)
    public_result = result.BuildPublicResult()
    source_product_page = public_result.get("source_product_page") or {}
    collection_summary = public_result.get("collection") or {}
    ocr_summary = public_result.get("ocr") or {}
    pipeline_steps = public_result.get("pipeline_steps") or []
    input_reconstruction = public_result.get("input_reconstruction") or {}
    if not isinstance(source_product_page, dict):
        source_product_page = {}
    if not isinstance(collection_summary, dict):
        collection_summary = {}
    if not isinstance(ocr_summary, dict):
        ocr_summary = {}
    if not isinstance(pipeline_steps, list):
        pipeline_steps = []
    if not isinstance(input_reconstruction, dict):
        input_reconstruction = {}
    warnings.extend(
        str(warning)
        for warning in public_result.get("warnings", [])
        if str(warning).strip()
    )
    warnings = list(dict.fromkeys(warnings))
    classification_input_product_facts = (
        input_reconstruction.get("classification_input_product_facts") or []
    )
    unresolved_product_facts = (
        input_reconstruction.get("unresolved_product_facts") or []
    )
    product_fact_conflicts = (
        input_reconstruction.get("product_fact_conflicts") or []
    )
    classification_input_text_lines = (
        input_reconstruction.get("classification_input_fact_texts") or []
    )
    facts = {
        "url": url,
        "source_urls": [url],
        "product_name": product_input.productName or "",
        "description": product_input.shortDescription or product_input.productNoticeText or "",
        "short_description": product_input.shortDescription or "",
        "product_domain": product_input.productDomain or "unknown",
        "source_product_page": source_product_page,
        "ocr_text": [product_input.ocrText] if product_input.ocrText else [],
        "ingredient_list": list(product_input.normalizedOcrFactTexts or []),
        "classification_input_product_facts": classification_input_product_facts,
        "unresolved_product_facts": unresolved_product_facts,
        "product_fact_conflicts": product_fact_conflicts,
        "classification_input_fact_texts": classification_input_text_lines,
        "origin_country": "KR",
        "intended_use": "human consumption",
        "warnings": warnings,
        "url_intake": {
            "artifact_root": str(artifact_root),
            "pipeline_steps": pipeline_steps,
            "collection": collection_summary,
            "ocr": ocr_summary,
            "ocr_image_count": ocr_summary.get("image_result_count", 0),
            "combined_ocr_text_length": ocr_summary.get("combined_text_length", 0),
            "parse_warning_count": collection_summary.get("warning_count", 0),
        },
        "input_reconstruction": input_reconstruction,
    }
    return facts


def build_raw_input_from_ui(
    *,
    query: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    """Map Dash text + Product facts JSON into EvidenceIntakeAgent input."""
    facts = _normalize_product_facts(facts or {})
    url = str(facts.get("url") or "").strip()
    if url and (
        "kurly.com/goods/" in url
        or "kurlyglobal.com/products/" in url
        or "kurlyglobal.com/en/products/" in url
    ):
        try:
            collected = collect_kurly_url_facts(url)
            merged = dict(facts)
            for key, value in collected.items():
                if value not in ("", [], None):
                    merged[key] = value
            facts = _normalize_product_facts(merged)
        except Exception as exc:  # noqa: BLE001
            facts.setdefault("warnings", [])
            facts["warnings"].append(f"kurly_url_intake_failed: {exc}")

    source_urls = facts.get("source_urls") or facts.get("source_url") or facts.get("url") or []
    if isinstance(source_urls, str):
        source_urls = [source_urls] if source_urls.strip() else []

    ocr_text = facts.get("ocr_text") or facts.get("coi_text") or facts.get("coi") or []
    if isinstance(ocr_text, str):
        ocr_text = [ocr_text] if ocr_text.strip() else []
    input_reconstruction = facts.get("input_reconstruction") or {}
    classification_input_product_facts = (
        facts.get("classification_input_product_facts")
        or input_reconstruction.get("classification_input_product_facts")
        or []
    )
    unresolved_product_facts = (
        facts.get("unresolved_product_facts")
        or input_reconstruction.get("unresolved_product_facts")
        or input_reconstruction.get("unresolved_facts")
        or []
    )
    product_fact_conflicts = (
        facts.get("product_fact_conflicts")
        or input_reconstruction.get("product_fact_conflicts")
        or input_reconstruction.get("conflicts")
        or []
    )
    classification_input_text_lines = (
        facts.get("classification_input_fact_texts")
        or input_reconstruction.get("classification_input_fact_texts")
        or facts.get("ingredient_list")
        or []
    )

    return {
        "product_name": facts.get("product_name") or query,
        "description": facts.get("description") or facts.get("short_description") or "",
        "composition": facts.get("composition") or classification_input_text_lines or [],
        "classification_input_product_facts": classification_input_product_facts,
        "unresolved_product_facts": unresolved_product_facts,
        "product_fact_conflicts": product_fact_conflicts,
        "classification_input_fact_texts": classification_input_text_lines,
        "ocr_text": ocr_text,
        "source_urls": source_urls,
        "origin_country": facts.get("origin_country") or "KR",
        "intended_use": facts.get("intended_use") or "unknown",
        "warnings": facts.get("warnings") or [],
        "url_intake": facts.get("url_intake") or {},
        "input_reconstruction": input_reconstruction,
    }


def _normalize_product_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Normalize explicit Product facts payloads."""
    if not isinstance(facts, dict):
        return {}
    return _normalize_kurly_result_facts(facts)


def _normalize_kurly_result_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Flatten the Kurly collector/OCR result shape into Product facts.

    Accepts the object shape emitted by ABS-Capstone-ASAPExpress, e.g.
    parsed_product_page/product/ocr_summary/combined_ocr_text.
    """
    if not isinstance(facts, dict):
        return {}
    raw_collection = facts.get("raw_collection") or {}
    parsed = (
        facts.get("parsed_product_page")
        or facts.get("source_product_page")
        or raw_collection.get("parsed_product_page")
        or {}
    )
    product = facts.get("product") or {}
    ocr_summary = facts.get("ocr_summary") or facts.get("ocr") or {}
    ocr_evidence = raw_collection.get("ocr_evidence") or {}
    llm_reconstruction = (
        facts.get("llm_reconstruction")
        or facts.get("input_reconstruction")
        or {}
    )
    normalization = ocr_summary.get("normalization") or {}

    fact_texts = (
        llm_reconstruction.get("classification_input_fact_texts")
        or llm_reconstruction.get("fact_texts_for_classification")
        or llm_reconstruction.get("normalized_fact_texts")
        or normalization.get("fact_texts")
        or []
    )
    classification_input_product_facts = (
        facts.get("classification_input_product_facts")
        or llm_reconstruction.get("classification_input_product_facts")
        or []
    )
    unresolved_product_facts = (
        facts.get("unresolved_product_facts")
        or llm_reconstruction.get("unresolved_product_facts")
        or llm_reconstruction.get("unresolved_facts")
        or []
    )
    product_fact_conflicts = (
        facts.get("product_fact_conflicts")
        or llm_reconstruction.get("product_fact_conflicts")
        or llm_reconstruction.get("conflicts")
        or []
    )
    combined_ocr_text = (
        facts.get("combined_ocr_text")
        or ocr_evidence.get("combined_ocr_text")
        or ""
    )
    ocr_text: list[str] = []
    if isinstance(combined_ocr_text, str) and combined_ocr_text.strip():
        ocr_text.append(combined_ocr_text)
    if isinstance(fact_texts, list):
        ocr_text.extend(str(t) for t in fact_texts if str(t).strip())

    product_page_url = (
        facts.get("product_page_url")
        or parsed.get("product_page_url")
        or facts.get("url")
    )
    product_name = (
        product.get("product_name")
        or parsed.get("product_name")
        or facts.get("product_name")
    )
    short_description = (
        product.get("short_description")
        or parsed.get("short_description")
        or facts.get("short_description")
        or facts.get("description")
    )
    product_domain = (
        product.get("product_domain")
        or parsed.get("product_domain")
        or facts.get("product_domain")
    )

    flattened = dict(facts)
    flattened.update({
        "url": product_page_url or facts.get("url") or "",
        "product_name": product_name or "",
        "description": short_description or "",
        "short_description": short_description or "",
        "product_domain": product_domain or facts.get("product_domain") or "unknown",
        "product_category": facts.get("product_category") or product_domain or "unknown",
        "brand_name": product.get("brand_name") or parsed.get("brand_name") or facts.get("brand_name") or "",
        "package_type": product.get("package_type") or parsed.get("package_type") or facts.get("package_type") or "",
        "sale_unit": product.get("sale_unit") or parsed.get("sale_unit") or facts.get("sale_unit") or "",
        "ocr_text": ocr_text or facts.get("ocr_text") or [],
        "ingredient_list": fact_texts or facts.get("ingredient_list") or [],
        "classification_input_product_facts": classification_input_product_facts,
        "unresolved_product_facts": unresolved_product_facts,
        "product_fact_conflicts": product_fact_conflicts,
        "classification_input_fact_texts": fact_texts,
    })
    return flattened


def run_document_pipeline(
    *,
    query: str,
    facts: dict[str, Any],
    include_celex_excerpt: bool = False,
    progress_callback=None,
) -> dict[str, Any]:
    """Run Evidence -> Classification -> Document -> Orchestrator.

    Return shape:
      {
        "store": BlackboardStore,
        "blackboard": dict,
        "raw_document_package": dict | None,
        "document_package": dict | None,
        "candidate_code_set": dict | None,
        "decision": dict | None,
        "agent_results": list[dict],
      }
    """
    store = BlackboardStore.create(runtime_mode="dash")

    def emit(stage: str, status: str, **payload) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback({
                "stage": stage,
                "status": status,
                "run_id": store.run_id,
                **payload,
            })
        except Exception:
            pass

    emit("Input_Intake", "running", message="사용자 입력/URL/OCR evidence intake 준비")
    raw_input = build_raw_input_from_ui(query=query, facts=facts)
    emit("Input_Intake", "completed", message="raw product facts 생성", raw_input=raw_input)

    agents = [
        EvidenceIntakeAgent(raw_input),
        ClassificationAgent(),
        DocumentAgent(include_celex_excerpt=include_celex_excerpt),
        OrchestratorAgent(),
    ]

    agent_results: list[dict[str, Any]] = []
    for agent in agents:
        emit(agent.agent_name, "running", message=f"{agent.stage} 실행 중")
        result = agent.execute(store)
        agent_results.append({
            "agent_name": agent.agent_name,
            "success": result.success,
            "error": result.error,
            "outputs_written": result.outputs_written,
        })
        bb_snapshot = store.load()
        partial = {
            "blackboard": bb_snapshot,
            "candidate_code_set": (bb_snapshot.get("candidate_code_sets") or [None])[-1],
            "document_package": (bb_snapshot.get("document_packages") or [None])[-1],
            "decision": (bb_snapshot.get("orchestrator_decisions") or [None])[-1],
            "agent_results": list(agent_results),
            "agent_runs": _read_agent_runs(store),
            "run_id": store.run_id,
            "run_dir": str(Path(store.run_dir)),
        }
        emit(
            agent.agent_name,
            "completed" if result.success else "failed",
            message=f"{agent.agent_name} 완료" if result.success else f"{agent.agent_name} 실패",
            agent_result=agent_results[-1],
            partial_result=partial,
        )
        if not result.success:
            break

    bb = store.load()
    document_package = (bb.get("document_packages") or [None])[-1]
    candidate_code_set = (bb.get("candidate_code_sets") or [None])[-1]
    decision = (bb.get("orchestrator_decisions") or [None])[-1]
    raw_document_package = (
        document_package.get("raw_document_package")
        if isinstance(document_package, dict)
        else None
    )

    return {
        "store": store,
        "blackboard": bb,
        "raw_document_package": raw_document_package,
        "document_package": document_package,
        "candidate_code_set": candidate_code_set,
        "decision": decision,
        "agent_results": agent_results,
        "agent_runs": _read_agent_runs(store),
        "run_id": store.run_id,
        "run_dir": str(Path(store.run_dir)),
    }
