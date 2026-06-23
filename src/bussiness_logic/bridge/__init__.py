"""교체 가능한 LLM 런타임 선택과 adapter 경계."""

from bussiness_logic.bridge.adapter import RuntimeAdapter
from bussiness_logic.bridge.embedding import (
    BuildTextEmbeddingAdapter,
    BuildTextEmbeddingRuntimeConfig,
    BuildTextEmbeddingRuntimeConfigFromAppConfig,
    ProbeTextEmbeddingDependency,
    SentenceTransformerTextEmbeddingAdapter,
    TextEmbeddingAdapter,
    TextEmbeddingAdapterBuildError,
    TextEmbeddingDependencyStatus,
    TextEmbeddingGenerationError,
    TextEmbeddingProviderKind,
    TextEmbeddingRequest,
    TextEmbeddingResponse,
    TextEmbeddingRuntimeConfig,
)
from bussiness_logic.bridge.factory import (
    BuildRuntimeAdapter,
    BuildRuntimeDescriptor,
    RuntimeAdapterBuildError,
)
from bussiness_logic.bridge.generator import GenerateRuntimeResponse, RuntimeGenerationError
from bussiness_logic.bridge.probe import (
    ProbeRuntimeDependency,
    UnsupportedRuntimeProbeError,
)
from bussiness_logic.bridge.schema import (
    LlmFinishReason,
    LlmGenerationOptions,
    LlmRequest,
    LlmResponse,
    LlmResponseFormat,
    LlmRuntimeConfig,
    LlmRuntimeKind,
    LlmTokenUsage,
    OperatingSystemKind,
    RuntimeDescriptor,
    RuntimeDependencyStatus,
)
from bussiness_logic.bridge.selector import (
    BuildDefaultLlmRuntimeConfig,
    BuildLlmRuntimeConfigFromEnv,
    DetectOperatingSystem,
    SelectDefaultRuntimeKind,
    UnsupportedLlmRuntimeError,
)

__all__ = [
    "BuildDefaultLlmRuntimeConfig",
    "BuildLlmRuntimeConfigFromEnv",
    "BuildRuntimeAdapter",
    "BuildRuntimeDescriptor",
    "BuildTextEmbeddingAdapter",
    "BuildTextEmbeddingRuntimeConfig",
    "BuildTextEmbeddingRuntimeConfigFromAppConfig",
    "DetectOperatingSystem",
    "GenerateRuntimeResponse",
    "LlmFinishReason",
    "LlmGenerationOptions",
    "LlmRequest",
    "LlmResponse",
    "LlmResponseFormat",
    "LlmRuntimeConfig",
    "LlmRuntimeKind",
    "LlmTokenUsage",
    "OperatingSystemKind",
    "ProbeRuntimeDependency",
    "ProbeTextEmbeddingDependency",
    "RuntimeAdapterBuildError",
    "RuntimeAdapter",
    "RuntimeDescriptor",
    "RuntimeDependencyStatus",
    "RuntimeGenerationError",
    "SelectDefaultRuntimeKind",
    "SentenceTransformerTextEmbeddingAdapter",
    "TextEmbeddingAdapter",
    "TextEmbeddingAdapterBuildError",
    "TextEmbeddingDependencyStatus",
    "TextEmbeddingGenerationError",
    "TextEmbeddingProviderKind",
    "TextEmbeddingRequest",
    "TextEmbeddingResponse",
    "TextEmbeddingRuntimeConfig",
    "UnsupportedLlmRuntimeError",
    "UnsupportedRuntimeProbeError",
]
