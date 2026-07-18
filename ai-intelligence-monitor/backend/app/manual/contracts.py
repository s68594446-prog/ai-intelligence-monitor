from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.core.enums import Adjudication, PipelineState, TrustTier


class ManualStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualDocumentCreate(ManualStrictModel):
    title: str = Field(min_length=3, max_length=500)
    url: HttpUrl | None = None
    source_name: str = Field(min_length=2, max_length=240)
    source_tier: TrustTier
    published_at: datetime
    language: str = Field(default="en", min_length=2, max_length=20)
    raw_text: str = Field(min_length=40)
    optional_notes: str | None = Field(default=None, max_length=4000)

    @field_validator("published_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        return value

    @field_validator("raw_text")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if len(value.strip()) < 40:
            raise ValueError("raw_text must contain at least 40 non-whitespace characters")
        return value


class ManualDocumentCreated(ManualStrictModel):
    document_id: int
    ingestion_id: str
    current_status: PipelineState
    created_at: datetime
    duplicate: bool = False


class ManualAnalysisResponse(ManualStrictModel):
    run_id: str
    final_status: str
    event_id: int | None
    signal_id: int | None
    adjudication: Adjudication | None
    artifact_paths: list[str]
    ops_check_status: bool

