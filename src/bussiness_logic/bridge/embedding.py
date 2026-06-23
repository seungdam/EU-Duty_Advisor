"""Semantic retrieval용 text embedding runtime adapter."""

from __future__ import annotations

import importlib.util
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field

from bussiness_logic.app_config import AppConfig, EmbeddingAppConfig, LoadAppConfig


class TextEmbeddingProviderKind(str, Enum):
    """교체 가능한 embedding provider 종류."""

    SENTENCE_TRANSFORMERS = "sentence_transformers"


class TextEmbeddingRuntimeConfig(BaseModel):
    """embedding adapter에 전달할 런타임 설정."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    enabled: bool = False
    runtime: str = "local"
    provider: TextEmbeddingProviderKind = TextEmbeddingProviderKind.SENTENCE_TRANSFORMERS
    modelName: str = Field(
        default="intfloat/multilingual-e5-small",
        alias="model_name",
    )
    device: Optional[str] = None
    batchSize: int = Field(default=32, alias="batch_size")
    normalizeEmbeddings: bool = Field(
        default=True,
        alias="normalize_embeddings",
    )
    localFilesOnly: bool = Field(default=True, alias="local_files_only")


class TextEmbeddingDependencyStatus(BaseModel):
    """선택 embedding provider를 현재 환경에서 사용할 수 있는지 나타낸다."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    provider: TextEmbeddingProviderKind
    isAvailable: bool = Field(alias="is_available")
    message: str
    moduleName: Optional[str] = Field(default=None, alias="module_name")
    limitations: List[str] = Field(default_factory=list)


class TextEmbeddingRequest(BaseModel):
    """adapter에 전달할 embedding 요청."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    texts: List[str]


class TextEmbeddingResponse(BaseModel):
    """provider 독립 embedding 응답."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    provider: TextEmbeddingProviderKind
    modelName: str = Field(alias="model_name")
    embeddings: List[List[float]]

    @computed_field(alias="input_count")
    @property
    def inputCount(self) -> int:
        return len(self.embeddings)

    @computed_field(alias="embedding_dimension")
    @property
    def embeddingDimension(self) -> int:
        if not self.embeddings:
            return 0
        return len(self.embeddings[0])


class TextEmbeddingAdapter(Protocol):
    """semantic retrieval 단계가 의존하는 provider 독립 interface."""

    def EmbedTexts(self, request: TextEmbeddingRequest) -> TextEmbeddingResponse:
        """텍스트 목록을 embedding vector 목록으로 변환한다."""


class TextEmbeddingAdapterBuildError(RuntimeError):
    """embedding adapter를 만들 수 없을 때 사용한다."""


class TextEmbeddingGenerationError(RuntimeError):
    """embedding 생성 중 provider 호출이 실패했을 때 사용한다."""


class SentenceTransformerTextEmbeddingAdapter:
    """sentence-transformers 기반 local embedding adapter."""

    def __init__(self, runtimeConfig: TextEmbeddingRuntimeConfig) -> None:
        self._runtimeConfig = runtimeConfig
        self._model: Any = None

    def EmbedTexts(self, request: TextEmbeddingRequest) -> TextEmbeddingResponse:
        if not request.texts:
            return TextEmbeddingResponse(
                provider=self._runtimeConfig.provider,
                modelName=self._runtimeConfig.modelName,
                embeddings=[],
            )

        model = self._LoadModel()
        try:
            encodedEmbeddings = model.encode(
                request.texts,
                batch_size=self._runtimeConfig.batchSize,
                normalize_embeddings=self._runtimeConfig.normalizeEmbeddings,
                show_progress_bar=False,
            )
        except Exception as exception:
            raise TextEmbeddingGenerationError(
                "Text embedding generation failed: {0}".format(exception)
            ) from exception

        vectors = (
            encodedEmbeddings.tolist()
            if hasattr(encodedEmbeddings, "tolist")
            else encodedEmbeddings
        )
        return TextEmbeddingResponse(
            provider=self._runtimeConfig.provider,
            modelName=self._runtimeConfig.modelName,
            embeddings=[
                [float(value) for value in vector]
                for vector in vectors
            ],
        )

    def _LoadModel(self) -> Any:
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exception:
            raise TextEmbeddingAdapterBuildError(
                "sentence-transformers package is required for semantic embedding."
            ) from exception

        if self._runtimeConfig.device is None:
            try:
                self._model = SentenceTransformer(
                    self._runtimeConfig.modelName,
                    local_files_only=self._runtimeConfig.localFilesOnly,
                )
            except Exception as exception:
                raise TextEmbeddingAdapterBuildError(
                    "Text embedding model load failed: {0}".format(exception)
                ) from exception
        else:
            try:
                self._model = SentenceTransformer(
                    self._runtimeConfig.modelName,
                    device=self._runtimeConfig.device,
                    local_files_only=self._runtimeConfig.localFilesOnly,
                )
            except Exception as exception:
                raise TextEmbeddingAdapterBuildError(
                    "Text embedding model load failed: {0}".format(exception)
                ) from exception
        return self._model


def BuildTextEmbeddingRuntimeConfigFromAppConfig(
    projectRootPath: str | Path = ".",
    appConfigPath: Optional[str | Path] = None,
    appConfig: Optional[AppConfig] = None,
) -> TextEmbeddingRuntimeConfig:
    """`.appconfig`의 embedding section을 runtime config로 변환한다."""

    resolvedAppConfig = (
        appConfig
        if appConfig is not None
        else LoadAppConfig(projectRootPath, appConfigPath)
    )
    return BuildTextEmbeddingRuntimeConfig(resolvedAppConfig.embedding)


def BuildTextEmbeddingRuntimeConfig(
    embeddingConfig: EmbeddingAppConfig,
) -> TextEmbeddingRuntimeConfig:
    """Pydantic app config를 embedding runtime config로 변환한다."""

    return TextEmbeddingRuntimeConfig(
        enabled=embeddingConfig.enabled,
        runtime=embeddingConfig.runtime,
        provider=embeddingConfig.provider,
        modelName=embeddingConfig.model,
        device=embeddingConfig.device,
        batchSize=embeddingConfig.batch_size,
        normalizeEmbeddings=embeddingConfig.normalize_embeddings,
        localFilesOnly=embeddingConfig.local_files_only,
    )


def ProbeTextEmbeddingDependency(
    runtimeConfig: TextEmbeddingRuntimeConfig,
) -> TextEmbeddingDependencyStatus:
    """선택 embedding provider의 기본 dependency를 확인한다."""

    if not runtimeConfig.enabled:
        return TextEmbeddingDependencyStatus(
            provider=runtimeConfig.provider,
            isAvailable=False,
            message="Text embedding runtime is disabled by appconfig.",
            limitations=["Set [embedding].enabled = true to use semantic retrieval."],
        )

    if runtimeConfig.provider == TextEmbeddingProviderKind.SENTENCE_TRANSFORMERS:
        moduleName = "sentence_transformers"
        isAvailable = importlib.util.find_spec(moduleName) is not None
        limitations: List[str] = []
        if isAvailable and runtimeConfig.device == "mps":
            mpsStatusMessage = _ReadMpsProbeMessage()
            if mpsStatusMessage is not None:
                limitations.append(mpsStatusMessage)
        if isAvailable:
            return TextEmbeddingDependencyStatus(
                provider=runtimeConfig.provider,
                isAvailable=True,
                message="sentence-transformers package is available.",
                moduleName=moduleName,
                limitations=limitations,
            )
        return TextEmbeddingDependencyStatus(
            provider=runtimeConfig.provider,
            isAvailable=False,
            message="sentence-transformers package is not installed.",
            moduleName=moduleName,
            limitations=[
                "Install sentence-transformers in the conda environment before enabling semantic retrieval.",
            ],
        )

    return TextEmbeddingDependencyStatus(
        provider=runtimeConfig.provider,
        isAvailable=False,
        message="Unsupported text embedding provider: {0}".format(
            runtimeConfig.provider.value,
        ),
    )


def BuildTextEmbeddingAdapter(
    runtimeConfig: TextEmbeddingRuntimeConfig,
    dependencyStatus: Optional[TextEmbeddingDependencyStatus] = None,
    requireAvailable: bool = True,
) -> Optional[TextEmbeddingAdapter]:
    """runtime config와 dependency 상태를 바탕으로 embedding adapter를 만든다."""

    if not runtimeConfig.enabled:
        return None

    resolvedDependencyStatus = (
        dependencyStatus
        if dependencyStatus is not None
        else ProbeTextEmbeddingDependency(runtimeConfig)
    )
    if requireAvailable and not resolvedDependencyStatus.isAvailable:
        raise TextEmbeddingAdapterBuildError(resolvedDependencyStatus.message)

    if runtimeConfig.provider == TextEmbeddingProviderKind.SENTENCE_TRANSFORMERS:
        return SentenceTransformerTextEmbeddingAdapter(runtimeConfig)

    raise TextEmbeddingAdapterBuildError(
        "Unsupported text embedding provider: {0}".format(
            runtimeConfig.provider.value,
        )
    )


def _ReadMpsProbeMessage() -> Optional[str]:
    if importlib.util.find_spec("torch") is None:
        return "Device is set to mps, but torch package is not installed."
    return "Device is set to mps; probe does not initialize PyTorch backend."
