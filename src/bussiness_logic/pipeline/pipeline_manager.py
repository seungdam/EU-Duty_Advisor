"""Top-level export pipeline manager."""

from __future__ import annotations

from collections.abc import Callable
import uuid
from pathlib import Path

from bussiness_logic.pipeline.blackboard import BlackboardStore
from bussiness_logic.app_config import LlmProfileName
from bussiness_logic.bridge.runtime_adapter import (
    BuildOptionalPipelineRuntimeAdapter,
)
from bussiness_logic.artifact_paths import BuildSafeArtifactPathSegment
from bussiness_logic.document.pipeline.document_recommendation_pipeline import (
    DocumentRecommendationPipeline,
)
from bussiness_logic.pipeline.run_paths import (
    BuildInternalRunId,
    PIPELINE_OUTPUTS_ROOT,
    ResolveProductArtifactId,
)
from bussiness_logic.classification.pipeline.hs_code_classification_pipeline import (
    HsCodeClassificationPipeline,
)
from bussiness_logic.product.pipeline.kurly_product_collection_pipeline import (
    KurlyProductCollectionPipeline,
)
from bussiness_logic.pipeline.pipeline_context import PipelineContext, ProgressCallback
from bussiness_logic.pipeline.pipeline_step import PipelineStep
from bussiness_logic.input_process.pipeline.user_input_preparation_pipeline import (
    UserInputPreparationPipeline,
)
from bussiness_logic.utils.json_types import JsonObject


class ExportPipelineManager:
    """Create the BlackboardStore and run the four export pipelines."""

    def __init__(self, *, pipelineOutputsRoot: Path = PIPELINE_OUTPUTS_ROOT) -> None:
        self._pipelineOutputsRoot = pipelineOutputsRoot

    def Run(
        self,
        *,
        query: str,
        facts: JsonObject,
        include_celex_excerpt: bool = False,
        progress_callback: ProgressCallback | None = None,
        job_id: str | None = None,
    ) -> dict[str, object]:
        effectiveJobId = job_id or f"job_{uuid.uuid4().hex[:10]}"
        context = PipelineContext(
            query=query,
            facts=dict(facts),
            store=self._CreateStore(
                query=query,
                facts=facts,
                effectiveJobId=effectiveJobId,
            ),
            includeCelexExcerpt=include_celex_excerpt,
            progressCallback=progress_callback,
        )
        for step in self._BuildSteps():
            step.Run(context)
            if context.shouldStop:
                break
            partialResult = context.BuildPartialResult()
            candidateCodeSet = partialResult.get("candidate_code_set")
            if (
                isinstance(candidateCodeSet, dict)
                and candidateCodeSet.get("classification_status") == "needs_more_facts"
            ):
                break
        return context.BuildFinalResult()

    def _CreateStore(
        self,
        *,
        query: str,
        facts: JsonObject,
        effectiveJobId: str,
    ) -> BlackboardStore:
        safeJobId = BuildSafeArtifactPathSegment(effectiveJobId, fallback="")
        if safeJobId != effectiveJobId:
            raise ValueError("job_id must be a safe artifact path segment.")

        productArtifactId = ResolveProductArtifactId(query, facts)
        runDirectory = self._pipelineOutputsRoot / productArtifactId / effectiveJobId
        userInputFacts = facts.get("user_input_facts") or {}
        if not isinstance(userInputFacts, dict):
            userInputFacts = {}
        originCountry = (
            str(
                userInputFacts.get("origin_country")
                or facts.get("origin_country")
                or "unknown"
            ).strip()
            or "unknown"
        )
        return BlackboardStore.create(
            origin_country=originCountry,
            runtime_mode="webapp",
            run_id=BuildInternalRunId(effectiveJobId),
            run_dir=runDirectory,
        )

    def _BuildSteps(self) -> list[PipelineStep]:
        pipelines = getattr(self, "_pipelines", None)
        if pipelines is not None:
            return [
                PipelineStep(type(pipeline).__name__, pipeline)
                for pipeline in pipelines
            ]
        return [
            PipelineStep("user_input_preparation", UserInputPreparationPipeline()),
            PipelineStep("kurly_product_collection", KurlyProductCollectionPipeline()),
            PipelineStep(
                "hs_code_classification",
                HsCodeClassificationPipeline(
                    identityHintRuntimeAdapter=(
                        BuildOptionalPipelineRuntimeAdapter(
                            LlmProfileName.IDENTITY_HINT,
                        )
                    ),
                    routingRuntimeAdapter=(
                        BuildOptionalPipelineRuntimeAdapter(
                            LlmProfileName.HS2_ROUTER,
                        )
                    ),
                    validationRuntimeAdapter=(
                        BuildOptionalPipelineRuntimeAdapter(
                            LlmProfileName.CLASSIFICATION_VALIDATOR,
                        )
                    ),
                ),
            ),
            PipelineStep("document_recommendation", DocumentRecommendationPipeline()),
        ]


class ExportRequirementPipeline:
    """Backward-compatible wrapper around ExportPipelineManager."""

    def __init__(self, *, pipelineOutputsRoot: Path = PIPELINE_OUTPUTS_ROOT) -> None:
        self._pipelineOutputsRoot = pipelineOutputsRoot

    def Run(
        self,
        *,
        query: str,
        facts: JsonObject,
        include_celex_excerpt: bool = False,
        progress_callback: Callable[[JsonObject], None] | None = None,
        job_id: str | None = None,
    ) -> dict[str, object]:
        return ExportPipelineManager(
            pipelineOutputsRoot=self._pipelineOutputsRoot,
        ).Run(
            query=query,
            facts=facts,
            include_celex_excerpt=include_celex_excerpt,
            progress_callback=progress_callback,
            job_id=job_id,
        )


def RunExportRequirementPipeline(
    *,
    query: str,
    facts: JsonObject,
    include_celex_excerpt: bool = False,
    progress_callback: Callable[[JsonObject], None] | None = None,
    job_id: str | None = None,
) -> dict[str, object]:
    return ExportRequirementPipeline().Run(
        query=query,
        facts=facts,
        include_celex_excerpt=include_celex_excerpt,
        progress_callback=progress_callback,
        job_id=job_id,
    )
