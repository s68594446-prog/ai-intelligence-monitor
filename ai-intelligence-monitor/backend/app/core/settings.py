from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    web_search_plan_path: Path = PROJECT_DIR / "config" / "discovery" / "dram_hbm_web_search.yaml"
    web_source_policy_path: Path = PROJECT_DIR / "config" / "discovery" / "web_source_policy.yaml"
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
    web_discovery_enabled: bool = Field(default=False, validation_alias="WEB_DISCOVERY_ENABLED")
    web_search_provider: Literal["brave", "searxng", "tavily", "exa", "you", "gemini_google"] = Field(
        default="searxng", validation_alias="WEB_SEARCH_PROVIDER"
    )
    web_search_fallbacks: str = Field(default="tavily,exa,you,gemini_google", validation_alias="WEB_SEARCH_FALLBACKS")
    web_search_max_fallbacks: int = Field(default=1, ge=0, le=1, validation_alias="WEB_SEARCH_MAX_FALLBACKS")
    brave_search_api_key: SecretStr | None = Field(
        default=None, validation_alias="BRAVE_SEARCH_API_KEY"
    )
    brave_search_base_url: str = Field(
        default="https://api.search.brave.com/res/v1/web/search",
        validation_alias="BRAVE_SEARCH_BASE_URL",
    )
    tavily_api_key: SecretStr | None = Field(default=None, validation_alias="TAVILY_API_KEY")
    exa_api_key: SecretStr | None = Field(default=None, validation_alias="EXA_API_KEY")
    you_api_key: SecretStr | None = Field(default=None, validation_alias="YOU_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    gemini_search_model: str = Field(default="gemini-2.5-flash", validation_alias="GEMINI_SEARCH_MODEL")
    searxng_image_digest: str = Field(
        default="UNVERIFIED",
        pattern=r"^(UNVERIFIED|sha256:[0-9a-f]{64})$",
        validation_alias="SEARXNG_IMAGE_DIGEST",
    )
    news_discovery_enabled: bool = Field(
        default=False, validation_alias="NEWS_DISCOVERY_ENABLED"
    )
    news_search_provider: Literal["thenewsapi"] = Field(
        default="thenewsapi", validation_alias="NEWS_SEARCH_PROVIDER"
    )
    news_search_max_queries: int = Field(
        default=2, ge=1, le=2, validation_alias="NEWS_SEARCH_MAX_QUERIES"
    )
    news_search_max_results_per_query: int = Field(
        default=3, ge=1, le=3,
        validation_alias="NEWS_SEARCH_MAX_RESULTS_PER_QUERY",
    )
    news_search_lookback_days: int = Field(
        default=7, ge=1, le=30, validation_alias="NEWS_SEARCH_LOOKBACK_DAYS"
    )
    thenewsapi_key: SecretStr | None = Field(
        default=None, validation_alias="THENEWSAPI_KEY"
    )
    tianapi_enabled: bool = Field(default=False, validation_alias="TIANAPI_ENABLED")
    tianapi_api_key: SecretStr | None = Field(default=None, validation_alias="TIANAPI_API_KEY")
    juhe_headlines_enabled: bool = Field(default=False, validation_alias="JUHE_HEADLINES_ENABLED")
    juhe_headlines_api_key: SecretStr | None = Field(default=None, validation_alias="JUHE_HEADLINES_API_KEY")
    juhe_ai_brief_enabled: bool = Field(default=False, validation_alias="JUHE_AI_BRIEF_ENABLED")
    juhe_ai_brief_api_key: SecretStr | None = Field(default=None, validation_alias="JUHE_AI_BRIEF_API_KEY")
    juhe_stock_enabled: bool = Field(default=False, validation_alias="JUHE_STOCK_ENABLED")
    web_search_timeout_seconds: float = Field(
        default=20.0, gt=0, le=120, validation_alias="WEB_SEARCH_TIMEOUT_SECONDS"
    )
    web_search_max_retries: int = Field(
        default=2, ge=0, le=2, validation_alias="WEB_SEARCH_MAX_RETRIES"
    )
    web_search_max_queries_per_run: int = Field(
        default=24, ge=1, le=50, validation_alias="WEB_SEARCH_MAX_QUERIES_PER_RUN"
    )
    web_search_max_results_per_query: int = Field(
        default=5, ge=1, le=20, validation_alias="WEB_SEARCH_MAX_RESULTS_PER_QUERY"
    )
    web_fetch_max_pages_per_run: int = Field(
        default=10, ge=0, le=30, validation_alias="WEB_FETCH_MAX_PAGES_PER_RUN"
    )
    web_triage_max_candidates_per_run: int = Field(
        default=20, ge=1, le=30,
        validation_alias="WEB_TRIAGE_MAX_CANDIDATES_PER_RUN",
    )
    web_analyze_max_documents_per_run: int = Field(
        default=3, ge=0, le=5, validation_alias="WEB_ANALYZE_MAX_DOCUMENTS_PER_RUN"
    )
    email_delivery_enabled: bool = Field(default=False, validation_alias="EMAIL_DELIVERY_ENABLED")
    email_auto_send: bool = Field(default=False, validation_alias="EMAIL_AUTO_SEND")
    smtp_host: str | None = Field(default=None, validation_alias="SMTP_HOST")
    smtp_port: int = Field(default=587, ge=1, le=65535, validation_alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, validation_alias="SMTP_USERNAME")
    smtp_password: SecretStr | None = Field(default=None, validation_alias="SMTP_PASSWORD")
    smtp_from_address: str | None = Field(default=None, validation_alias="SMTP_FROM_ADDRESS")
    smtp_to_addresses: str = Field(default="", validation_alias="SMTP_TO_ADDRESSES")
    smtp_security: Literal["STARTTLS", "SSL"] = Field(
        default="STARTTLS", validation_alias="SMTP_SECURITY"
    )
    smtp_timeout_seconds: float = Field(
        default=20.0, gt=0, le=120, validation_alias="SMTP_TIMEOUT_SECONDS"
    )
    smtp_message_id_domain: str | None = Field(
        default=None, validation_alias="SMTP_MESSAGE_ID_DOMAIN"
    )
    analysis_prompt_path: Path = BACKEND_DIR / "app" / "prompts" / "analysis_v1.txt"
    red_team_prompt_path: Path = BACKEND_DIR / "app" / "prompts" / "red_team_v1.txt"
    discovery_triage_prompt_path: Path = (
        BACKEND_DIR / "app" / "prompts" / "discovery_triage_v1.txt"
    )

    @field_validator(
        "smtp_password", "brave_search_api_key", "tavily_api_key", "exa_api_key",
        "you_api_key", "gemini_api_key", "thenewsapi_key", "tianapi_api_key",
        "juhe_headlines_api_key", "juhe_ai_brief_api_key", mode="before"
    )
    @classmethod
    def normalize_empty_smtp_password(cls, value):
        if value is None or value == "":
            return None
        return value

    model_config = SettingsConfigDict(
        env_prefix="AIM_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )


settings = Settings()
