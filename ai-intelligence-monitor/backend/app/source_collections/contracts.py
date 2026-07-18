from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.core.enums import TrustTier


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRole(StrEnum):
    DIRECT = "DIRECT"
    DEPENDENCY_SENTINEL = "DEPENDENCY_SENTINEL"


class DiscoveryMethod(StrEnum):
    RSS = "RSS"
    HTML_INDEX = "HTML_INDEX"


class CandidateStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    FILTERED_OUT = "FILTERED_OUT"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"


class ExtractionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    METADATA_ONLY = "METADATA_ONLY"
    TOO_SHORT = "TOO_SHORT"
    BLOCKED = "BLOCKED"
    HTTP_ERROR = "HTTP_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    DISALLOWED_REDIRECT = "DISALLOWED_REDIRECT"


class ApprovedSource(StrictModel):
    source_id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    enabled: bool
    source_tier: TrustTier
    role: SourceRole
    discovery_method: DiscoveryMethod
    index_url: HttpUrl | None = None
    feed_url: HttpUrl | None = None
    allowed_domains: list[str] = Field(min_length=1)
    article_url_patterns: list[str] = Field(min_length=1)
    body_selectors: list[str] = Field(default_factory=lambda: ["article", "main"])
    language: str
    max_items_per_run: int = Field(ge=1, le=10)
    request_timeout_seconds: float = Field(gt=0, le=120)
    minimum_interval_seconds: float = Field(ge=1, le=60)
    relevance_terms: list[str] = Field(min_length=1)
    exclusion_terms: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def require_discovery_url(self):
        if self.discovery_method == DiscoveryMethod.RSS and not self.feed_url:
            raise ValueError("RSS source requires feed_url")
        if self.discovery_method == DiscoveryMethod.HTML_INDEX and not self.index_url:
            raise ValueError("HTML_INDEX source requires index_url")
        return self

    @property
    def discovery_url(self) -> str:
        return str(self.feed_url or self.index_url)


class SourceRegistry(StrictModel):
    registry_version: str
    coverage_id: str
    sources: list[ApprovedSource] = Field(min_length=1)


class DiscoveredItem(StrictModel):
    source_id: str
    title: str
    canonical_url: str
    published_at: datetime | None
    summary: str = ""
    discovered_at: datetime
    discovery_method: DiscoveryMethod


class PrefilterResult(StrictModel):
    canonical_url: str
    candidate_status: CandidateStatus
    matched_terms: list[str]
    excluded_terms: list[str]
    match_reason: str


class FetchResult(StrictModel):
    source_id: str
    canonical_url: str
    final_url: str
    http_status: int | None
    fetched_at: datetime
    title: str
    published_at: datetime | None
    language: str
    raw_text: str = ""
    content_hash: str | None = None
    extraction_status: ExtractionStatus
    response_metadata: dict = Field(default_factory=dict)


class CollectionRequest(StrictModel):
    coverage_id: str = "dram_hbm"
    source_ids: list[str] = Field(min_length=1)
    since_hours: int = Field(default=168, ge=1, le=24 * 365)
    max_items_per_source: int = Field(default=5, ge=1, le=10)
    analyze: bool = False
    dry_run: bool = False


class CollectionResponse(StrictModel):
    run_id: str
    status: str
    sources_requested: list[str]
    discovered_count: int
    candidate_count: int
    fetched_count: int
    document_count: int
    duplicate_count: int
    filtered_count: int
    error_count: int
    artifact_path: str
    analyzed_document_ids: list[int] = Field(default_factory=list)
