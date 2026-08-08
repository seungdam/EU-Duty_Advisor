"""비밀값이 아닌 앱 실행 설정을 TOML에서 Pydantic model로 읽는다."""

import os
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 이하 fallback 안내용
    tomllib = None  # type: ignore[assignment]


APP_CONFIG_FILE_NAME = ".appconfig"
APP_CONFIG_PATH_ENV_NAME = "ASAP_APP_CONFIG_PATH"
REASONING_EFFORT_VALUES = frozenset({"none", "minimal", "low", "medium", "high"})
LLM_PROVIDER_SCOPED_FIELDS = frozenset(
    {
        "model",
        "endpoint_url",
        "chat_completions_path",
        "supports_response_format",
        "reasoning_effort",
        "api_key_env",
    }
)
HOSTED_LLM_RUNTIME_NAMES = frozenset({"openai", "anthropic"})


class LlmProfileName(str, Enum):
    """서로 다른 모델 설정을 가질 수 있는 LLM/VLM 호출 용도."""

    PRODUCT_VLM = "product_vlm"
    PRODUCT_VLM_GPT = "product_vlm_gpt"
    PRODUCT_VLM_GEMINI = "product_vlm_gemini"
    PRODUCT_VLM_CLAUDE = "product_vlm_claude"
    INPUT_RECONSTRUCTION = "input_reconstruction"
    INPUT_RECONSTRUCTION_GPT = "input_reconstruction_gpt"
    INPUT_RECONSTRUCTION_GEMINI = "input_reconstruction_gemini"
    INPUT_RECONSTRUCTION_CLAUDE = "input_reconstruction_claude"
    IDENTITY_HINT = "identity_hint"
    HS2_ROUTER = "hs2_router"
    CLASSIFICATION_VALIDATOR = "classification_validator"
    CN_PREDICATE_COMPILER = "cn_predicate_compiler"


class LlmAppConfig(BaseModel):
    """LLM runtime profile 설정."""

    model_config = ConfigDict(extra="ignore")

    runtime: Optional[StrictStr] = None
    provider: Optional[StrictStr] = None
    model: Optional[StrictStr] = None
    endpoint_url: Optional[StrictStr] = None
    chat_completions_path: Optional[StrictStr] = None
    timeout_seconds: Optional[StrictInt] = None
    supports_response_format: Optional[StrictBool] = None
    reasoning_effort: Optional[StrictStr] = None
    api_key_env: Optional[StrictStr] = None

    @field_validator("reasoning_effort")
    @classmethod
    def NormalizeReasoningEffort(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None

        normalizedValue = value.strip().lower()
        if normalizedValue not in REASONING_EFFORT_VALUES:
            raise ValueError(
                "reasoning_effort must be one of: {0}".format(
                    ", ".join(sorted(REASONING_EFFORT_VALUES)),
                )
            )
        return normalizedValue

    @field_validator("api_key_env")
    @classmethod
    def NormalizeApiKeyEnv(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalizedValue = value.strip()
        if normalizedValue == "":
            return None
        if not normalizedValue.replace("_", "").isalnum():
            raise ValueError("api_key_env must be an environment variable name.")
        return normalizedValue


class EmbeddingAppConfig(BaseModel):
    """Semantic retrieval embedding runtime profile 설정."""

    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool = False
    runtime: StrictStr = "local"
    provider: StrictStr = "sentence_transformers"
    model: StrictStr = "intfloat/multilingual-e5-small"
    device: Optional[StrictStr] = "mps"
    batch_size: StrictInt = 32
    normalize_embeddings: StrictBool = True
    local_files_only: StrictBool = True

    @field_validator("runtime", "provider")
    @classmethod
    def NormalizeRuntimeName(cls, value: str) -> str:
        normalizedValue = value.strip()
        if normalizedValue == "":
            raise ValueError("embedding runtime/provider must not be empty.")
        return normalizedValue.lower()

    @field_validator("model")
    @classmethod
    def NormalizeModelName(cls, value: str) -> str:
        normalizedValue = value.strip()
        if normalizedValue == "":
            raise ValueError("embedding model must not be empty.")
        return normalizedValue

    @field_validator("batch_size")
    @classmethod
    def ValidateBatchSize(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("embedding batch_size must be positive.")
        return value

    @field_validator("device")
    @classmethod
    def NormalizeOptionalString(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalizedValue = value.strip()
        if normalizedValue == "":
            return None
        return normalizedValue.lower()


class AppPathsConfig(BaseModel):
    """앱 실행 중 참조하는 경로 설정."""

    model_config = ConfigDict(extra="ignore")

    ontology_root: Path = Path("docs/ASAP_Ontology/linkml")
    ontology_smoke_summary_artifact: Path = Path(
        "artifacts/core-smoke/runtime-smoke-summary.json",
    )
    ontology_human_review_package_artifact: Path = Path(
        "artifacts/core-smoke/stage1-human-review-package.json",
    )
    kurly_smoke_artifact_root: Path = Path("artifacts/kurly-market-smoke")
    kurly_smoke_summary_artifact: Path = Path(
        "artifacts/kurly-market-smoke/runtime-smoke-summary.json",
    )
    blackboard_runs_root: Path = Path("artifacts/runs")
    pipeline_outputs_root: Path = Path("artifacts/outputs")
    product_input_artifact_root: Path = Path("artifacts/product_input")
    blackboard_schema: Path = Path(
        "docs/ASAP_Ontology/linkml/generated/asap_runtime.schema.json",
    )

    def ResolvePath(self, projectRootPath: str | Path, configPath: Path) -> Path:
        expandedPath = configPath.expanduser()
        if expandedPath.is_absolute():
            return expandedPath
        return Path(projectRootPath) / expandedPath


class KurlySmokeAppConfig(BaseModel):
    """KurlyMarket 상품 수집 smoke 설정."""

    model_config = ConfigDict(extra="ignore")

    product_urls: list[StrictStr] = Field(
        default_factory=list,
    )
    timeout_seconds: StrictInt = 60
    scroll_count: StrictInt = 8
    headless: StrictBool = True
    run_ocr_fallback: StrictBool = True
    use_structured_ocr: StrictBool = True
    structured_ocr_provider: StrictStr = "paddleocr_vl"
    max_ocr_image_count: StrictInt = 8
    download_workers: StrictInt = 4
    max_queued_downloads: StrictInt = 4
    max_pending_ocr_images: StrictInt = 4
    structured_ocr_max_tile_height_pixels: StrictInt = 2400
    structured_ocr_max_tile_side_pixels: StrictInt = 4000
    structured_ocr_tile_overlap_pixels: StrictInt = 240
    structured_ocr_use_projection_tiling: StrictBool = True
    structured_ocr_allow_hard_cut_fallback: StrictBool = False
    structured_ocr_vl_rec_backend: Optional[StrictStr] = None
    structured_ocr_vl_rec_server_url: Optional[StrictStr] = None
    structured_ocr_vl_rec_api_model_name: Optional[StrictStr] = None
    use_input_reconstruction: StrictBool = True
    use_llm_input_reconstruction: StrictBool = True
    llm_input_reconstruction_max_tokens: StrictInt = 4096
    input_dictionary_fuzzy_min_ratio: StrictFloat = 0.86
    write_summary_artifact: StrictBool = True
    log_full_result: StrictBool = False
    max_logged_notice_options: StrictInt = 3
    max_logged_fields_per_option: StrictInt = 5
    max_logged_ocr_candidate_urls: StrictInt = 5
    field_value_preview_characters: StrictInt = 220
    ocr_text_preview_characters: StrictInt = 500

    @field_validator("product_urls")
    @classmethod
    def NormalizeProductUrls(cls, value: list[StrictStr]) -> list[str]:
        normalizedUrls = [
            item.strip()
            for item in value
            if (
                isinstance(item, str)
                and item.strip().startswith(("http://", "https://"))
            )
        ]
        return normalizedUrls

    @field_validator("llm_input_reconstruction_max_tokens")
    @classmethod
    def ValidateLlmInputReconstructionMaxTokens(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("llm_input_reconstruction_max_tokens must be positive.")
        return value

    @field_validator("structured_ocr_provider")
    @classmethod
    def NormalizeStructuredOcrProvider(cls, value: str) -> str:
        normalizedValue = value.strip().lower()
        if normalizedValue not in {"paddleocr_vl", "llm_bridge"}:
            raise ValueError(
                "structured_ocr_provider must be paddleocr_vl or llm_bridge."
            )
        return normalizedValue

    @field_validator("max_ocr_image_count")
    @classmethod
    def ValidateMaxOcrImageCount(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_ocr_image_count must be non-negative.")
        return value

    @field_validator("download_workers")
    @classmethod
    def ValidateDownloadWorkers(cls, value: int) -> int:
        if not 1 <= value <= 16:
            raise ValueError("download_workers must be between 1 and 16.")
        return value

    @field_validator("max_queued_downloads")
    @classmethod
    def ValidateMaxQueuedDownloads(cls, value: int) -> int:
        if not 0 <= value <= 64:
            raise ValueError("max_queued_downloads must be between 0 and 64.")
        return value

    @field_validator("max_pending_ocr_images")
    @classmethod
    def ValidateMaxPendingOcrImages(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError("max_pending_ocr_images must be between 1 and 64.")
        return value

    def BuildStructuredOcrVlExtraOptions(self) -> dict[str, str]:
        options = {
            "vl_rec_backend": self.structured_ocr_vl_rec_backend,
            "vl_rec_server_url": self.structured_ocr_vl_rec_server_url,
            "vl_rec_api_model_name": self.structured_ocr_vl_rec_api_model_name,
        }
        return {
            key: value.strip()
            for key, value in options.items()
            if isinstance(value, str) and value.strip()
        }


class OntologySmokeAppConfig(BaseModel):
    """온톨로지 smoke 설정."""

    model_config = ConfigDict(extra="ignore")

    phase_id: StrictStr = "stage1_classification"
    top_k: StrictInt = 8
    max_result_count: StrictInt = 6
    cn_candidate_top_k: StrictInt = 5
    max_product_smoke_inputs: StrictInt = 2
    run_kurly_smoke_before_ontology: StrictBool = False
    write_summary_artifact: StrictBool = True
    run_llm_connection_smoke: StrictBool = False
    text_preview_characters: StrictInt = 700
    validation_issue_preview_count: StrictInt = 3
    resource_check_preview_count: StrictInt = 8
    max_validation_fixture_candidates: StrictInt = 3
    stage1_backtracking_retry_attempt: StrictInt = 0
    use_semantic_candidate_retrieval: StrictBool = True
    semantic_candidate_top_k: StrictInt = 8
    semantic_min_score: StrictFloat = 0.0
    hybrid_candidate_limit: Optional[StrictInt] = None


class ClassificationAppConfig(BaseModel):
    """Stage 1 CN 후보 검색 설정."""

    model_config = ConfigDict(extra="ignore")

    use_semantic_candidate_retrieval: StrictBool = True
    semantic_candidate_top_k: StrictInt = 8
    semantic_min_score: StrictFloat = 0.0
    hybrid_candidate_limit: Optional[StrictInt] = None
    beam_hs2_per_parent: StrictInt = 3
    beam_hs4_per_parent: StrictInt = 3
    beam_hs6_per_parent: StrictInt = 3
    beam_hs2_global_limit: StrictInt = 3
    beam_hs4_global_limit: StrictInt = 9
    beam_hs6_global_limit: StrictInt = 18
    beam_semantic_slots_per_parent: StrictInt = 1
    backtracking_max_retry_count: StrictInt = 1

    @field_validator(
        "semantic_candidate_top_k",
        "beam_hs2_per_parent",
        "beam_hs4_per_parent",
        "beam_hs6_per_parent",
        "beam_hs2_global_limit",
        "beam_hs4_global_limit",
        "beam_hs6_global_limit",
    )
    @classmethod
    def ValidatePositiveClassificationLimit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("classification candidate limits must be positive.")
        return value

    @field_validator(
        "beam_semantic_slots_per_parent",
    )
    @classmethod
    def ValidateNonNegativeClassificationLimit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("classification semantic limits must be non-negative.")
        return value

    @field_validator("backtracking_max_retry_count")
    @classmethod
    def ValidateBacktrackingRetryCount(cls, value: int) -> int:
        if value not in {0, 1}:
            raise ValueError(
                "classification backtracking_max_retry_count must be 0 or 1."
            )
        return value


class PipelineExecutionAppConfig(BaseModel):
    """단일 web process 내부 pipeline run 실행 한도."""

    model_config = ConfigDict(extra="ignore")

    max_workers: StrictInt = 2
    max_queued_runs: StrictInt = 2

    @field_validator("max_workers")
    @classmethod
    def ValidateMaxWorkers(cls, value: int) -> int:
        if not 1 <= value <= 8:
            raise ValueError("pipeline_execution max_workers must be between 1 and 8.")
        return value

    @field_validator("max_queued_runs")
    @classmethod
    def ValidateMaxQueuedRuns(cls, value: int) -> int:
        if not 0 <= value <= 64:
            raise ValueError(
                "pipeline_execution max_queued_runs must be between 0 and 64."
            )
        return value


class WebAppConfig(BaseModel):
    """React webapp와 pipeline backend 실행 경계 설정."""

    model_config = ConfigDict(extra="ignore")

    backend_host: StrictStr = "127.0.0.1"
    backend_port: StrictInt = 8060
    allowed_frontend_origins: list[StrictStr] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:4173",
            "http://localhost:4173",
        ],
    )

    @field_validator("backend_port")
    @classmethod
    def ValidatePort(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("web port must be between 1 and 65535.")
        return value

    @field_validator("allowed_frontend_origins")
    @classmethod
    def NormalizeAllowedFrontendOrigins(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(
            origin.strip().rstrip("/")
            for origin in value
            if origin.strip().startswith(("http://", "https://"))
        ))

class AppConfig(BaseModel):
    """비밀값이 아닌 프로젝트 실행 설정."""

    model_config = ConfigDict(extra="ignore")

    llm: LlmAppConfig = Field(default_factory=LlmAppConfig)
    llm_profiles: Dict[LlmProfileName, LlmAppConfig] = Field(
        default_factory=dict,
    )
    embedding: EmbeddingAppConfig = Field(default_factory=EmbeddingAppConfig)
    paths: AppPathsConfig = Field(default_factory=AppPathsConfig)
    classification: ClassificationAppConfig = Field(
        default_factory=ClassificationAppConfig,
    )
    pipeline_execution: PipelineExecutionAppConfig = Field(
        default_factory=PipelineExecutionAppConfig,
    )
    web: WebAppConfig = Field(default_factory=WebAppConfig)
    kurly_smoke: KurlySmokeAppConfig = Field(default_factory=KurlySmokeAppConfig)
    ontology_smoke: OntologySmokeAppConfig = Field(
        default_factory=OntologySmokeAppConfig,
    )

    def ResolveLlmProfile(self, profileName: LlmProfileName) -> LlmAppConfig:
        profileConfig = self.llm_profiles.get(profileName)
        if profileConfig is None:
            return self.llm.model_copy(deep=True)

        profileValues = profileConfig.model_dump(exclude_none=True)
        runtimeChanged = (
            profileConfig.runtime is not None
            and profileConfig.runtime.strip().lower()
            != (self.llm.runtime or "").strip().lower()
        )
        providerChanged = (
            profileConfig.provider is not None
            and profileConfig.provider.strip().lower()
            != (self.llm.provider or "").strip().lower()
        )
        if not runtimeChanged and not providerChanged:
            return self.llm.model_copy(update=profileValues, deep=True)

        resolvedValues = self.llm.model_dump()
        for fieldName in LLM_PROVIDER_SCOPED_FIELDS:
            resolvedValues.pop(fieldName, None)
        if runtimeChanged and profileConfig.provider is None:
            resolvedValues.pop("provider", None)
        resolvedValues.update(profileValues)

        resolvedRuntime = str(resolvedValues.get("runtime") or "").strip().lower()
        if (
            resolvedRuntime in HOSTED_LLM_RUNTIME_NAMES
            and profileConfig.api_key_env is None
        ):
            raise ValueError(
                "llm_profiles.{0}.api_key_env is required when runtime or "
                "provider differs from [llm].".format(profileName.value)
            )
        return LlmAppConfig.model_validate(resolvedValues)


def LoadEnvironmentFile(environmentFilePath: str | Path) -> bool:
    """Load non-empty values without overriding the process environment."""

    try:
        content = Path(environmentFilePath).expanduser().read_text(encoding="utf-8")
    except FileNotFoundError:
        return False

    for rawLine in content.splitlines():
        line = rawLine.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, rawValue = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = rawValue.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value and key not in os.environ:
            os.environ[key] = value
    return True


def LoadAppConfig(
    projectRootPath: str | Path,
    configPath: Optional[str | Path] = None,
) -> AppConfig:
    resolvedProjectRootPath = Path(projectRootPath)
    if configPath is None:
        configPath = os.environ.get(APP_CONFIG_PATH_ENV_NAME)
    if configPath is not None:
        rawConfigPath = Path(configPath).expanduser()
        resolvedConfigPath = (
            rawConfigPath
            if rawConfigPath.is_absolute()
            else resolvedProjectRootPath / rawConfigPath
        )
    else:
        resolvedConfigPath = resolvedProjectRootPath / APP_CONFIG_FILE_NAME
    if not resolvedConfigPath.exists():
        return AppConfig()
    if tomllib is None:
        raise RuntimeError("Python 3.11+ tomllib is required to read .appconfig.")

    with resolvedConfigPath.open("rb") as configFile:
        configData = tomllib.load(configFile)
    if not isinstance(configData, dict):
        return AppConfig()
    return AppConfig.model_validate(configData)
