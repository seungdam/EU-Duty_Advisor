"""비밀값이 아닌 앱 실행 설정을 TOML에서 Pydantic model로 읽는다."""

from pathlib import Path
from typing import Any, Optional

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
REASONING_EFFORT_VALUES = frozenset({"none", "minimal", "low", "medium", "high"})


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

    ontology_root: Path = Path("docs/ASAP_Ontology_v1")
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
    blackboard_schema: Path = Path(
        "docs/ASAP_Ontology_v1/linkml/generated/asap_runtime.schema.json",
    )
    dash_url_intake_artifact_root: Path = Path("artifacts/asap_dash_url_intake")
    taric_master_table: Path = Path(
        "data/processed/TARIC/taric_master_table.csv",
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
    max_ocr_image_count: StrictInt = 8
    structured_ocr_max_tile_height_pixels: StrictInt = 2400
    structured_ocr_max_tile_side_pixels: StrictInt = 4000
    structured_ocr_tile_overlap_pixels: StrictInt = 240
    structured_ocr_use_projection_tiling: StrictBool = True
    structured_ocr_allow_hard_cut_fallback: StrictBool = False
    use_input_reconstruction: StrictBool = True
    use_llm_input_reconstruction: StrictBool = False
    write_llm_input_reconstruction_debug_artifacts: StrictBool = True
    llm_input_reconstruction_max_tokens: StrictInt = 4096
    input_dictionary_path: Optional[Path] = None
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


class OntologySmokeAppConfig(BaseModel):
    """온톨로지 smoke 설정."""

    model_config = ConfigDict(extra="ignore")

    phase_id: StrictStr = "stage1_classification"
    top_k: StrictInt = 8
    max_result_count: StrictInt = 6
    cn_candidate_top_k: StrictInt = 5
    max_product_smoke_inputs: StrictInt = 2
    answer_csv_path: Path = Path("data/answer.csv")
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


class AppConfig(BaseModel):
    """비밀값이 아닌 프로젝트 실행 설정."""

    model_config = ConfigDict(extra="ignore")

    llm: LlmAppConfig = Field(default_factory=LlmAppConfig)
    embedding: EmbeddingAppConfig = Field(default_factory=EmbeddingAppConfig)
    paths: AppPathsConfig = Field(default_factory=AppPathsConfig)
    classification: ClassificationAppConfig = Field(
        default_factory=ClassificationAppConfig,
    )
    kurly_smoke: KurlySmokeAppConfig = Field(default_factory=KurlySmokeAppConfig)
    ontology_smoke: OntologySmokeAppConfig = Field(
        default_factory=OntologySmokeAppConfig,
    )


def LoadAppConfig(
    projectRootPath: str | Path,
    configPath: Optional[str | Path] = None,
) -> AppConfig:
    resolvedProjectRootPath = Path(projectRootPath)
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
