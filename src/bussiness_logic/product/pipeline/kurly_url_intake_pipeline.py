"""KurlyMarket URL intake pipeline."""

from collections.abc import Callable
from typing import Dict, List, Optional

from bussiness_logic.product.web_parser.kurly_market_collector import KurlyPageCollector
from bussiness_logic.product.web_parser.kurly_market_schema import KurlyCollectionResult
from bussiness_logic.product.ocr.ocr_fallback import (
    ProductOcrFallbackRunner,
    ProductOcrImageResult,
)
from bussiness_logic.product.ocr.download_execution import (
    DownloadExecutionCoordinator,
)
from bussiness_logic.product.ocr.ocr_normalization import (
    ProductOcrFactNormalizationResult,
    ProductOcrFactNormalizer,
)
from bussiness_logic.product.ocr.paddle_ocr import ProductOcrEngine
from bussiness_logic.input_process.reconstruction import (
    InputReconstructionResult,
    ProductInputReconstructionService,
)
from bussiness_logic.product.pipeline.kurly_url_intake_schema import (
    KurlyUrlIntakeInput,
    KurlyUrlIntakeResult,
    KurlyUrlIntakeStep,
)


class KurlyUrlIntakePipeline:
    """KurlyMarket URL parsing, OCR fallback, input reconstruction을 연결한다."""

    def __init__(
        self,
        collector: KurlyPageCollector,
        ocrEngine: Optional[ProductOcrEngine] = None,
        screeningOcrEngine: Optional[ProductOcrEngine] = None,
        ocrFactNormalizer: Optional[ProductOcrFactNormalizer] = None,
        inputReconstructionService: Optional[ProductInputReconstructionService] = None,
        imageStatusCallback: Optional[
            Callable[[List[Dict[str, object]]], None]
        ] = None,
        downloadExecutionCoordinator: Optional[
            DownloadExecutionCoordinator
        ] = None,
    ) -> None:
        self._collector = collector
        self._ocrEngine = ocrEngine
        self._screeningOcrEngine = screeningOcrEngine
        self._ocrFactNormalizer = ocrFactNormalizer or ProductOcrFactNormalizer()
        self._inputReconstructionService = inputReconstructionService
        self._imageStatusCallback = imageStatusCallback
        self._downloadExecutionCoordinator = downloadExecutionCoordinator

    def Run(
        self,
        pipelineInput: KurlyUrlIntakeInput,
    ) -> KurlyUrlIntakeResult:
        return self.Collect(pipelineInput)

    def Collect(
        self,
        pipelineInput: KurlyUrlIntakeInput,
    ) -> KurlyUrlIntakeResult:
        steps: List[KurlyUrlIntakeStep] = []
        errors: List[str] = []

        self._collector.ValidateProductPageUrl(pipelineInput.productPageUrl)
        steps.append(
            KurlyUrlIntakeStep(
                stepName="validate_product_page_url",
                message="supported KurlyMarket product page URL",
            )
        )

        renderedPageEvidence = self._collector.CollectRenderedPageEvidence(
            pipelineInput.productPageUrl,
        )
        steps.append(
            KurlyUrlIntakeStep(
                stepName="collect_rendered_page_evidence",
                message=(
                    "visible_text_length={0}, product_notice_text_length={1}, "
                    "product_detail_image_url_count={2}"
                ).format(
                    len(renderedPageEvidence.visibleText),
                    len(renderedPageEvidence.productNoticeText),
                    len(renderedPageEvidence.productDetailImageUrls),
                ),
            )
        )

        collectionResult = self._collector.BuildCollectionResult(renderedPageEvidence)
        steps.append(
            KurlyUrlIntakeStep(
                stepName="parse_product_page_evidence",
                message=(
                    "product_notice_field_count={0}, "
                    "requires_ocr_fallback={1}, "
                    "ocr_candidate_image_url_count={2}"
                ).format(
                    len(collectionResult.parsedProductPage.productNoticeFields),
                    collectionResult.parsedProductPage.requiresOcrFallback,
                    len(collectionResult.ocrCandidateImageUrls),
                ),
            )
        )

        selectedImageUrls = set(
            collectionResult.ocrCandidateImageUrls[
                : max(0, pipelineInput.maxOcrImageCount)
            ]
            if pipelineInput.runOcrFallback
            and collectionResult.parsedProductPage.requiresOcrFallback
            else []
        )
        imageUrls = list(dict.fromkeys([
            *collectionResult.productDetailImageUrls,
            *collectionResult.ocrCandidateImageUrls,
        ]))
        imageEvidenceItems = [
            {
                "image_id": f"collected-image-{imageIndex}",
                "preview_url": imageUrl,
                "source_page_url": collectionResult.productPageUrl,
                "status": "queued" if imageUrl in selectedImageUrls else "discovered",
                "rejection_reason": "",
                "failure_reason": "",
            }
            for imageIndex, imageUrl in enumerate(imageUrls, start=1)
        ]
        self._PublishImageEvidenceItems(imageEvidenceItems)

        def ReportImageStatus(
            _imageIndex: int,
            imageUrl: str,
            status: str,
            rejectionReason: str,
            failureReason: str,
        ) -> None:
            item = next(
                (
                    imageItem
                    for imageItem in imageEvidenceItems
                    if imageItem["preview_url"] == imageUrl
                ),
                None,
            )
            if item is None:
                return
            item.update({
                "status": status,
                "rejection_reason": rejectionReason,
                "failure_reason": failureReason,
            })
            self._PublishImageEvidenceItems(imageEvidenceItems)

        ocrImageResults: List[ProductOcrImageResult] = []
        if pipelineInput.runOcrFallback:
            ocrImageResults = self._RunOcrFallback(
                collectionResult=collectionResult,
                pipelineInput=pipelineInput,
                steps=steps,
                errors=errors,
                imageStatusCallback=ReportImageStatus,
            )
        else:
            steps.append(
                KurlyUrlIntakeStep(
                    stepName="ocr_fallback",
                    message="skipped because runOcrFallback=False",
                )
            )

        combinedOcrText = ProductOcrFallbackRunner.BuildCombinedOcrText(
            ocrImageResults
        )
        if self._inputReconstructionService is None:
            ocrNormalizationResult = self._ocrFactNormalizer.Normalize(
                combinedOcrText,
                productDomain=collectionResult.parsedProductPage.productDomain.value,
            )
        else:
            ocrNormalizationResult = ProductOcrFactNormalizationResult(
                rawLineCount=len(
                    [line for line in combinedOcrText.splitlines() if line.strip()]
                ),
            )
        inputReconstructionResult = (
            self._inputReconstructionService.ReconstructFromPipelineParts(
                collectionResult=collectionResult,
                ocrImageResults=ocrImageResults,
                combinedOcrText=combinedOcrText,
            )
            if self._inputReconstructionService is not None
            else None
        )
        if inputReconstructionResult is not None:
            ocrNormalizationResult = ocrNormalizationResult.model_copy(
                update={
                    "factTexts": list(inputReconstructionResult.normalizedFactTexts),
                    "factLineCount": len(inputReconstructionResult.normalizedFactTexts),
                }
            )
            steps.append(
                KurlyUrlIntakeStep(
                    stepName="reconstruct_product_input",
                    message=(
                        "fact_count={0}, unresolved_count={1}, conflict_count={2}, "
                        "dictionary_match_count={3}, used_llm={4}, fallback_reason={5}"
                    ).format(
                        len(inputReconstructionResult.productFacts),
                        len(inputReconstructionResult.unresolvedFacts),
                        len(inputReconstructionResult.conflicts),
                        len(inputReconstructionResult.dictionaryMatches),
                        inputReconstructionResult.usedLlmReconstruction,
                        inputReconstructionResult.fallbackReason,
                    ),
                )
            )
        else:
            steps.append(
                KurlyUrlIntakeStep(
                    stepName="reconstruct_product_input",
                    message="skipped because input reconstruction is not configured",
                )
            )
        steps.append(
            KurlyUrlIntakeStep(
                stepName="build_classification_fact_texts",
                message="raw_line_count={0}, classification_fact_text_count={1}".format(
                    ocrNormalizationResult.rawLineCount,
                    ocrNormalizationResult.factLineCount,
                ),
            )
        )

        return KurlyUrlIntakeResult(
            collectionResult=collectionResult,
            renderedPageEvidence=renderedPageEvidence,
            ocrImageResults=ocrImageResults,
            combinedOcrText=combinedOcrText,
            ocrNormalizationResult=ocrNormalizationResult,
            inputReconstructionResult=inputReconstructionResult
            if inputReconstructionResult is not None
            else InputReconstructionResult(),
            steps=steps,
            errors=errors,
        )

    def _RunOcrFallback(
        self,
        collectionResult: KurlyCollectionResult,
        pipelineInput: KurlyUrlIntakeInput,
        steps: List[KurlyUrlIntakeStep],
        errors: List[str],
        imageStatusCallback: Callable[[int, str, str, str, str], None],
    ) -> List[ProductOcrImageResult]:
        if not collectionResult.parsedProductPage.requiresOcrFallback:
            steps.append(
                KurlyUrlIntakeStep(
                    stepName="ocr_fallback",
                    message="skipped because structured notice is sufficient",
                )
            )
            return []

        if self._ocrEngine is None:
            message = "OCR fallback requested but ProductOcrEngine is not configured"
            errors.append(message)
            steps.append(
                KurlyUrlIntakeStep(
                    stepName="ocr_fallback",
                    succeeded=False,
                    message=message,
                )
            )
            return []

        ocrFallbackRunner = ProductOcrFallbackRunner(
            self._ocrEngine,
            screeningEngine=self._screeningOcrEngine,
            downloadExecutionCoordinator=self._downloadExecutionCoordinator,
        )
        imageResults = ocrFallbackRunner.Run(
            imageUrls=collectionResult.ocrCandidateImageUrls,
            artifactRootPath=pipelineInput.artifactRootPath,
            productPageUrl=collectionResult.productPageUrl,
            maxImageCount=pipelineInput.maxOcrImageCount,
            downloadTimeoutSeconds=pipelineInput.downloadTimeoutSeconds,
            reuseArtifactImages=pipelineInput.reuseOcrImageArtifacts,
            imageStatusCallback=imageStatusCallback,
        )
        errors.extend(
            imageResult.error
            for imageResult in imageResults
            if imageResult.error is not None
        )

        candidateImageCount = len(collectionResult.ocrCandidateImageUrls)
        selectedImageCount = min(candidateImageCount, pipelineInput.maxOcrImageCount)
        structuredOcrImageCount = sum(
            "structured_ocr" in imageResult.processingTimes
            for imageResult in imageResults
        )
        successfulOcrImageCount = sum(
            imageResult.error is None
            and imageResult.skippedReason is None
            and imageResult.ocrText.strip() != ""
            for imageResult in imageResults
        )
        skippedOcrImageCount = sum(
            imageResult.skippedReason is not None
            for imageResult in imageResults
        )
        screenedRawImageCount = sum(
            imageResult.structuredOcr.textMergeMode == "screened_raw_only"
            for imageResult in imageResults
        )

        steps.append(
            KurlyUrlIntakeStep(
                stepName="ocr_fallback",
                succeeded=not errors,
                message=(
                    "candidate_image_count={0}, selected_image_count={1}, "
                    "ocr_image_count={2}, successful_ocr_count={3}, "
                    "skipped_ocr_count={4}, structured_ocr_image_count={5}, "
                    "screened_raw_image_count={6}, reused_image_count={7}, "
                    "error_count={8}"
                ).format(
                    candidateImageCount,
                    selectedImageCount,
                    len(imageResults),
                    successfulOcrImageCount,
                    skippedOcrImageCount,
                    structuredOcrImageCount,
                    screenedRawImageCount,
                    sum(
                        "cached_image_read" in imageResult.processingTimes
                        for imageResult in imageResults
                    ),
                    len(errors),
                ),
            )
        )
        return imageResults

    def _PublishImageEvidenceItems(
        self,
        imageEvidenceItems: List[Dict[str, object]],
    ) -> None:
        if self._imageStatusCallback is None or not imageEvidenceItems:
            return
        try:
            self._imageStatusCallback([
                dict(imageItem)
                for imageItem in imageEvidenceItems
            ])
        except Exception:
            # 진행 알림 실패가 수집 결과를 실패로 바꾸면 안 된다.
            pass
