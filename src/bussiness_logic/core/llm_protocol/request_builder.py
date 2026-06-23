"""Ontology context를 bridge LLM 요청으로 변환하는 builder."""

from pathlib import Path
from typing import Optional

from bussiness_logic.bridge import LlmGenerationOptions, LlmRequest, LlmResponseFormat
from bussiness_logic.core.context_retrieval.context_builder import OntologyContextBuilder
from bussiness_logic.core.context_retrieval.schema import PackagedOntologyContext


DEFAULT_ONTOLOGY_SYSTEM_PROMPT = """\
You are an EU import-requirement decision-support assistant for Korean exporters.
Use the supplied core context as source guidance.
Separate HS, CN, TARIC, regulatory requirements, documents, origin, and FTA reasoning.
Do not present candidate classification or regulatory requirements as final legal determinations.
When evidence is insufficient, explicitly say that human review or official confirmation is needed.
"""


class LlmRequestBuilder:
    """Ontology RAG 결과를 provider 독립 LlmRequest로 만든다."""

    def __init__(
        self,
        defaultSystemPrompt: str = DEFAULT_ONTOLOGY_SYSTEM_PROMPT,
    ) -> None:
        self.defaultSystemPrompt = defaultSystemPrompt.strip()

    def BuildRequest(
        self,
        userPrompt: str,
        packagedContext: PackagedOntologyContext,
        systemPrompt: Optional[str] = None,
        responseFormat: LlmResponseFormat = LlmResponseFormat.TEXT,
        generationOptions: Optional[LlmGenerationOptions] = None,
    ) -> LlmRequest:
        return LlmRequest(
            userPrompt=userPrompt,
            systemPrompt=(systemPrompt or self.defaultSystemPrompt),
            contextChunks=list(packagedContext.contextChunks),
            responseFormat=responseFormat,
            generationOptions=generationOptions or LlmGenerationOptions(),
        )


class OntologyRequestBuilder:
    """core root와 사용자 질문만으로 LlmRequest를 만드는 공개 facade."""

    def __init__(
        self,
        ontologyRootPath: str | Path,
        contextBuilder: Optional[OntologyContextBuilder] = None,
        llmRequestBuilder: Optional[LlmRequestBuilder] = None,
    ) -> None:
        self.contextBuilder = contextBuilder or OntologyContextBuilder(
            ontologyRootPath,
        )
        self.llmRequestBuilder = llmRequestBuilder or LlmRequestBuilder()

    def BuildRequest(
        self,
        query: str,
        userPrompt: str,
        phaseId: Optional[str] = None,
        topK: int = 8,
        maxResultCount: int = 8,
        includeReferenceDocuments: bool = False,
        includeInactiveDocuments: bool = False,
        systemPrompt: Optional[str] = None,
        responseFormat: LlmResponseFormat = LlmResponseFormat.TEXT,
        generationOptions: Optional[LlmGenerationOptions] = None,
    ) -> LlmRequest:
        packagedContext = self.contextBuilder.BuildContext(
            query=query,
            phaseId=phaseId,
            topK=topK,
            maxResultCount=maxResultCount,
            includeReferenceDocuments=includeReferenceDocuments,
            includeInactiveDocuments=includeInactiveDocuments,
        )
        return self.llmRequestBuilder.BuildRequest(
            userPrompt=userPrompt,
            packagedContext=packagedContext,
            systemPrompt=systemPrompt,
            responseFormat=responseFormat,
            generationOptions=generationOptions,
        )
