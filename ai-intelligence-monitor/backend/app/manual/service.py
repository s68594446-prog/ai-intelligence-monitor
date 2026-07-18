import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    FactType,
    PipelineState,
    SourceType,
    TrustTier,
    VerificationStatus,
)
from app.core.settings import Settings, settings
from app.coverage.service import ensure_coverage
from app.harness.runner import Harness
from app.manual.collector import ManualCollector, manual_snapshot
from app.manual.contracts import ManualAnalysisResponse, ManualDocumentCreate, ManualDocumentCreated
from app.models import Document, Event, RunAudit, Signal, Source
from app.pipeline.adapters import AnalysisAdapter, RedTeamAdapter
from app.pipeline.provider import build_live_adapters
from app.prompts import ANALYSIS_PROMPT_VERSION, RED_TEAM_PROMPT_VERSION


def _source_type(tier: TrustTier) -> SourceType:
    return {
        TrustTier.T1: SourceType.COMPANY,
        TrustTier.T2: SourceType.INDUSTRY_RESEARCH,
        TrustTier.T3: SourceType.FORUM,
    }[tier]


def submit_manual_document(
    session: Session,
    payload: ManualDocumentCreate,
) -> ManualDocumentCreated:
    content_hash = hashlib.sha256(payload.raw_text.encode("utf-8")).hexdigest()
    existing = session.scalar(
        select(Document)
        .where(
            Document.content_hash == content_hash,
            Document.ingestion_id.is_not(None),
        )
        .order_by(Document.created_at.asc())
    )
    if existing is not None:
        return ManualDocumentCreated(
            document_id=existing.id,
            ingestion_id=existing.ingestion_id,
            current_status=existing.pipeline_state,
            created_at=existing.created_at,
            duplicate=True,
        )
    source = Source(
        name=payload.source_name,
        source_type=_source_type(payload.source_tier),
        trust_tier=payload.source_tier,
        url=str(payload.url) if payload.url else "",
        active=True,
    )
    session.add(source)
    session.flush()
    ingestion_id = "manual-" + uuid4().hex
    verification_status = (
        VerificationStatus.MONITORED_NOT_TRUSTED
        if payload.source_tier == TrustTier.T3
        else VerificationStatus.UNVERIFIED
    )
    document = Document(
        run_id=ingestion_id,
        ingestion_id=ingestion_id,
        source_id=source.id,
        title=payload.title,
        url=str(payload.url) if payload.url else "",
        published_at=payload.published_at.astimezone(timezone.utc),
        fetched_at=datetime.now(timezone.utc),
        language=payload.language,
        raw_text=payload.raw_text,
        content_hash=content_hash,
        fact_type=FactType.UNKNOWN,
        verification_status=verification_status,
        pipeline_state=PipelineState.COLLECTED,
        event_key_hint=f"manual-{content_hash[:24]}",
        fixture_payload={},
        optional_notes=payload.optional_notes,
    )
    session.add(document)
    session.commit()
    return ManualDocumentCreated(
        document_id=document.id,
        ingestion_id=ingestion_id,
        current_status=document.pipeline_state,
        created_at=document.created_at,
        duplicate=False,
    )


def build_manual_harness(
    session: Session,
    document_id: int,
    app_settings: Settings = settings,
    *,
    analysis_adapter: AnalysisAdapter | None = None,
    red_team_adapter: RedTeamAdapter | None = None,
) -> tuple[Harness, dict]:
    topic, thesis = ensure_coverage(session, app_settings.coverage_path)
    thesis_snapshot = {
        "statement": thesis.statement,
        "assumptions": thesis.assumptions,
        "disconfirming_conditions": thesis.disconfirming_conditions,
        "reverse_triggers": thesis.reverse_triggers,
        "version": thesis.version,
    }
    if analysis_adapter is None or red_team_adapter is None:
        default_analysis, default_red_team = build_live_adapters(
            app_settings,
            topic.config_snapshot,
            thesis_snapshot,
        )
    analysis = analysis_adapter or default_analysis
    red_team = red_team_adapter or default_red_team
    harness = Harness(
        session=session,
        runs_dir=app_settings.runs_dir,
        coverage_path=app_settings.coverage_path,
        collector=ManualCollector(session, document_id),
        analysis=analysis,
        red_team=red_team,
        prompt_version=f"{ANALYSIS_PROMPT_VERSION}+{RED_TEAM_PROMPT_VERSION}",
        analysis_prompt_version=getattr(analysis, "prompt_version", ANALYSIS_PROMPT_VERSION),
        red_team_prompt_version=getattr(red_team, "prompt_version", RED_TEAM_PROMPT_VERSION),
        model_name=getattr(analysis, "model", None),
        red_team_model_name=getattr(
            red_team,
            "model",
            None,
        ),
    )
    return harness, topic.config_snapshot


def analyze_manual_document(
    session: Session,
    document_id: int,
    app_settings: Settings = settings,
    *,
    analysis_adapter: AnalysisAdapter | None = None,
    red_team_adapter: RedTeamAdapter | None = None,
) -> ManualAnalysisResponse:
    document = session.get(Document, document_id)
    if document is None or document.ingestion_id is None:
        raise ValueError(f"manual document not found: {document_id}")
    existing_run = session.scalar(
        select(RunAudit).where(
            RunAudit.run_id == document.run_id,
            RunAudit.run_kind == "manual",
        )
    )
    if existing_run is not None:
        return _analysis_response(session, existing_run)
    harness, coverage_snapshot = build_manual_harness(
        session,
        document_id,
        app_settings,
        analysis_adapter=analysis_adapter,
        red_team_adapter=red_team_adapter,
    )
    snapshot = manual_snapshot(document)
    run = harness.run(
        f"manual:{document_id}",
        coverage_snapshot=coverage_snapshot,
        run_kind="manual",
        replay_mode="LIVE_MODEL",
        manual_input_snapshot=snapshot,
    )
    if app_settings.email_auto_send:
        from app.delivery.coordinator import coordinate_post_ops_email_delivery

        coordinate_post_ops_email_delivery(
            session,
            run.run_id,
            app_settings=app_settings,
        )
    return _analysis_response(session, run)


def _analysis_response(session: Session, run: RunAudit) -> ManualAnalysisResponse:
    event = session.scalar(select(Event).where(Event.run_id == run.run_id))
    signal = session.scalar(select(Signal).where(Signal.run_id == run.run_id))
    return ManualAnalysisResponse(
        run_id=run.run_id,
        final_status=run.status.value,
        event_id=event.id if event else None,
        signal_id=signal.id if signal else None,
        adjudication=signal.adjudication if signal else None,
        artifact_paths=run.artifact_paths,
        ops_check_status=bool(run.ops_check and run.ops_check.get("passed")),
    )
