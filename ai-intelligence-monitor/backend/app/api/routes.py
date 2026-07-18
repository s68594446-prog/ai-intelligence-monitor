from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.harness.service import build_harness, replay_run
from app.fixed_sources.collector import FixedSourceError, FixedSourceSecurityError
from app.fixed_sources.contracts import FixedFetchResponse, FixedSourceSummary
from app.fixed_sources.service import fetch_fixed_source, list_fixed_sources
from app.manual.contracts import ManualAnalysisResponse, ManualDocumentCreate, ManualDocumentCreated
from app.manual.replay import replay_manual_run
from app.manual.service import analyze_manual_document, submit_manual_document
from app.models import CoverageTopic, RunAudit, Signal, SourceFetchAudit
from app.pipeline.errors import (
    LLMAdapterTimeout,
    LLMConfigurationError,
    LLMOutputValidationError,
    LLMProviderError,
)
from app.source_collections.contracts import CollectionRequest, CollectionResponse
from app.source_collections.replay import replay_frozen_source
from app.source_collections.service import collect_sources
from app.source_collections.security import CollectionError


router = APIRouter()


def _run_payload(run: RunAudit) -> dict:
    return {
        "run_id": run.run_id,
        "replay_of": run.replay_of,
        "scenario": run.scenario,
        "status": run.status.value,
        "coverage_id": run.coverage_id,
        "coverage_version": run.coverage_version,
        "thesis_version": run.thesis_version,
        "prompt_version": run.prompt_version,
        "adapter_versions": run.adapter_versions,
        "documents_processed": run.documents_processed,
        "events_created": run.events_created,
        "signals_created": run.signals_created,
        "alerts_selected": run.alerts_selected,
        "errors": run.errors,
        "artifact_paths": run.artifact_paths,
        "ops_check": run.ops_check,
        "run_kind": run.run_kind,
        "replay_mode": run.replay_mode,
        "analysis_prompt_version": run.analysis_prompt_version,
        "red_team_prompt_version": run.red_team_prompt_version,
        "model_name": run.model_name,
        "red_team_model_name": run.red_team_model_name,
        "llm_schema_validated": run.llm_schema_validated,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "phase": "2",
        "network_collectors": True,
        "network_collection_mode": "approved-fixed-sources-manual-trigger-only",
        "llm": False,
        "manual_llm": True,
    }


@router.get("/api/coverage")
def coverage(session: Session = Depends(get_session)) -> list[dict]:
    topics = session.scalars(select(CoverageTopic).order_by(CoverageTopic.created_at.desc())).all()
    return [
        {
            "coverage_id": topic.coverage_id,
            "name": topic.name,
            "version": topic.version,
            "selected": topic.selected,
            "config_snapshot": topic.config_snapshot,
        }
        for topic in topics
    ]


@router.get("/api/runs")
def runs(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(RunAudit).order_by(RunAudit.started_at.desc())).all()
    return [_run_payload(row) for row in rows]


@router.get("/api/runs/{run_id}")
def run_detail(run_id: str, session: Session = Depends(get_session)) -> dict:
    run = session.scalar(select(RunAudit).where(RunAudit.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_payload(run)


@router.get("/api/signals")
def signals(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(Signal).order_by(Signal.created_at.desc())).all()
    return [
        {
            "id": row.id,
            "run_id": row.run_id,
            "event_id": row.event_id,
            "affected_variable": row.affected_variable,
            "thesis_version": row.thesis_version,
            "signal_level": row.signal_level.value,
            "pipeline_state": row.pipeline_state.value,
            "adjudication": row.adjudication.value if row.adjudication else None,
        }
        for row in rows
    ]


@router.post("/api/source-collections", response_model=CollectionResponse)
def create_source_collection(
    payload: CollectionRequest,
    session: Session = Depends(get_session),
) -> CollectionResponse:
    try:
        return collect_sources(session, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CollectionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/demo/run")
def demo_run(session: Session = Depends(get_session)) -> dict:
    return _run_payload(build_harness(session).run("verified_s3"))


@router.get("/api/fixed-sources", response_model=list[FixedSourceSummary])
def fixed_sources() -> list[FixedSourceSummary]:
    return list_fixed_sources()


@router.post("/api/fixed-sources/{source_key}/fetch", response_model=FixedFetchResponse)
def fetch_source(
    source_key: str,
    limit: int | None = Query(default=None, ge=1, le=10),
    session: Session = Depends(get_session),
) -> FixedFetchResponse:
    try:
        return fetch_fixed_source(session, source_key, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FixedSourceSecurityError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (FixedSourceError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/fetch-audits")
def fetch_audits(
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.scalars(
        select(SourceFetchAudit).order_by(SourceFetchAudit.started_at.desc()).limit(limit)
    ).all()
    return [
        {
            "fetch_id": row.fetch_id,
            "source_key": row.source_key,
            "source_name": row.source_name,
            "status": row.status.value,
            "request_url": row.request_url,
            "http_status": row.http_status,
            "response_sha256": row.response_sha256,
            "response_bytes": row.response_bytes,
            "candidates_found": row.candidates_found,
            "documents_created": row.documents_created,
            "duplicates_found": row.duplicates_found,
            "errors": row.errors,
            "item_results": row.item_results,
            "artifact_path": row.artifact_path,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }
        for row in rows
    ]


@router.post("/api/manual-documents", response_model=ManualDocumentCreated)
def create_manual_document(
    payload: ManualDocumentCreate,
    session: Session = Depends(get_session),
) -> ManualDocumentCreated:
    return submit_manual_document(session, payload)


@router.post(
    "/api/manual-documents/{document_id}/analyze",
    response_model=ManualAnalysisResponse,
)
def analyze_manual(
    document_id: int,
    session: Session = Depends(get_session),
) -> ManualAnalysisResponse:
    try:
        return analyze_manual_document(session, document_id)
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMAdapterTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMOutputValidationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/runs/{run_id}/replay")
def replay(
    run_id: str,
    mode: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    try:
        original = session.scalar(select(RunAudit).where(RunAudit.run_id == run_id))
        if original is None:
            raise ValueError(f"run not found: {run_id}")
        if original.run_kind == "source_collection":
            replayed, diff = replay_frozen_source(session, run_id)
        elif original.run_kind == "manual":
            replayed, diff = replay_manual_run(
                session,
                run_id,
                mode=mode or "frozen-output",
            )
        else:
            replayed, diff = replay_run(session, run_id)
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMAdapterTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"run": _run_payload(replayed), "diff": diff}
