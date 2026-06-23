"""Stage 1 classification traversal controller."""

from typing import List, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from bussiness_logic.core.classification import (
    CnCandidate,
    CnCandidateRetriever,
    DEFAULT_CN_CANDIDATE_TOP_K,
    ProductClassificationInput,
    Stage1ResponseValidationReport,
)
from bussiness_logic.core.decision_flow.decision_policy import (
    Stage1DecisionPolicy,
    ClassificationDecisionHandler,
)


DEFAULT_STAGE1_TRAVERSAL_MAX_RETRY_COUNT = 1


class Stage1TraversalReport(BaseModel):
    """Stage 1 decision 결과를 다음 pipeline action으로 변환한 report."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    traversalStatus: str = Field(alias="traversal_status")
    nextAction: str = Field(alias="next_action")
    decisionStatus: str = Field(alias="decision_status")
    currentCandidateHs8Codes: List[str] = Field(
        default_factory=list,
        alias="current_candidate_hs8_codes",
    )
    retainedCandidateHs8Codes: List[str] = Field(
        default_factory=list,
        alias="retained_candidate_hs8_codes",
    )
    rejectedCandidateHs8Codes: List[str] = Field(
        default_factory=list,
        alias="rejected_candidate_hs8_codes",
    )
    backtrackingRecommended: bool = Field(
        default=False,
        alias="backtracking_recommended",
    )
    backtrackingTargetLevel: Optional[str] = Field(
        default=None,
        alias="backtracking_target_level",
    )
    backtrackingReason: Optional[str] = Field(
        default=None,
        alias="backtracking_reason",
    )


class Stage1TraversalController:
    """Stage 1 후보 검토 결과를 다음 단계 action으로 연결한다."""

    def __init__(
        self,
        decisionPolicy: Optional[Stage1DecisionPolicy] = None,
    ) -> None:
        self._decisionPolicy = decisionPolicy or Stage1DecisionPolicy()

    def BuildReport(
        self,
        validationReport: Stage1ResponseValidationReport,
        candidates: Sequence[CnCandidate],
    ) -> Stage1TraversalReport:
        decisionReport = self._decisionPolicy.BuildDecision(
            validationReport,
            candidates,
        )
        return self.BuildFromDecision(decisionReport, candidates)

    def BuildFromDecision(
        self,
        decisionReport: ClassificationDecisionHandler,
        candidates: Sequence[CnCandidate] = (),
    ) -> Stage1TraversalReport:
        currentCandidateHs8Codes = (
            [candidate.hs8 for candidate in candidates]
            if candidates
            else self._BuildUniqueCandidateCodes(
                [
                    decisionReport.strongCandidateHs8Codes,
                    decisionReport.possibleCandidateHs8Codes,
                    decisionReport.insufficientInformationHs8Codes,
                    decisionReport.unlikelyCandidateHs8Codes,
                ]
            )
        )
        retainedCandidateHs8Codes = self._BuildUniqueCandidateCodes(
            [
                decisionReport.strongCandidateHs8Codes,
                decisionReport.possibleCandidateHs8Codes,
                decisionReport.insufficientInformationHs8Codes,
            ]
        )

        if decisionReport.decisionStatus == "invalid_response_requires_retry":
            traversalStatus = "retry_required"
            nextAction = "retry_llm_response"
        elif decisionReport.backtrackingRecommended:
            traversalStatus = "needs_backtracking"
            nextAction = "backtrack_candidate_scope"
        elif (
            decisionReport.decisionStatus
            == "insufficient_information_before_code_selection"
        ):
            traversalStatus = "needs_more_product_information"
            nextAction = "request_missing_product_information"
        elif decisionReport.decisionStatus == "multiple_strong_candidates_need_review":
            traversalStatus = "needs_candidate_comparison"
            nextAction = "compare_retained_candidates"
        else:
            traversalStatus = "ready_for_human_review"
            nextAction = "prepare_human_review_package"

        return Stage1TraversalReport(
            traversalStatus=traversalStatus,
            nextAction=nextAction,
            decisionStatus=decisionReport.decisionStatus,
            currentCandidateHs8Codes=currentCandidateHs8Codes,
            retainedCandidateHs8Codes=retainedCandidateHs8Codes,
            rejectedCandidateHs8Codes=list(decisionReport.unlikelyCandidateHs8Codes),
            backtrackingRecommended=decisionReport.backtrackingRecommended,
            backtrackingTargetLevel=decisionReport.backtrackingTargetLevel,
            backtrackingReason=decisionReport.backtrackingReason,
        )

    def BuildBacktrackingCandidates(
        self,
        productInput: ProductClassificationInput,
        currentCandidates: Sequence[CnCandidate],
        decisionReport: ClassificationDecisionHandler,
        candidateRetriever: CnCandidateRetriever,
        topK: int = DEFAULT_CN_CANDIDATE_TOP_K,
        visitedHs8Codes: Sequence[str] = (),
        completedRetryCount: int = 0,
        maxRetryCount: int = DEFAULT_STAGE1_TRAVERSAL_MAX_RETRY_COUNT,
    ) -> List[CnCandidate]:
        if completedRetryCount >= maxRetryCount:
            return []

        traversalReport = self.BuildFromDecision(decisionReport, currentCandidates)
        if traversalReport.nextAction != "backtrack_candidate_scope":
            return []

        excludedHs8Codes = set(visitedHs8Codes)
        excludedHs8Codes.update(
            traversalReport.rejectedCandidateHs8Codes
            or traversalReport.currentCandidateHs8Codes
        )
        alternativeCandidates = candidateRetriever.FindAlternativeCandidates(
            productInput=productInput,
            currentCandidates=currentCandidates,
            excludedHs8Codes=sorted(excludedHs8Codes),
            topK=topK,
        )
        if alternativeCandidates:
            return alternativeCandidates
        return candidateRetriever.FindSiblingCandidates(
            productInput=productInput,
            currentCandidates=currentCandidates,
            excludedHs8Codes=sorted(excludedHs8Codes),
            topK=topK,
        )

    def _BuildUniqueCandidateCodes(
        self,
        codeGroups: Sequence[Sequence[str]],
    ) -> List[str]:
        candidateCodes: List[str] = []
        for codeGroup in codeGroups:
            for candidateCode in codeGroup:
                if candidateCode not in candidateCodes:
                    candidateCodes.append(candidateCode)
        return candidateCodes
