"""LLM 런타임 계층의 데이터 계약."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class OperatingSystemKind(str, Enum):
    """로컬 LLM 런타임 선택에 필요한 OS 구분."""

    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"
    UNKNOWN = "unknown"


class LlmRuntimeKind(str, Enum):
    """교체 가능한 LLM 런타임 종류."""

    OMLX = "omlx"
    OLLAMA = "ollama"
    OPENAI = "openai"


class LlmResponseFormat(str, Enum):
    """LLM 응답 형식에 대한 provider 독립 요청 힌트."""

    TEXT = "text"
    JSON_OBJECT = "json_object"


class LlmFinishReason(str, Enum):
    """provider별 종료 사유를 공통 범주로 정규화한 값."""

    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALLS = "tool_calls"
    UNKNOWN = "unknown"


class RuntimeDependencyStatus(BaseModel):
    """현재 환경에서 선택 runtime을 사용할 수 있는지 나타내는 probe 결과."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    runtimeKind: LlmRuntimeKind = Field(alias="runtime_kind")
    isAvailable: bool = Field(alias="is_available")
    message: str
    moduleName: Optional[str] = Field(default=None, alias="module_name")
    executablePath: Optional[str] = Field(default=None, alias="executable_path")
    endpointUrl: Optional[str] = Field(default=None, alias="endpoint_url")
    limitations: List[str] = Field(default_factory=list)


class LlmTokenUsage(BaseModel):
    """provider별 token 사용량 필드를 공통 필드로 정규화한 값."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    inputTokens: Optional[int] = Field(default=None, alias="input_tokens")
    outputTokens: Optional[int] = Field(default=None, alias="output_tokens")
    totalTokens: Optional[int] = Field(default=None, alias="total_tokens")


class LlmGenerationOptions(BaseModel):
    """LLM 호출 시 런타임에 전달할 생성 옵션."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    temperature: float = 0.0
    maxTokens: Optional[int] = Field(default=None, alias="max_tokens")
    topP: Optional[float] = Field(default=None, alias="top_p")
    stopSequences: List[str] = Field(
        default_factory=list,
        alias="stop_sequences",
    )


class LlmRuntimeConfig(BaseModel):
    """adapter에 전달할 런타임 설정."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    runtimeKind: LlmRuntimeKind = Field(alias="runtime_kind")
    modelName: Optional[str] = Field(default=None, alias="model_name")
    executablePath: Optional[str] = Field(default=None, alias="executable_path")
    endpointUrl: Optional[str] = Field(default=None, alias="endpoint_url")
    extraOptions: Dict[str, Any] = Field(default_factory=dict, alias="extra_options")


class RuntimeDescriptor(BaseModel):
    """선택 runtime의 실행 방식과 dependency 상태를 설명하는 객체."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    runtimeKind: LlmRuntimeKind = Field(alias="runtime_kind")
    dependencyStatus: RuntimeDependencyStatus = Field(alias="dependency_status")
    moduleName: Optional[str] = Field(default=None, alias="module_name")
    executablePath: Optional[str] = Field(default=None, alias="executable_path")
    endpointUrl: Optional[str] = Field(default=None, alias="endpoint_url")
    extraOptions: Dict[str, Any] = Field(default_factory=dict, alias="extra_options")


class LlmRequest(BaseModel):
    """RAG 또는 보고서 생성 단계가 LLM adapter에 전달하는 요청."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    userPrompt: str = Field(alias="user_prompt")
    systemPrompt: Optional[str] = Field(default=None, alias="system_prompt")
    contextChunks: List[str] = Field(default_factory=list, alias="context_chunks")
    responseFormat: LlmResponseFormat = Field(
        default=LlmResponseFormat.TEXT,
        alias="response_format",
    )
    generationOptions: LlmGenerationOptions = Field(
        default_factory=LlmGenerationOptions,
        alias="generation_options",
    )


class LlmResponse(BaseModel):
    """LLM adapter가 반환하는 provider 독립 응답과 추적 정보."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    generatedText: str = Field(alias="generated_text")
    runtimeKind: LlmRuntimeKind = Field(alias="runtime_kind")
    modelName: Optional[str] = Field(default=None, alias="model_name")
    responseFormat: LlmResponseFormat = Field(
        default=LlmResponseFormat.TEXT,
        alias="response_format",
    )
    finishReason: LlmFinishReason = Field(
        default=LlmFinishReason.UNKNOWN,
        alias="finish_reason",
    )
    providerFinishReason: Optional[str] = Field(
        default=None,
        alias="provider_finish_reason",
    )
    tokenUsage: LlmTokenUsage = Field(
        default_factory=LlmTokenUsage,
        alias="token_usage",
    )
    responseId: Optional[str] = Field(default=None, alias="response_id")
    rawResponse: Dict[str, Any] = Field(default_factory=dict, alias="raw_response")
    limitations: List[str] = Field(default_factory=list)
