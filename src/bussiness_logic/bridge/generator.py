"""LLM runtime별 generate callable 구현."""

import json
import os
from abc import ABC, abstractmethod
from http.client import HTTPException
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bussiness_logic.bridge.probe import (
    DEFAULT_OPENAI_API_KEY_ENV_NAMES,
    DEFAULT_OPENAI_ENDPOINT_URL,
    DEFAULT_OLLAMA_ENDPOINT_URL,
    DEFAULT_OMLX_ENDPOINT_URL,
    PRIMARY_LLM_API_KEY_ENV_NAME,
)
from bussiness_logic.bridge.schema import (
    LlmFinishReason,
    LlmRequest,
    LlmResponse,
    LlmResponseFormat,
    LlmRuntimeConfig,
    LlmRuntimeKind,
    LlmTokenUsage,
    RuntimeDescriptor,
)


DEFAULT_HTTP_TIMEOUT_SECONDS = 120
DEFAULT_OPENAI_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


class RuntimeGenerationError(RuntimeError):
    """LLM runtime 호출이 실패했을 때 사용한다."""


class RuntimeGenerationStrategy(ABC):
    """runtime별 generation 구현을 분리하는 strategy interface."""

    @abstractmethod
    def Generate(
        self,
        runtimeDescriptor: RuntimeDescriptor,
        runtimeConfig: LlmRuntimeConfig,
        request: LlmRequest,
    ) -> LlmResponse:
        """runtime별 LLM 생성을 수행한다."""
        ...


class OpenAiCompatibleGenerationStrategy(RuntimeGenerationStrategy):
    """oMLX처럼 OpenAI-compatible chat endpoint를 제공하는 런타임."""

    def Generate(
        self,
        runtimeDescriptor: RuntimeDescriptor,
        runtimeConfig: LlmRuntimeConfig,
        request: LlmRequest,
    ) -> LlmResponse:
        return _GenerateWithOpenAiCompatibleRuntime(
            runtimeDescriptor,
            runtimeConfig,
            request,
        )


class OllamaGenerationStrategy(RuntimeGenerationStrategy):
    """Ollama generate endpoint를 사용하는 런타임."""

    def Generate(
        self,
        runtimeDescriptor: RuntimeDescriptor,
        runtimeConfig: LlmRuntimeConfig,
        request: LlmRequest,
    ) -> LlmResponse:
        return _GenerateWithOllamaRuntime(
            runtimeDescriptor,
            runtimeConfig,
            request,
        )


class OpenAiGenerationStrategy(RuntimeGenerationStrategy):
    """OpenAI API chat completions endpoint를 사용하는 런타임."""

    def Generate(
        self,
        runtimeDescriptor: RuntimeDescriptor,
        runtimeConfig: LlmRuntimeConfig,
        request: LlmRequest,
    ) -> LlmResponse:
        return _GenerateWithOpenAiRuntime(
            runtimeDescriptor,
            runtimeConfig,
            request,
        )


def GenerateRuntimeResponse(
    runtimeDescriptor: RuntimeDescriptor,
    runtimeConfig: LlmRuntimeConfig,
    request: LlmRequest,
) -> LlmResponse:
    """runtimeKind에 따라 실제 generate 호출을 dispatch한다."""

    generationStrategy = _BuildGenerationStrategy(runtimeDescriptor.runtimeKind)
    return generationStrategy.Generate(runtimeDescriptor, runtimeConfig, request)


def _BuildGenerationStrategy(
    runtimeKind: LlmRuntimeKind,
) -> RuntimeGenerationStrategy:
    if runtimeKind == LlmRuntimeKind.OMLX:
        return OpenAiCompatibleGenerationStrategy()

    if runtimeKind == LlmRuntimeKind.OLLAMA:
        return OllamaGenerationStrategy()

    if runtimeKind == LlmRuntimeKind.OPENAI:
        return OpenAiGenerationStrategy()

    raise RuntimeGenerationError(
        "No generate implementation is configured for: {0}".format(
            runtimeKind.value,
        )
    )


def _GenerateWithOpenAiRuntime(
    runtimeDescriptor: RuntimeDescriptor,
    runtimeConfig: LlmRuntimeConfig,
    request: LlmRequest,
) -> LlmResponse:
    endpointUrl = _BuildEndpointUrl(
        runtimeDescriptor.endpointUrl or DEFAULT_OPENAI_ENDPOINT_URL,
        _ReadStringOption(
            runtimeConfig,
            "chat_completions_path",
            DEFAULT_OPENAI_CHAT_COMPLETIONS_PATH,
        ),
    )
    payload = _BuildOpenAiChatPayload(
        runtimeConfig,
        request,
        includeResponseFormat=_ShouldIncludeOpenAiRuntimeResponseFormat(
            runtimeConfig,
        ),
    )
    responseData = _PostJson(
        endpointUrl,
        payload,
        _ReadTimeoutSeconds(runtimeConfig),
        _ReadOpenAiHeaders(runtimeConfig),
    )

    generatedText = _ExtractOpenAiChatText(responseData)
    responseModelName = responseData.get("model")
    if not isinstance(responseModelName, str) or responseModelName.strip() == "":
        responseModelName = runtimeConfig.modelName

    return LlmResponse(
        generatedText=generatedText,
        runtimeKind=runtimeConfig.runtimeKind,
        modelName=responseModelName,
        responseFormat=request.responseFormat,
        finishReason=_NormalizeFinishReason(
            _ExtractOpenAiProviderFinishReason(responseData),
        ),
        providerFinishReason=_ExtractOpenAiProviderFinishReason(responseData),
        tokenUsage=_ExtractOpenAiTokenUsage(responseData),
        responseId=_ExtractResponseId(responseData),
        rawResponse=responseData,
        limitations=[
            "OpenAI API output is draft reasoning and must not be treated as official determination.",
        ],
    )


def _GenerateWithOpenAiCompatibleRuntime(
    runtimeDescriptor: RuntimeDescriptor,
    runtimeConfig: LlmRuntimeConfig,
    request: LlmRequest,
) -> LlmResponse:
    endpointUrl = _BuildEndpointUrl(
        runtimeDescriptor.endpointUrl or DEFAULT_OMLX_ENDPOINT_URL,
        _ReadStringOption(
            runtimeConfig,
            "chat_completions_path",
            "/v1/chat/completions",
        ),
    )
    payload = _BuildOpenAiChatPayload(
        runtimeConfig,
        request,
        includeResponseFormat=_ShouldIncludeOpenAiCompatibleResponseFormat(
            runtimeConfig,
        ),
    )
    responseData = _PostJson(
        endpointUrl,
        payload,
        _ReadTimeoutSeconds(runtimeConfig),
        _ReadHeaders(runtimeConfig),
    )

    generatedText = _ExtractOpenAiChatText(responseData)
    return LlmResponse(
        generatedText=generatedText,
        runtimeKind=runtimeConfig.runtimeKind,
        modelName=runtimeConfig.modelName,
        responseFormat=request.responseFormat,
        finishReason=_NormalizeFinishReason(
            _ExtractOpenAiProviderFinishReason(responseData),
        ),
        providerFinishReason=_ExtractOpenAiProviderFinishReason(responseData),
        tokenUsage=_ExtractOpenAiTokenUsage(responseData),
        responseId=_ExtractResponseId(responseData),
        rawResponse=responseData,
        limitations=[
            "LLM output is draft reasoning and must not be treated as official determination.",
        ],
    )


def _GenerateWithOllamaRuntime(
    runtimeDescriptor: RuntimeDescriptor,
    runtimeConfig: LlmRuntimeConfig,
    request: LlmRequest,
) -> LlmResponse:
    endpointUrl = _BuildEndpointUrl(
        runtimeDescriptor.endpointUrl or DEFAULT_OLLAMA_ENDPOINT_URL,
        "/api/generate",
    )
    payload = _BuildOllamaPayload(runtimeConfig, request)
    responseData = _PostJson(
        endpointUrl,
        payload,
        _ReadTimeoutSeconds(runtimeConfig),
        _ReadHeaders(runtimeConfig),
    )

    generatedText = str(responseData.get("response", ""))
    return LlmResponse(
        generatedText=generatedText,
        runtimeKind=runtimeConfig.runtimeKind,
        modelName=runtimeConfig.modelName,
        responseFormat=request.responseFormat,
        finishReason=_NormalizeFinishReason(
            _ExtractOllamaProviderFinishReason(responseData),
        ),
        providerFinishReason=_ExtractOllamaProviderFinishReason(responseData),
        tokenUsage=_ExtractOllamaTokenUsage(responseData),
        rawResponse=responseData,
        limitations=[
            "LLM output is draft reasoning and must not be treated as official determination.",
        ],
    )


def _BuildOpenAiChatPayload(
    runtimeConfig: LlmRuntimeConfig,
    request: LlmRequest,
    includeResponseFormat: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": _ReadModelName(runtimeConfig),
        "messages": _BuildChatMessages(request),
        "stream": False,
        "temperature": request.generationOptions.temperature,
    }

    if request.generationOptions.maxTokens is not None:
        payload["max_tokens"] = request.generationOptions.maxTokens
    if request.generationOptions.topP is not None:
        payload["top_p"] = request.generationOptions.topP
    if request.generationOptions.stopSequences:
        payload["stop"] = list(request.generationOptions.stopSequences)
    if (
        includeResponseFormat
        and request.responseFormat == LlmResponseFormat.JSON_OBJECT
    ):
        payload["response_format"] = {"type": "json_object"}
    reasoningEffort = _ReadReasoningEffort(runtimeConfig)
    if reasoningEffort is not None:
        payload["reasoning_effort"] = reasoningEffort

    return payload


def _BuildOllamaPayload(
    runtimeConfig: LlmRuntimeConfig,
    request: LlmRequest,
) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "temperature": request.generationOptions.temperature,
    }

    if request.generationOptions.maxTokens is not None:
        options["num_predict"] = request.generationOptions.maxTokens
    if request.generationOptions.topP is not None:
        options["top_p"] = request.generationOptions.topP
    if request.generationOptions.stopSequences:
        options["stop"] = list(request.generationOptions.stopSequences)

    payload: Dict[str, Any] = {
        "model": _ReadModelName(runtimeConfig),
        "prompt": _BuildPlainPrompt(request),
        "stream": False,
        "options": options,
    }

    if request.responseFormat == LlmResponseFormat.JSON_OBJECT:
        payload["format"] = "json"

    return payload


def _BuildChatMessages(request: LlmRequest) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    systemContent = _BuildSystemContent(request)
    if systemContent != "":
        messages.append({"role": "system", "content": systemContent})

    messages.append({"role": "user", "content": request.userPrompt})
    return messages


def _BuildSystemContent(request: LlmRequest) -> str:
    parts: List[str] = []
    if request.systemPrompt is not None and request.systemPrompt.strip() != "":
        parts.append(request.systemPrompt.strip())

    if request.contextChunks:
        parts.append(
            "Use the following source-grounded context as reference material only."
        )
        parts.extend(request.contextChunks)

    return "\n\n".join(parts)


def _BuildPlainPrompt(request: LlmRequest) -> str:
    parts: List[str] = []
    systemContent = _BuildSystemContent(request)
    if systemContent != "":
        parts.append(systemContent)

    parts.append("User request:")
    parts.append(request.userPrompt)
    return "\n\n".join(parts)


def _PostJson(
    endpointUrl: str,
    payload: Dict[str, Any],
    timeoutSeconds: int,
    headers: Dict[str, str],
) -> Dict[str, Any]:
    request = Request(
        endpointUrl,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **headers,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeoutSeconds) as response:
            responseBody = response.read().decode("utf-8")
    except HTTPError as error:
        errorBody = error.read().decode("utf-8", errors="replace")
        raise RuntimeGenerationError(
            "Runtime HTTP request failed: {0} {1}".format(
                error.code,
                errorBody,
            )
        ) from error
    except URLError as error:
        raise RuntimeGenerationError(
            "Runtime endpoint is not reachable: {0}".format(error.reason)
        ) from error
    except (HTTPException, TimeoutError, OSError) as error:
        raise RuntimeGenerationError(
            "Runtime HTTP response could not be read: {0}".format(error)
        ) from error

    if responseBody.strip() == "":
        return {}

    try:
        responseData = json.loads(responseBody)
    except json.JSONDecodeError as error:
        raise RuntimeGenerationError(
            "Runtime response is not valid JSON."
        ) from error

    if not isinstance(responseData, dict):
        raise RuntimeGenerationError("Runtime response JSON must be an object.")

    return responseData


def _ExtractOpenAiChatText(responseData: Dict[str, Any]) -> str:
    firstChoice = _ReadFirstOpenAiChoice(responseData)
    if firstChoice is None:
        return ""

    message = firstChoice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if content is not None:
            return str(content)

    text = firstChoice.get("text")
    if text is not None:
        return str(text)

    return ""


def _ReadFirstOpenAiChoice(responseData: Dict[str, Any]) -> Dict[str, Any] | None:
    choices = responseData.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    firstChoice = choices[0]
    if not isinstance(firstChoice, dict):
        return None

    return firstChoice


def _ExtractOpenAiProviderFinishReason(
    responseData: Dict[str, Any],
) -> str | None:
    firstChoice = _ReadFirstOpenAiChoice(responseData)
    if firstChoice is None:
        return None

    finishReason = firstChoice.get("finish_reason")
    if finishReason is None:
        return None

    return str(finishReason)


def _ExtractOllamaProviderFinishReason(
    responseData: Dict[str, Any],
) -> str | None:
    finishReason = responseData.get("done_reason")
    if finishReason is not None:
        return str(finishReason)

    if responseData.get("done") is True:
        return LlmFinishReason.STOP.value

    return None


def _NormalizeFinishReason(
    providerFinishReason: str | None,
) -> LlmFinishReason:
    if providerFinishReason is None:
        return LlmFinishReason.UNKNOWN

    normalizedReason = providerFinishReason.strip().lower()
    if normalizedReason in {"stop", "complete", "completed"}:
        return LlmFinishReason.STOP
    if normalizedReason in {"length", "max_tokens", "max_token", "token_limit"}:
        return LlmFinishReason.LENGTH
    if normalizedReason == "content_filter":
        return LlmFinishReason.CONTENT_FILTER
    if normalizedReason in {"tool_calls", "function_call"}:
        return LlmFinishReason.TOOL_CALLS

    return LlmFinishReason.UNKNOWN


def _ExtractOpenAiTokenUsage(responseData: Dict[str, Any]) -> LlmTokenUsage:
    usage = responseData.get("usage")
    if not isinstance(usage, dict):
        return LlmTokenUsage()

    return LlmTokenUsage(
        inputTokens=_ReadOptionalInt(usage, "prompt_tokens"),
        outputTokens=_ReadOptionalInt(usage, "completion_tokens"),
        totalTokens=_ReadOptionalInt(usage, "total_tokens"),
    )


def _ExtractOllamaTokenUsage(responseData: Dict[str, Any]) -> LlmTokenUsage:
    inputTokens = _ReadOptionalInt(responseData, "prompt_eval_count")
    outputTokens = _ReadOptionalInt(responseData, "eval_count")
    totalTokens = None
    if inputTokens is not None and outputTokens is not None:
        totalTokens = inputTokens + outputTokens

    return LlmTokenUsage(
        inputTokens=inputTokens,
        outputTokens=outputTokens,
        totalTokens=totalTokens,
    )


def _ExtractResponseId(responseData: Dict[str, Any]) -> str | None:
    responseId = responseData.get("id")
    if isinstance(responseId, str) and responseId.strip() != "":
        return responseId

    return None


def _ReadOptionalInt(data: Dict[str, Any], fieldName: str) -> int | None:
    value = data.get(fieldName)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value

    return None


def _BuildEndpointUrl(baseUrl: str, defaultPath: str) -> str:
    normalizedBaseUrl = baseUrl.rstrip("/")
    normalizedDefaultPath = (
        defaultPath if defaultPath.startswith("/") else "/" + defaultPath
    )

    if normalizedBaseUrl.endswith(normalizedDefaultPath):
        return normalizedBaseUrl

    defaultPathParts = normalizedDefaultPath.strip("/").split("/")
    if len(defaultPathParts) > 1:
        defaultPathPrefix = "/" + defaultPathParts[0]
        defaultPathRemainder = "/" + "/".join(defaultPathParts[1:])
        if normalizedBaseUrl.endswith(defaultPathPrefix):
            return normalizedBaseUrl + defaultPathRemainder

    return normalizedBaseUrl + normalizedDefaultPath


def _ReadModelName(runtimeConfig: LlmRuntimeConfig) -> str:
    if runtimeConfig.modelName is not None and runtimeConfig.modelName.strip() != "":
        return runtimeConfig.modelName

    modelName = runtimeConfig.extraOptions.get("model")
    if isinstance(modelName, str) and modelName.strip() != "":
        return modelName

    raise RuntimeGenerationError("Runtime generation requires a model name.")


def _ReadTimeoutSeconds(runtimeConfig: LlmRuntimeConfig) -> int:
    timeoutSeconds = runtimeConfig.extraOptions.get("timeout_seconds")
    if isinstance(timeoutSeconds, int) and timeoutSeconds > 0:
        return timeoutSeconds

    return DEFAULT_HTTP_TIMEOUT_SECONDS


def _ReadHeaders(runtimeConfig: LlmRuntimeConfig) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    apiKey = runtimeConfig.extraOptions.get("api_key")
    if isinstance(apiKey, str) and apiKey.strip() != "":
        headers["Authorization"] = "Bearer {0}".format(apiKey)
        headers["x-api-key"] = apiKey

    return headers


def _ShouldIncludeOpenAiCompatibleResponseFormat(
    runtimeConfig: LlmRuntimeConfig,
) -> bool:
    optionValue = runtimeConfig.extraOptions.get("supports_response_format")
    if isinstance(optionValue, bool):
        return optionValue

    return False


def _ShouldIncludeOpenAiRuntimeResponseFormat(
    runtimeConfig: LlmRuntimeConfig,
) -> bool:
    optionValue = runtimeConfig.extraOptions.get("supports_response_format")
    if isinstance(optionValue, bool):
        return optionValue

    providerName = runtimeConfig.extraOptions.get("provider")
    if isinstance(providerName, str) and providerName.strip().lower() == "openai":
        return True

    return False


def _ReadStringOption(
    runtimeConfig: LlmRuntimeConfig,
    optionName: str,
    defaultValue: str,
) -> str:
    optionValue = runtimeConfig.extraOptions.get(optionName)
    if isinstance(optionValue, str) and optionValue.strip() != "":
        return optionValue.strip()

    return defaultValue


def _ReadReasoningEffort(runtimeConfig: LlmRuntimeConfig) -> str | None:
    optionValue = runtimeConfig.extraOptions.get("reasoning_effort")
    if not isinstance(optionValue, str):
        return None

    normalizedValue = optionValue.strip().lower()
    if normalizedValue in {"none", "minimal", "low", "medium", "high"}:
        return normalizedValue

    return None


def _ReadOpenAiHeaders(runtimeConfig: LlmRuntimeConfig) -> Dict[str, str]:
    apiKey = _ReadApiKey(runtimeConfig, DEFAULT_OPENAI_API_KEY_ENV_NAMES)
    if apiKey is None:
        raise RuntimeGenerationError(
            "Hosted LLM runtime generation requires {0} or "
            "extraOptions['api_key'].".format(PRIMARY_LLM_API_KEY_ENV_NAME)
        )

    return {
        "Authorization": "Bearer {0}".format(apiKey),
    }


def _ReadApiKey(
    runtimeConfig: LlmRuntimeConfig,
    apiKeyEnvNames: List[str],
) -> str | None:
    optionValue = runtimeConfig.extraOptions.get("api_key")
    if isinstance(optionValue, str) and optionValue.strip() != "":
        return optionValue.strip()

    for envName in apiKeyEnvNames:
        envValue = os.environ.get(envName)
        if envValue is not None and envValue.strip() != "":
            return envValue.strip()

    return None
