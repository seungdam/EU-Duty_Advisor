"""교체 가능한 LLM 런타임 adapter."""

from typing import Callable, Generic, TypeVar

from bussiness_logic.bridge.schema import (
    LlmRequest,
    LlmResponse,
    LlmRuntimeConfig,
    LlmRuntimeKind,
)


RuntimeDescriptorT = TypeVar("RuntimeDescriptorT")
GenerateCallable = Callable[
    [RuntimeDescriptorT, LlmRuntimeConfig, LlmRequest],
    LlmResponse,
]


class RuntimeAdapter(Generic[RuntimeDescriptorT]):
    """런타임 설명자와 generate 함수를 주입받는 generic adapter."""

    def __init__(
        self,
        runtimeConfig: LlmRuntimeConfig,
        runtimeDescriptor: RuntimeDescriptorT,
        generateCallable: GenerateCallable[RuntimeDescriptorT],
    ) -> None:
        self._runtimeConfig = runtimeConfig
        self._runtimeDescriptor = runtimeDescriptor
        self._generateCallable = generateCallable

    def RuntimeKind(self) -> LlmRuntimeKind:
        """adapter가 감싸는 실제 런타임 종류를 반환한다."""
        return self._runtimeConfig.runtimeKind

    def RuntimeConfig(self) -> LlmRuntimeConfig:
        """adapter 생성 시 확정된 런타임 설정을 반환한다."""
        return self._runtimeConfig

    def RuntimeDescriptor(self) -> RuntimeDescriptorT:
        """adapter에 주입된 runtime descriptor를 반환한다."""
        return self._runtimeDescriptor

    def Generate(self, request: LlmRequest) -> LlmResponse:
        """LLM 생성을 수행하고 런타임 독립 응답으로 변환한다."""
        return self._generateCallable(
            self._runtimeDescriptor,
            self._runtimeConfig,
            request,
        )
