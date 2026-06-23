"""Product source pipeline schema."""

from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

from bussiness_logic.product.web_parser.kurly_market_schema import (
    KurlyCollectionResult,
    RenderedPageEvidence,
)
from bussiness_logic.product.ocr.ocr_fallback import (
    DEFAULT_PRODUCT_OCR_IMAGE_ARTIFACT_ROOT_PATH,
    DEFAULT_PRODUCT_OCR_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
    ProductOcrImageResult,
)
from bussiness_logic.product.ocr.ocr_normalization import ProductOcrFactNormalizationResult
from bussiness_logic.input_process.reconstruction import ProductFactReconstructionResult


class KurlyPipelineInput(BaseModel):
    """KurlyMarket 수집 wrapper 입력."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    productPageUrl: str
    runOcrFallback: bool = False
    artifactRootPath: Path = DEFAULT_PRODUCT_OCR_IMAGE_ARTIFACT_ROOT_PATH
    maxOcrImageCount: int = Field(
        default=20,
        description="OCR fallback image batch size. All candidate images are processed in batches.",
    )
    downloadTimeoutSeconds: int = DEFAULT_PRODUCT_OCR_IMAGE_DOWNLOAD_TIMEOUT_SECONDS


class PipelineStep(BaseModel):
    """wrapper가 실행한 단계 하나의 상태."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    stepName: str = Field(alias="step_name")
    succeeded: bool = True
    message: str = ""


class KurlyPipelineResult(BaseModel):
    """KurlyMarket parsing과 선택적 OCR fallback을 묶은 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    collectionResult: KurlyCollectionResult = Field(exclude=True)
    renderedPageEvidence: Optional[RenderedPageEvidence] = Field(
        default=None,
        exclude=True,
    )
    ocrImageResults: List[ProductOcrImageResult] = Field(
        default_factory=list,
        exclude=True,
    )
    combinedOcrText: str = Field(default="", alias="combined_ocr_text")
    ocrNormalizationResult: ProductOcrFactNormalizationResult = Field(
        default_factory=ProductOcrFactNormalizationResult,
        alias="ocr_normalization",
    )
    inputReconstructionResult: ProductFactReconstructionResult = Field(
        default_factory=ProductFactReconstructionResult,
        alias="input_reconstruction",
    )
    steps: List[PipelineStep] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    def BuildPublicResult(self) -> Dict[str, object]:
        """UI/API에 전달할 입력 처리 결과만 노출한다."""

        reconstructionData = self.inputReconstructionResult.model_dump(
            mode="json",
            by_alias=True,
            include={
                "productFacts",
                "reconstructedTables",
                "unresolvedFacts",
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
        reconstructedTables = list(
            reconstructionData.get("reconstructed_tables", [])
        )
        unresolvedFacts = list(reconstructionData.get("unresolved_facts", []))
        classificationFactTexts = list(
            reconstructionData.get("normalized_fact_texts", [])
        )
        usedLlmReconstruction = bool(
            reconstructionData.get("used_llm_reconstruction")
        )
        fallbackReason = reconstructionData.get("fallback_reason")
        collectionData = self.collectionResult.model_dump(
            mode="json",
            by_alias=True,
            include={
                "visibleTextLineCount",
                "productNoticeTextLineCount",
                "productDetailImageUrlCount",
                "ocrCandidateImageUrlCount",
                "warnings",
            },
        )
        collectionData["product_detail_image_count"] = collectionData.pop(
            "product_detail_image_url_count",
            0,
        )
        collectionData["ocr_candidate_image_count"] = collectionData.pop(
            "ocr_candidate_image_url_count",
            0,
        )
        collectionData["warning_count"] = len(self.collectionResult.warnings)
        warnings = list(
            dict.fromkeys(
                [
                    *self.collectionResult.warnings,
                    *reconstructionData.get("warnings", []),
                    *[
                        "pipeline_error: {0}".format(error)
                        for error in self.errors
                    ],
                ]
            )
        )
        return {
            "product_page_url": self.collectionResult.productPageUrl,
            "source_product_page": (
                self.collectionResult.parsedProductPage.model_dump(
                    mode="json",
                    by_alias=True,
                )
            ),
            "collection": collectionData,
            "ocr": self._BuildOcrSummary(includeDebugArtifacts=False),
            "input_reconstruction": {
                "mode": (
                    "llm_reconstruction"
                    if usedLlmReconstruction
                    else "fallback_reconstruction"
                    if productFacts or classificationFactTexts
                    else "unavailable"
                ),
                "used_llm_reconstruction": usedLlmReconstruction,
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
                "conflict_count": len(reconstructionData.get("conflicts", [])),
                "fact_text_count": len(classificationFactTexts),
                "classification_input_product_facts": productFacts,
                "reconstructed_tables": reconstructedTables,
                "llm_reconstructed_product_facts": (
                    productFacts if usedLlmReconstruction else []
                ),
                "fallback_product_facts": (
                    [] if usedLlmReconstruction else productFacts
                ),
                "unresolved_product_facts": unresolvedFacts,
                "product_fact_conflicts": list(
                    reconstructionData.get("conflicts", [])
                ),
                "classification_input_fact_texts": classificationFactTexts,
                "source_ref_labels": dict(
                    reconstructionData.get("source_ref_labels", {})
                ),
                "source_evidence_preview": list(
                    reconstructionData.get("source_evidence_preview", [])
                ),
                "warnings": list(reconstructionData.get("warnings", [])),
                "debug_artifact_count": len(
                    self.inputReconstructionResult.debugArtifacts
                ),
            },
            "pipeline_steps": [
                step.model_dump(mode="json", by_alias=True)
                for step in self.steps
            ],
            "errors": list(self.errors),
            "warnings": warnings,
        }

    def _BuildOcrSummary(self, includeDebugArtifacts: bool) -> Dict[str, object]:
        successfulImageResults = [
            imageResult
            for imageResult in self.ocrImageResults
            if imageResult.error is None and imageResult.ocrText.strip() != ""
        ]
        structuredTableImageResults = [
            imageResult
            for imageResult in successfulImageResults
            if imageResult.structuredOcr.usedStructuredTables
        ]
        summary: Dict[str, object] = {
            "image_result_count": len(self.ocrImageResults),
            "successful_image_count": len(successfulImageResults),
            "failed_image_count": (
                len(self.ocrImageResults) - len(successfulImageResults)
            ),
            "artifact_image_count": sum(
                len(imageResult.imagePaths)
                if imageResult.imagePaths
                else 1
                if imageResult.imagePath is not None
                else 0
                for imageResult in self.ocrImageResults
            ),
            "structured_table_image_count": len(structuredTableImageResults),
            "structured_table_count": sum(
                len(imageResult.structuredOcr.tables)
                for imageResult in successfulImageResults
            ),
            "raw_tile_text_count": sum(
                len(imageResult.structuredOcr.rawTileTexts)
                for imageResult in successfulImageResults
            ),
            "raw_text_length": sum(
                len(imageResult.structuredOcr.rawText)
                for imageResult in successfulImageResults
            ),
            "combined_text_length": len(self.combinedOcrText),
            "raw_line_count": self.ocrNormalizationResult.rawLineCount,
        }
        if not includeDebugArtifacts:
            return summary

        summary.update(
            {
                "normalized_fact_count": self.ocrNormalizationResult.factLineCount,
                "input_reconstruction": (
                    self.inputReconstructionResult.model_dump(
                        mode="json",
                        by_alias=True,
                    )
                ),
                "normalization": self.ocrNormalizationResult.model_dump(
                    mode="json",
                    by_alias=True,
                ),
                "image_artifacts": [
                    {
                        "index": imageIndex,
                        "image_path": (
                            str(imageResult.imagePath)
                            if imageResult.imagePath is not None
                            else None
                        ),
                        "image_paths": list(imageResult.imagePaths),
                        "text_length": len(imageResult.ocrText),
                        "used_structured_tables": (
                            imageResult.structuredOcr.usedStructuredTables
                        ),
                        "structured_table_count": len(
                            imageResult.structuredOcr.tables
                        ),
                        "structured_fallback_reason": (
                            imageResult.structuredOcr.fallbackReason
                        ),
                        "structured_warnings": list(
                            imageResult.structuredOcr.warnings
                        ),
                        "text_merge_mode": imageResult.structuredOcr.textMergeMode,
                        "raw_tile_text_count": len(
                            imageResult.structuredOcr.rawTileTexts
                        ),
                        "raw_text_length": len(imageResult.structuredOcr.rawText),
                        "error": imageResult.error,
                    }
                    for imageIndex, imageResult in enumerate(
                        self.ocrImageResults,
                        start=1,
                    )
                    if imageResult.imagePath is not None
                    and imageResult.ocrText.strip() != ""
                ],
            }
        )
        return summary

    @computed_field(alias="product_page_url")
    @property
    def productPageUrl(self) -> str:
        return self.collectionResult.productPageUrl

    @computed_field(alias="parsed_product_page")
    @property
    def parsedProductPage(self) -> Dict[str, object]:
        return self.collectionResult.parsedProductPage.model_dump(
            mode="json",
            by_alias=True,
        )

    @computed_field(alias="collection_summary")
    @property
    def collectionSummary(self) -> Dict[str, object]:
        return self.collectionResult.model_dump(
            mode="json",
            by_alias=True,
            include={
                "productPageUrl",
                "visibleTextLineCount",
                "productNoticeTextLineCount",
                "productDetailImageUrlCount",
                "ocrCandidateImageUrlCount",
                "warnings",
            },
        )

    @computed_field(alias="rendered_page_evidence_summary")
    @property
    def renderedPageEvidenceSummary(self) -> Optional[Dict[str, object]]:
        if self.renderedPageEvidence is None:
            return None
        return self.renderedPageEvidence.model_dump(mode="json", by_alias=True)

    @computed_field(alias="ocr_summary")
    @property
    def ocrSummary(self) -> Dict[str, object]:
        return self._BuildOcrSummary(includeDebugArtifacts=True)
