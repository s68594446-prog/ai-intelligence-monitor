from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, false
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.enums import (
    Adjudication,
    DeliveryStatus,
    Direction,
    FactType,
    FetchStatus,
    PipelineState,
    RunStatus,
    SignalLevel,
    SourceType,
    ThesisStatus,
    TrustTier,
    VerificationStatus,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enum_column(enum_type):
    return SAEnum(enum_type, native_enum=False, validate_strings=True)


class Base(DeclarativeBase):
    pass


class CoverageTopic(Base):
    __tablename__ = "coverage_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coverage_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(40))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    theses: Mapped[list["Thesis"]] = relationship(back_populates="coverage_topic")


class Thesis(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coverage_topic_id: Mapped[int] = mapped_column(ForeignKey("coverage_topics.id"))
    statement: Mapped[str] = mapped_column(Text)
    assumptions: Mapped[list[str]] = mapped_column(JSON)
    disconfirming_conditions: Mapped[list[str]] = mapped_column(JSON)
    reverse_triggers: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    version: Mapped[str] = mapped_column(String(40))
    status: Mapped[ThesisStatus] = mapped_column(enum_column(ThesisStatus), default=ThesisStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    coverage_topic: Mapped[CoverageTopic] = relationship(back_populates="theses")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(240))
    source_type: Mapped[SourceType] = mapped_column(enum_column(SourceType))
    trust_tier: Mapped[TrustTier] = mapped_column(enum_column(TrustTier))
    url: Mapped[str] = mapped_column(String(1000))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    language: Mapped[str] = mapped_column(String(20), default="en")
    raw_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    fact_type: Mapped[FactType] = mapped_column(enum_column(FactType))
    verification_status: Mapped[VerificationStatus] = mapped_column(enum_column(VerificationStatus))
    pipeline_state: Mapped[PipelineState] = mapped_column(enum_column(PipelineState))
    event_key_hint: Mapped[str] = mapped_column(String(200))
    fixture_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    ingestion_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    optional_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    source: Mapped[Source] = relationship()


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    canonical_title: Mapped[str] = mapped_column(String(500))
    event_key: Mapped[str] = mapped_column(String(200), index=True)
    summary: Mapped[str] = mapped_column(Text)
    document_ids: Mapped[list[int]] = mapped_column(JSON)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    pipeline_state: Mapped[PipelineState] = mapped_column(enum_column(PipelineState))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    coverage_topic_id: Mapped[int] = mapped_column(ForeignKey("coverage_topics.id"))
    affected_variable: Mapped[str] = mapped_column(String(200))
    thesis_version: Mapped[str] = mapped_column(String(40))
    direction: Mapped[Direction] = mapped_column(enum_column(Direction))
    novelty: Mapped[int] = mapped_column(Integer)
    materiality: Mapped[int] = mapped_column(Integer)
    source_quality: Mapped[int] = mapped_column(Integer)
    signal_level: Mapped[SignalLevel] = mapped_column(enum_column(SignalLevel))
    analysis_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    red_team_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    adjudication: Mapped[Adjudication | None] = mapped_column(enum_column(Adjudication), nullable=True)
    pipeline_state: Mapped[PipelineState] = mapped_column(enum_column(PipelineState))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeliveryFeedback(Base):
    __tablename__ = "delivery_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    delivery_status: Mapped[DeliveryStatus] = mapped_column(enum_column(DeliveryStatus))
    rendered_path: Mapped[str] = mapped_column(String(1000))
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RunAudit(Base):
    __tablename__ = "run_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    replay_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scenario: Mapped[str] = mapped_column(String(100))
    status: Mapped[RunStatus] = mapped_column(enum_column(RunStatus))
    coverage_id: Mapped[str] = mapped_column(String(80))
    coverage_version: Mapped[str] = mapped_column(String(40))
    coverage_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    thesis_version: Mapped[str] = mapped_column(String(40))
    prompt_version: Mapped[str] = mapped_column(String(80))
    model_adapter: Mapped[str] = mapped_column(String(200))
    adapter_versions: Mapped[dict[str, str]] = mapped_column(JSON)
    documents_processed: Mapped[int] = mapped_column(Integer, default=0)
    events_created: Mapped[int] = mapped_column(Integer, default=0)
    signals_created: Mapped[int] = mapped_column(Integer, default=0)
    alerts_selected: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    artifact_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    ops_check: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    run_kind: Mapped[str] = mapped_column(String(40), default="fixture", server_default="fixture")
    replay_mode: Mapped[str] = mapped_column(
        String(40),
        default="DETERMINISTIC",
        server_default="DETERMINISTIC",
    )
    manual_input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    analysis_prompt_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    red_team_prompt_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    red_team_model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    llm_schema_validated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StateTransitionAudit(Base):
    __tablename__ = "state_transition_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int] = mapped_column(Integer)
    from_state: Mapped[PipelineState] = mapped_column(enum_column(PipelineState))
    to_state: Mapped[PipelineState] = mapped_column(enum_column(PipelineState))
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SourceFetchAudit(Base):
    __tablename__ = "source_fetch_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetch_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_key: Mapped[str] = mapped_column(String(80), index=True)
    source_name: Mapped[str] = mapped_column(String(240))
    status: Mapped[FetchStatus] = mapped_column(enum_column(FetchStatus))
    request_url: Mapped[str] = mapped_column(String(1000))
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(500), nullable=True)
    response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_bytes: Mapped[int] = mapped_column(Integer, default=0)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0)
    documents_created: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_found: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    item_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    artifact_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
