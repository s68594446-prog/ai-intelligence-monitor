from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.core.enums import FetchStatus, SourceType, TrustTier


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FixedSourceConfig(StrictModel):
    key: str = Field(pattern=r"^[a-z0-9_]+$", max_length=80)
    name: str = Field(min_length=2, max_length=240)
    active: bool = True
    trust_tier: TrustTier
    source_type: SourceType
    mode: Literal["rss_full", "micron_ir_html"]
    index_url: HttpUrl
    allowed_hosts: list[str] = Field(min_length=1)
    allowed_path_prefixes: list[str] = Field(min_length=1)
    language: str = Field(default="en", min_length=2, max_length=20)
    required_categories: list[str] = Field(default_factory=list)
    discovery_terms: list[str] = Field(min_length=1)
    max_candidates: int = Field(default=50, ge=1, le=100)
    default_limit: int = Field(default=5, ge=1, le=10)
    timezone: str = "UTC"

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower().rstrip(".") for item in value]
        if any(not item or ":" in item or "/" in item for item in normalized):
            raise ValueError("allowed_hosts must contain hostnames only")
        return normalized

    @field_validator("allowed_path_prefixes")
    @classmethod
    def validate_prefixes(cls, value: list[str]) -> list[str]:
        if any(not item.startswith("/") or ".." in item for item in value):
            raise ValueError("allowed path prefixes must be absolute paths without traversal")
        return value


class OfficialSourceRegistry(StrictModel):
    version: str
    sources: list[FixedSourceConfig] = Field(min_length=1)


class FetchCandidate(StrictModel):
    title: str = Field(min_length=3, max_length=500)
    url: HttpUrl
    published_at: datetime
    language: str
    raw_text: str = Field(min_length=40)
    response_sha256: str = Field(min_length=64, max_length=64)
    response_bytes: int = Field(ge=1)
    http_status: int


class CollectorResult(StrictModel):
    status: FetchStatus
    request_url: str
    http_status: int
    etag: str | None = None
    last_modified: str | None = None
    response_sha256: str | None = None
    response_bytes: int = 0
    candidates: list[FetchCandidate] = Field(default_factory=list)
    item_results: list[dict] = Field(default_factory=list)


class FixedSourceSummary(StrictModel):
    key: str
    name: str
    trust_tier: TrustTier
    source_type: SourceType
    mode: str
    index_url: str
    active: bool
    default_limit: int


class FixedFetchResponse(StrictModel):
    fetch_id: str
    source_key: str
    status: FetchStatus
    candidates_found: int
    documents_created: int
    duplicates_found: int
    document_ids: list[int]
    duplicate_document_ids: list[int]
    artifact_path: str | None
    errors: list[str]
