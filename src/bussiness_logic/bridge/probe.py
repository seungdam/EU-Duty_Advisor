"""LLM runtime dependency probe."""

import os
from typing import List, Optional

from bussiness_logic.bridge.schema import (
    LlmRuntimeConfig,
    LlmRuntimeKind,
    RuntimeDependencyStatus,
)


DEFAULT_OMLX_ENDPOINT_URL = "http://127.0.0.1:8000"
DEFAULT_OLLAMA_ENDPOINT_URL = "http://localhost:11434"
DEFAULT_OPENAI_ENDPOINT_URL = "https://api.openai.com"
PRIMARY_LLM_API_KEY_ENV_NAME = "EU_EXPORT_LLM_API_KEY"
HOSTED_LLM_API_KEY_ENV_NAMES = [
    PRIMARY_LLM_API_KEY_ENV_NAME,
    "EU_EXPORT_OPENAI_API_KEY",
    "EU_EXPORT_GOOGLE_AI_STUDIO_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
]
DEFAULT_OPENAI_API_KEY_ENV_NAMES = HOSTED_LLM_API_KEY_ENV_NAMES


class UnsupportedRuntimeProbeError(RuntimeError):
    """probe 구현이 없는 runtimeKind가 들어왔을 때 사용한다."""


def ProbeRuntimeDependency(
    runtimeConfig: LlmRuntimeConfig,
) -> RuntimeDependencyStatus:
    """선택 runtime을 현재 환경에서 사용할 수 있는지 확인한다."""

    if runtimeConfig.runtimeKind == LlmRuntimeKind.OMLX:
        return _ProbeEndpointRuntime(
            runtimeConfig,
            DEFAULT_OMLX_ENDPOINT_URL,
            "oMLX API endpoint setting is available.",
        )

    if runtimeConfig.runtimeKind == LlmRuntimeKind.OLLAMA:
        return _ProbeEndpointRuntime(
            runtimeConfig,
            DEFAULT_OLLAMA_ENDPOINT_URL,
            "Ollama API endpoint setting is available.",
        )

    if runtimeConfig.runtimeKind == LlmRuntimeKind.OPENAI:
        return _ProbeApiKeyRuntime(
            runtimeConfig,
            DEFAULT_OPENAI_ENDPOINT_URL,
            DEFAULT_OPENAI_API_KEY_ENV_NAMES,
            "LLM API key setting is available.",
            "LLM API key setting is missing.",
        )

    raise UnsupportedRuntimeProbeError(
        "No runtime dependency probe is configured for: {0}".format(
            runtimeConfig.runtimeKind.value,
        )
    )


def _ProbeApiKeyRuntime(
    runtimeConfig: LlmRuntimeConfig,
    defaultEndpointUrl: str,
    apiKeyEnvNames: List[str],
    availableMessage: str,
    missingMessage: str,
) -> RuntimeDependencyStatus:
    endpointUrl = runtimeConfig.endpointUrl or defaultEndpointUrl
    apiKey = _ReadApiKey(runtimeConfig, apiKeyEnvNames)

    if apiKey is not None:
        return RuntimeDependencyStatus(
            runtimeKind=runtimeConfig.runtimeKind,
            isAvailable=True,
            message=availableMessage,
            endpointUrl=endpointUrl,
            limitations=[
                "Dependency probe does not call the external API.",
                "Runtime availability only means an API key was configured.",
            ],
        )

    return RuntimeDependencyStatus(
        runtimeKind=runtimeConfig.runtimeKind,
        isAvailable=False,
        message=missingMessage,
        endpointUrl=endpointUrl,
        limitations=[
            "Set {0} in .env for hosted LLM APIs.".format(
                PRIMARY_LLM_API_KEY_ENV_NAME,
            ),
            "Alternatively pass extraOptions['api_key'] in LlmRuntimeConfig.",
        ],
    )


def _ProbeEndpointRuntime(
    runtimeConfig: LlmRuntimeConfig,
    defaultEndpointUrl: str,
    availableMessage: str,
) -> RuntimeDependencyStatus:
    endpointUrl = runtimeConfig.endpointUrl or defaultEndpointUrl

    if runtimeConfig.modelName is not None and runtimeConfig.modelName.strip() != "":
        return RuntimeDependencyStatus(
            runtimeKind=runtimeConfig.runtimeKind,
            isAvailable=True,
            message=availableMessage,
            endpointUrl=endpointUrl,
            limitations=[
                "Dependency probe does not call the runtime HTTP endpoint.",
                "Runtime availability only means endpoint and model are configured.",
            ],
        )

    return RuntimeDependencyStatus(
        runtimeKind=runtimeConfig.runtimeKind,
        isAvailable=False,
        message="Local API runtime requires EU_EXPORT_LLM_MODEL.",
        endpointUrl=endpointUrl,
        limitations=[
            "Set EU_EXPORT_LLM_MODEL to the model served by the local runtime.",
        ],
    )


def _ReadApiKey(
    runtimeConfig: LlmRuntimeConfig,
    apiKeyEnvNames: List[str],
) -> Optional[str]:
    optionValue = runtimeConfig.extraOptions.get("api_key")
    if isinstance(optionValue, str) and optionValue.strip() != "":
        return optionValue.strip()

    for envName in apiKeyEnvNames:
        envValue = os.environ.get(envName)
        if envValue is not None and envValue.strip() != "":
            return envValue.strip()

    return None
