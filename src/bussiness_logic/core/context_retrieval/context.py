"""검색 결과를 LLM context window에 맞게 포장하는 계층."""

from typing import List, Sequence

from bussiness_logic.core.context_retrieval.schema import (
    OntologyRetrievalResult,
    PackagedOntologyContext,
)


DEFAULT_MAX_CONTEXT_TOKENS = 6000
DEFAULT_RESERVED_OUTPUT_TOKENS = 1200


class ContextPackager:
    """검색 결과를 token 예산 안에서 bridge용 context chunk로 변환한다."""

    def __init__(
        self,
        maxContextTokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        reservedOutputTokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
    ) -> None:
        self.maxContextTokens = max(1, maxContextTokens)
        self.reservedOutputTokens = max(0, reservedOutputTokens)

    def Package(
        self,
        retrievalResults: Sequence[OntologyRetrievalResult],
        maxResultCount: int = 8,
    ) -> PackagedOntologyContext:
        candidateResults = retrievalResults[: max(0, maxResultCount)]
        selectedResults: List[OntologyRetrievalResult] = []
        contextChunks: List[str] = []
        totalTokenEstimate = 0
        tokenBudget = max(1, self.maxContextTokens - self.reservedOutputTokens)
        stoppedByBudget = False

        for retrievalResult in candidateResults:
            chunkTokenEstimate = retrievalResult.chunk.tokenEstimate
            if totalTokenEstimate + chunkTokenEstimate > tokenBudget:
                stoppedByBudget = True
                break

            selectedResults.append(retrievalResult)
            contextChunks.append(retrievalResult.chunk.ToContextText())
            totalTokenEstimate += chunkTokenEstimate

        omittedResultCount = max(0, len(retrievalResults) - len(selectedResults))
        warnings: List[str] = []
        if len(retrievalResults) > len(candidateResults):
            warnings.append(
                "Some core retrieval results were omitted because of maxResultCount.",
            )
        if stoppedByBudget:
            warnings.append(
                "Some core retrieval results were omitted because of the context budget.",
            )
        if not selectedResults:
            warnings.append("No core context was selected for the LLM request.")

        return PackagedOntologyContext(
            contextChunks=contextChunks,
            selectedResults=selectedResults,
            totalTokenEstimate=totalTokenEstimate,
            omittedResultCount=omittedResultCount,
            warnings=warnings,
        )
