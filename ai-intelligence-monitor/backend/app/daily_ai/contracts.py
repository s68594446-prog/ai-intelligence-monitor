from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StableStatus(StrEnum):
    SUCCESS = "SUCCESS"
    MISSING_API_KEY = "MISSING_API_KEY"
    RATE_LIMITED = "RATE_LIMITED"
    USAGE_LIMIT_REACHED = "USAGE_LIMIT_REACHED"
    API_NOT_ACTIVATED = "API_NOT_ACTIVATED"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    KEY_EXPIRED = "KEY_EXPIRED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    MAINTENANCE = "MAINTENANCE"
    ENDPOINT_DISABLED = "ENDPOINT_DISABLED"


class TianNewsItem(StrictModel):
    provider_id: str
    title: str
    url: HttpUrl
    description: str = ""
    published_at: str | None = None
    source: str = ""
    title_policy: Literal["DISCOVERY_ONLY"] = "DISCOVERY_ONLY"
    description_policy: Literal["DISCOVERY_ONLY"] = "DISCOVERY_ONLY"
    eligible_as_evidence: Literal[False] = False


class TianApiPage(StrictModel):
    provider: Literal["tianapi_ai"] = "tianapi_ai"
    provider_version: Literal["tianapi-ai-v1"] = "tianapi-ai-v1"
    status: StableStatus
    request_count: int = Field(ge=0, le=1)
    page: Literal[1] = 1
    num: int = Field(ge=1, le=5)
    result_count: int = Field(ge=0, le=5)
    remaining_quota_status: Literal["AVAILABLE", "UNAVAILABLE"] = "UNAVAILABLE"
    raw_response_sha256: str | None = None
    items: list[TianNewsItem] = Field(max_length=5)


class UniverseStatus(StrEnum):
    VERIFIED = "VERIFIED"
    STALE_UNIVERSE = "STALE_UNIVERSE"


class AShareConstituent(StrictModel):
    security_code: str = Field(pattern=r"^\d{6}$")
    company_name: str = Field(min_length=1, max_length=100)
    exchange: Literal["SSE", "SZSE", "BSE"]
    index_codes: list[Literal["931071", "950180"]] = Field(min_length=1)
    effective_date: str


class UniverseSource(StrictModel):
    index_code: Literal["931071", "950180"]
    url: HttpUrl
    retrieved_at: datetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AShareUniverse(StrictModel):
    version: Literal["a-share-ai-universe-v1"] = "a-share-ai-universe-v1"
    status: UniverseStatus
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sources: list[UniverseSource] = Field(min_length=2, max_length=2)
    constituents: list[AShareConstituent]


class EvidenceQuote(StrictModel):
    quote: str = Field(min_length=1, max_length=1000)
    source_url: HttpUrl


class AShareAnalysisContract(StrictModel):
    company_name: str
    security_code: str = Field(pattern=r"^\d{6}$")
    exchange: Literal["SSE", "SZSE", "BSE"]
    announcement_type: str
    announced_at: str
    ai_relevance: Literal["DIRECT", "INDIRECT", "CONCEPT_ONLY", "NONE"]
    what_happened: str
    contains_verifiable_numbers: bool
    direct_business_impact: str
    china_ai_chain_impact: str
    novelty: Literal["NEW_FACT", "REPEATED_STATEMENT", "UNKNOWN"]
    concept_only: bool
    risks_and_counterevidence: list[str]
    follow_up_points: list[str]
    evidence: list[EvidenceQuote] = Field(min_length=1)
    uncertainties: list[str]
    importance_score: int = Field(ge=0, le=100)
    importance_reason: str
    investment_advice: Literal[False] = False

    @model_validator(mode="after")
    def reject_concept_as_important(self):
        if (self.concept_only or self.ai_relevance in {"CONCEPT_ONLY", "NONE"}) and self.importance_score >= 60:
            raise ValueError("concept-only or unrelated disclosures cannot be important")
        return self


class PdfExtraction(StrictModel):
    status: Literal["SUCCESS", "BLOCKED_PDF"]
    source_url: HttpUrl
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=0)
    text: str = ""
    error_code: str | None = None


class AShareOpsResult(StrictModel):
    version: Literal["a-share-ai-ops-v1"] = "a-share-ai-ops-v1"
    passed: bool
    checks: dict[str, bool]
    errors: list[str]
