from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'monitor.db'}"
    runs_dir: Path = PROJECT_DIR / "runs"
    coverage_path: Path = PROJECT_DIR / "config" / "coverage" / "dram_hbm.yaml"
    fixtures_dir: Path = PROJECT_DIR / "fixtures"
    fixed_sources_path: Path = PROJECT_DIR / "config" / "sources" / "official_sources.yaml"
    source_registry_path: Path = PROJECT_DIR / "config" / "sources" / "dram_hbm_sources.yaml"
    fetch_audits_dir: Path = PROJECT_DIR / "fetches"
    source_collection_max_documents: int = Field(
        default=10, ge=1, le=10, validation_alias="SOURCE_COLLECTION_MAX_DOCUMENTS"
    )
    source_collection_max_analysis: int = Field(
        default=3, ge=0, le=3, validation_alias="SOURCE_COLLECTION_MAX_ANALYSIS"
    )
    fixed_fetch_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        le=120,
        validation_alias="FIXED_FETCH_TIMEOUT_SECONDS",
    )
    fixed_fetch_max_bytes: int = Field(
        default=2_000_000,
        ge=10_000,
        le=10_000_000,
        validation_alias="FIXED_FETCH_MAX_BYTES",
    )
    fixed_fetch_user_agent: str = Field(
        default="AI-Intelligence-Monitor/0.2 (controlled official-source collector)",
        validation_alias="FIXED_FETCH_USER_AGENT",
    )
    prompt_version: str = "phase0-prompt-v1"
    llm_provider: Literal["openai", "deepseek"] = Field(
        default="openai",
        validation_alias="LLM_PROVIDER",
    )
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.6-sol", validation_alias="OPENAI_MODEL")
    openai_red_team_model: str | None = Field(
        default=None,
        validation_alias="OPENAI_RED_TEAM_MODEL",
    )
    deepseek_api_key: str | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias="DEEPSEEK_MODEL",
    )
    deepseek_red_team_model: str | None = Field(
        default=None,
        validation_alias="DEEPSEEK_RED_TEAM_MODEL",
    )
    llm_timeout_seconds: float = Field(default=60.0, gt=0, validation_alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=2, ge=0, le=10, validation_alias="LLM_MAX_RETRIES")
    analysis_prompt_path: Path = BACKEND_DIR / "app" / "prompts" / "analysis_v1.txt"
    red_team_prompt_path: Path = BACKEND_DIR / "app" / "prompts" / "red_team_v1.txt"

    model_config = SettingsConfigDict(
        env_prefix="AIM_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )


settings = Settings()
