from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.source_collections.contracts import SourceRole


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassifiedTier(StrEnum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    UNVERIFIED = "UNVERIFIED"


class DiscoveryStatus(StrEnum):
    PLANNED = "PLANNED"
    PASSED = "PASSED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    FAILED_OPS_CHECK = "FAILED_OPS_CHECK"


class CoverageRelevance(StrEnum):
    IN_SCOPE = "IN_SCOPE"
    POSSIBLE = "POSSIBLE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class FetchStatus(StrEnum):
    SUCCESS = "SUCCESS"
    BLOCKED = "BLOCKED"
    HTTP_ERROR = "HTTP_ERROR"
    INVALID_CONTENT = "INVALID_CONTENT"
    TOO_LARGE = "TOO_LARGE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


class QueryTemplate(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    variable: str
    purpose: str
    language: Literal["en", "zh"]
    time_range: str
    query: str = Field(min_length=8, max_length=500)


class QueryPlanConfig(StrictModel):
    plan_version: str
    coverage_id: str
    default_time_range: str
    queries: list[QueryTemplate] = Field(min_length=1)


class SearchQuery(StrictModel):
    query_id: str
    text: str
    variable: str
    purpose: str
    language: Literal["en", "zh"]
    time_range: str
    plan_version: str


class ProviderSearchHit(StrictModel):
    title: str
    url: str
    snippet: str = ""
    rank: int = Field(ge=1)
    published_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    provider_score: float | None = None
    source_metadata: dict = Field(default_factory=dict)
    retrieval_status: str = "SUCCESS"


class ProviderSearchPage(StrictModel):
    provider: str
    adapter_version: str
    query_id: str
    endpoint: str
    request_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    provider_request_id: str | None = None
    query_text_hash: str | None = None
    raw_response_sha256: str | None = None
    retrieval_status: str = "SUCCESS"
    fallback_from: str | None = None
    request_metadata: dict = Field(default_factory=dict)
    results: list[ProviderSearchHit]
    raw_response: dict


class NormalizedSearchResult(StrictModel):
    candidate_id: str
    canonical_url: str
    title: str
    snippet: str
    query_ids: list[str] = Field(min_length=1)
    best_rank: int = Field(ge=1)
    source_tier: ClassifiedTier
    source_policy_version: str
    source_domain: str
    primary_parent_id: str | None = None
    provider_provenance: list[dict] = Field(default_factory=list)


class DiscoveryTriageContract(StrictModel):
    candidate_id: str
    coverage_relevance: CoverageRelevance
    should_fetch: bool
    affected_variables: list[str]
    source_role: SourceRole
    primary_source_needed: bool
    fetch_priority: int = Field(ge=0, le=100)
    validation_questions: list[str]
    primary_search_terms: list[str] = Field(default_factory=list, max_length=3)
    source_type_suggestion: str
    snippet_is_evidence: Literal[False] = False

    @model_validator(mode="after")
    def bound_primary_search(self):
        if self.primary_source_needed and not self.primary_search_terms:
            raise ValueError("primary_source_needed requires bounded search terms")
        if any(not term.strip() or len(term) > 200 or "\n" in term or "\r" in term for term in self.primary_search_terms):
            raise ValueError("primary source search terms must be single-line and bounded")
        if self.coverage_relevance == CoverageRelevance.OUT_OF_SCOPE and self.should_fetch:
            raise ValueError("out-of-scope result cannot be fetched")
        return self


class TriageResult(StrictModel):
    candidate_id: str
    contract: DiscoveryTriageContract
    adapter_name: str
    adapter_version: str
    prompt_version: str
    model_name: str
    schema_validated: bool = True
    authorized_payload_keys: list[str]


class WebFetchResult(StrictModel):
    candidate_id: str
    canonical_url: str
    final_url: str
    status: FetchStatus
    http_status: int | None = None
    content_type: str | None = None
    response_bytes: int = 0
    response_sha256: str | None = None
    title: str
    language: str = ""
    published_at: datetime | None = None
    raw_text: str = ""
    content_hash: str | None = None
    request_count: int = 0
    retry_count: int = 0
    redirect_count: int = 0
    security_checks: dict[str, bool] = Field(default_factory=dict)
    error_code: str | None = None


class DocumentDecision(StrictModel):
    candidate_id: str
    decision: Literal["CREATED", "DUPLICATE", "FILTERED", "BLOCKED", "UNVERIFIED"]
    reason: str
    canonical_url: str
    source_tier: ClassifiedTier
    source_role: SourceRole
    query_ids: list[str]
    event_cluster_key: str
    document_id: int | None = None
    content_hash: str | None = None
    analysis_run_id: str | None = None
    source_chain_ids: list[str] = Field(default_factory=list)


class DiscoveryRequest(StrictModel):
    coverage_id: Literal["dram_hbm"] = "dram_hbm"
    search: bool = False
    news: bool = False
    fetch: bool = False
    triage: bool = False
    analyze: bool = False

    @model_validator(mode="after")
    def require_explicit_predecessors(self):
        discovery_requested = self.search or self.news
        if self.fetch and not (discovery_requested and self.triage):
            raise ValueError("fetch requires explicit search or news plus triage")
        if self.triage and not discovery_requested:
            raise ValueError("triage requires explicit search or news")
        if self.analyze and not (discovery_requested and self.triage and self.fetch):
            raise ValueError("analyze requires explicit discovery, triage, and fetch")
        return self


class DiscoveryResponse(StrictModel):
    run_id: str
    status: DiscoveryStatus
    plan_count: int
    search_result_count: int
    triage_count: int
    fetched_count: int
    document_count: int
    duplicate_count: int
    analyzed_count: int
    blocked_count: int
    artifact_paths: list[str]
    ops_check: dict


class LiveSearchReplayDiff(StrictModel):
    added_urls: list[str]
    disappeared_urls: list[str]
    rank_changes: list[dict]
    content_hash_changes: list[dict]
    source_tier_changes: list[dict]
    document_decision_changes: list[dict]
