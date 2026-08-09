"""Run product evidence intake and HS/CN classification components."""

from __future__ import annotations

from bussiness_logic.classification.components.classification import ClassificationComponent
from bussiness_logic.classification.components.hs2_routing import Hs2RoutingComponent
from bussiness_logic.input_process.components.evidence_intake import EvidenceIntakeComponent
from bussiness_logic.product.components.product_understanding import (
    ProductUnderstandingComponent,
)
from bussiness_logic.product.services.identity_hint_agent import IdentityHintAgent
from bussiness_logic.classification.pipeline.raw_input import (
    BuildRawInputFromPreparedFacts,
)
from bussiness_logic.pipeline.pipeline_context import PipelineContext
from bussiness_logic.pipeline.pipeline_step import PipelineStep


class _BuildClassificationRawInputStep:
    def Run(self, context: PipelineContext) -> None:
        context.Emit(
            "Input_Intake",
            "running",
            message="사용자 입력/URL/OCR evidence intake 준비",
        )
        context.rawInput = BuildRawInputFromPreparedFacts(
            query=context.query,
            facts=context.facts,
        )
        context.Emit(
            "Input_Intake",
            "completed",
            message="raw product facts 생성",
            raw_input=context.rawInput,
        )


class HsCodeClassificationPipeline:
    def __init__(
        self,
        *,
        identityHintRuntimeAdapter: object | None = None,
        routingRuntimeAdapter: object | None = None,
        validationRuntimeAdapter: object | None = None,
    ) -> None:
        self._identityHintRuntimeAdapter = identityHintRuntimeAdapter
        self._routingRuntimeAdapter = routingRuntimeAdapter
        self._validationRuntimeAdapter = validationRuntimeAdapter

    def Run(self, context: PipelineContext) -> None:
        PipelineStep(
            "build_raw_input",
            _BuildClassificationRawInputStep(),
        ).Run(context)
        if context.shouldStop:
            return
        for step in self._BuildComponentSteps(context):
            step.Run(context)
            if context.shouldStop:
                return

    def _BuildComponentSteps(
        self,
        context: PipelineContext,
    ) -> tuple[PipelineStep, ...]:
        return (
            PipelineStep(
                "evidence_intake",
                EvidenceIntakeComponent(context.rawInput),
            ),
            PipelineStep(
                "product_understanding",
                ProductUnderstandingComponent(
                    IdentityHintAgent(self._identityHintRuntimeAdapter),
                ),
            ),
            PipelineStep(
                "hs2_routing",
                Hs2RoutingComponent(self._routingRuntimeAdapter),
            ),
            PipelineStep(
                "classification",
                ClassificationComponent(
                    validationRuntimeAdapter=self._validationRuntimeAdapter,
                ),
            ),
        )
