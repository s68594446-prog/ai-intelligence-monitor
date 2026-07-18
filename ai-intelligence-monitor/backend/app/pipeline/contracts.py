from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.core.enums import (
    Adjudication,
    Direction,
    FactType,
    MissingMarker,
    RedTeamAction,
    SignalLevel,
    SourceType,
    TrustTier,
    ThesisDirection,
    ValueKind,
    VerificationStatus,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceInput(StrictModel):
    key: str
    name: str
    source_type: SourceType
    trust_tier: TrustTier
    url: str


class FactInput(StrictModel):
    statement: str
    fact_type: FactType
    value_kind: ValueKind = ValueKind.TEXT
    value: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    missing_marker: MissingMarker | None = None

    @model_validator(mode="after")
    def number_has_value(self):
        if self.value_kind == ValueKind.NUMBER and self.value is None and self.missing_marker is None:
            raise ValueError("numeric fact requires value or an explicit missing marker")
        return self


class AnalysisDirective(StrictModel):
    affected_variable: str
    direction: Direction
    novelty: Annotated[int, Field(ge=0, le=100)]
    materiality: Annotated[int, Field(ge=0, le=100)]
    source_quality: Annotated[int, Field(ge=0, le=100)]
    signal_level: SignalLevel
    facts: list[FactInput]
    marginal_change: str
    thesis_impact: str
    unknowns: list[str] = Field(default_factory=list)
    next_validation: str
    adjudication: Adjudication
    out_of_scope: bool = False


class EvidenceReference(StrictModel):
    reference_id: str
    quote: str = Field(min_length=1)


class ExtractedClaim(StrictModel):
    statement: str = Field(min_length=1)
    fact_type: FactType
    value_kind: ValueKind = ValueKind.TEXT
    value: str | None = None
    evidence_references: list[EvidenceReference] = Field(default_factory=list)
    missing_marker: MissingMarker | None = None


class LLMAnalysisContract(StrictModel):
    coverage_match: bool
    affected_variables: list[str]
    extracted_claims: list[ExtractedClaim]
    novelty: Annotated[int, Field(ge=0, le=100)]
    marginal_change: str
    thesis_direction: ThesisDirection
    affected_assumptions: list[str]
    materiality: Annotated[int, Field(ge=0, le=100)]
    proposed_signal_level: SignalLevel
    unknowns: list[str]
    next_validation_points: list[str]
    reasoning_summary: str

    @model_validator(mode="after")
    def enforce_coverage_mapping(self):
        if self.coverage_match and not self.affected_variables:
            raise ValueError("coverage_match=true requires at least one affected variable")
        if not self.coverage_match and self.proposed_signal_level not in {SignalLevel.S0, SignalLevel.S1}:
            raise ValueError("out-of-scope content cannot propose S2 or S3")
        return self


class RedTeamDirective(StrictModel):
    action: RedTeamAction
    resulting_level: SignalLevel
    assumption_at_risk: str
    alternative_explanation: str
    confirmation_bias: str
    market_knowledge: str
    cannot_prove: str
    next_validation: str
    worst_position_risk: str


class LLMRedTeamContract(StrictModel):
    action: RedTeamAction
    challenged_assumptions: list[str]
    alternative_explanations: list[str]
    confirmation_bias_risk: str
    priced_in_assessment: str
    non_conclusions: list[str]
    validation_points: list[str]
    downside_case: str
    recommended_signal_level: SignalLevel
    confidence: Annotated[int, Field(ge=0, le=100)]


class DocumentInput(StrictModel):
    fixture_id: str
    source: SourceInput
    title: str
    url: str
    published_at: datetime
    language: str = "en"
    raw_text: str
    fact_type: FactType
    verification_status: VerificationStatus
    event_key: str
    event_summary: str
    conflicted: bool = False
    analysis: AnalysisDirective
    red_team: RedTeamDirective | None = None
    optional_notes: str | None = None

    @field_validator("url")
    @classmethod
    def validate_optional_url(cls, value: str) -> str:
        if value:
            HttpUrl(value)
        return value


class FixtureScenario(StrictModel):
    scenario: str
    description: str
    documents: list[DocumentInput]


class CollectedBatch(StrictModel):
    adapter_name: str
    adapter_version: str
    scenario: str
    documents: list[DocumentInput]
    persisted_document_ids: dict[str, int] = Field(default_factory=dict)


class AnalysisResult(AnalysisDirective):
    adapter_name: str
    adapter_version: str
    research_contract: LLMAnalysisContract | None = None
    prompt_version: str | None = None
    model_name: str | None = None
    schema_validated: bool = True


class RedTeamResult(RedTeamDirective):
    adapter_name: str
    adapter_version: str
    research_contract: LLMRedTeamContract | None = None
    prompt_version: str | None = None
    model_name: str | None = None
    schema_validated: bool = True
