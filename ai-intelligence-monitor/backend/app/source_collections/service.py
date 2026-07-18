import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import RunStatus
from app.core.settings import Settings, settings
from app.coverage.service import ensure_coverage
from app.manual.contracts import ManualDocumentCreate
from app.manual.service import analyze_manual_document, submit_manual_document
from app.models import Document, RunAudit
from app.source_collections.contracts import (
    CandidateStatus,
    CollectionRequest,
    CollectionResponse,
    DiscoveryMethod,
    ExtractionStatus,
)
from app.source_collections.discovery import HTMLIndexSourceDiscovery, RSSSourceDiscovery
from app.source_collections.fetch import HTTPArticleFetcher
from app.source_collections.ops import SourceCollectionOpsChecker
from app.source_collections.prefilter import prefilter
from app.source_collections.registry import get_source, load_registry
from app.source_collections.security import CollectionError, SafeHttpClient


def _dump(path: Path, payload: dict):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str, ensure_ascii=False), encoding="utf-8")


def collect_sources(
    session: Session,
    request: CollectionRequest,
    *,
    app_settings: Settings = settings,
    clients: dict[str, httpx.Client] | None = None,
    sleep_fn=None,
    analyzer=analyze_manual_document,
) -> CollectionResponse:
    registry = load_registry(app_settings.source_registry_path)
    if request.coverage_id != registry.coverage_id or request.coverage_id != "dram_hbm":
        raise ValueError("Phase 2 supports only dram_hbm coverage")
    selected = [get_source(registry, source_id) for source_id in request.source_ids]
    topic, thesis = ensure_coverage(session, app_settings.coverage_path)
    coverage = topic.config_snapshot
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-source-" + uuid4().hex[:8]
    run_dir = Path(app_settings.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    audit = RunAudit(
        run_id=run_id, scenario="source_collection", status=RunStatus.RUNNING,
        coverage_id=topic.coverage_id, coverage_version=topic.version,
        coverage_snapshot=coverage, thesis_version=thesis.version,
        prompt_version="phase2-source-collection-v1", model_adapter="SourceCollectionService",
        adapter_versions={"registry": registry.registry_version, "discovery": "source-discovery-v1", "fetch": "http-article-fetch-v1"},
        errors=[], artifact_paths=[], run_kind="source_collection", replay_mode="LIVE_SOURCE",
    )
    session.add(audit)
    session.commit()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=request.since_hours)
    per_source_results = []
    discovered_items = []
    prefilter_results = []
    fetch_results = []
    filtered_items = []
    failures = []
    duplicates = []
    document_decisions = []
    created_ids = []
    reused_ids = []
    analyzed_ids = []
    request_count = 0
    retry_count = 0
    clients = clients or {}
    for source in selected:
        source_result = {
            "source_id": source.source_id, "source_tier": source.source_tier.value,
            "role": source.role.value, "status": "PASSED", "requested_urls": [source.discovery_url],
            "discovered_count": 0, "candidate_count": 0, "filtered_count": 0,
            "fetch_results": [], "errors": [],
        }
        safe = SafeHttpClient(
            source, user_agent=app_settings.fixed_fetch_user_agent,
            client=clients.get(source.source_id),
            sleep_fn=sleep_fn or __import__("time").sleep,
            max_retries=2,
        )
        try:
            discovery = RSSSourceDiscovery() if source.discovery_method == DiscoveryMethod.RSS else HTMLIndexSourceDiscovery()
            items = discovery.discover(source, safe)
            items = [item for item in items if item.published_at is None or item.published_at >= since]
            items = items[: min(request.max_items_per_source, source.max_items_per_run)]
            source_result["discovered_count"] = len(items)
            for item in items:
                serialized = item.model_dump(mode="json")
                discovered_items.append(serialized)
                result = prefilter(item, source, coverage)
                prefilter_results.append(result.model_dump(mode="json"))
                if result.candidate_status != CandidateStatus.CANDIDATE:
                    source_result["filtered_count"] += 1
                    filtered_items.append({**serialized, **result.model_dump(mode="json")})
                    continue
                source_result["candidate_count"] += 1
                if request.dry_run:
                    continue
                fetched = HTTPArticleFetcher(max_bytes=app_settings.fixed_fetch_max_bytes).fetch(item, source, safe)
                fetched_payload = fetched.model_dump(mode="json")
                source_result["fetch_results"].append(fetched_payload)
                fetch_results.append(fetched_payload)
                if fetched.extraction_status != ExtractionStatus.SUCCESS:
                    failures.append({"source_id": source.source_id, "url": item.canonical_url, "error_type": fetched.response_metadata.get("error_type") or fetched.extraction_status.value})
                    continue
                existing_url = session.scalar(
                    select(Document).where(Document.url == fetched.canonical_url, Document.ingestion_id.is_not(None)).order_by(Document.created_at.asc())
                )
                if existing_url is not None:
                    reused_ids.append(existing_url.id)
                    duplicates.append({"reason": "CANONICAL_URL", "document_id": existing_url.id, "canonical_url": fetched.canonical_url})
                    document_decisions.append({"decision": "DUPLICATE", "reason": "CANONICAL_URL", "document_id": existing_url.id, "canonical_url": fetched.canonical_url})
                    continue
                if len(created_ids) >= app_settings.source_collection_max_documents:
                    failures.append({"source_id": source.source_id, "url": item.canonical_url, "error_type": "DOCUMENT_BUDGET_EXCEEDED"})
                    continue
                created = submit_manual_document(session, ManualDocumentCreate(
                    title=fetched.title, url=fetched.canonical_url, source_name=source.name,
                    source_tier=source.source_tier, published_at=fetched.published_at or item.published_at or now,
                    language=fetched.language, raw_text=fetched.raw_text,
                    optional_notes=f"Collected from approved registry source {source.source_id}; registry {registry.registry_version}.",
                ))
                document = session.get(Document, created.document_id)
                provenance = {
                    "collection_run_id": run_id, "source_id": source.source_id,
                    "registry_version": registry.registry_version, "canonical_url": fetched.canonical_url,
                    "final_url": fetched.final_url, "extraction_status": fetched.extraction_status.value,
                    "response_metadata": fetched.response_metadata,
                }
                document.fixture_payload = {**(document.fixture_payload or {}), **provenance}
                session.commit()
                if created.duplicate:
                    reused_ids.append(document.id)
                    duplicates.append({"reason": "CONTENT_HASH", "document_id": document.id, "canonical_url": fetched.canonical_url})
                    document_decisions.append({"decision": "DUPLICATE", "reason": "CONTENT_HASH", "document_id": document.id, "canonical_url": fetched.canonical_url})
                else:
                    created_ids.append(document.id)
                    document_decisions.append({"decision": "CREATED", "document_id": document.id, "canonical_url": fetched.canonical_url, "content_hash": document.content_hash})
                    if request.analyze and len(analyzed_ids) < app_settings.source_collection_max_analysis:
                        try:
                            analyzer(session, document.id)
                            analyzed_ids.append(document.id)
                        except Exception as exc:
                            failures.append({"source_id": source.source_id, "document_id": document.id, "error_type": type(exc).__name__})
            if source_result["errors"]:
                source_result["status"] = "FAILED"
        except Exception as exc:
            source_result["status"] = "FAILED"
            code = getattr(exc, "code", type(exc).__name__)
            source_result["errors"].append({"error_type": code, "message": str(exc)})
            failures.append({"source_id": source.source_id, "error_type": code})
        finally:
            request_count += safe.request_count
            retry_count += safe.retry_count
            safe.close()
            per_source_results.append(source_result)
    successful_sources = sum(item["status"] == "PASSED" for item in per_source_results)
    collection_status = "PASSED" if not failures else "PARTIAL_SUCCESS" if successful_sources else "FAILED"
    artifact = {
        "run_id": run_id, "registry_version": registry.registry_version,
        "registry_snapshot": registry.model_dump(mode="json"), "coverage_version": topic.version,
        "coverage_snapshot": coverage, "started_at": audit.started_at, "finished_at": datetime.now(timezone.utc),
        "requested_sources": request.source_ids, "request": request.model_dump(mode="json"),
        "per_source_results": per_source_results, "discovered_items": discovered_items,
        "prefilter_results": prefilter_results, "fetch_results": fetch_results,
        "duplicates": duplicates, "filtered_items": filtered_items, "failures": failures,
        "documents_created": created_ids, "documents_reused": sorted(set(reused_ids)),
        "document_decisions": document_decisions, "analyzed_document_ids": analyzed_ids,
        "request_count": request_count, "retry_count": retry_count,
        "limits": {"max_documents": app_settings.source_collection_max_documents, "max_analysis": app_settings.source_collection_max_analysis, "max_requests": 100},
        "status": collection_status,
    }
    artifact_path = run_dir / "00-source-fetch.json"
    _dump(artifact_path, artifact)
    ops = SourceCollectionOpsChecker(session).check(artifact)
    artifact["ops_check"] = ops
    if not ops["passed"]:
        artifact["status"] = "FAILED_OPS_CHECK"
    _dump(artifact_path, artifact)
    audit.artifact_paths = [str(artifact_path)]
    audit.documents_processed = len(created_ids)
    audit.errors = [f"{item.get('source_id')}: {item.get('error_type')}" for item in failures]
    audit.ops_check = {**ops, "collection_status": artifact["status"]}
    audit.status = RunStatus.PASSED if ops["passed"] and successful_sources else RunStatus.FAILED_OPS_CHECK if not ops["passed"] else RunStatus.FAILED
    audit.finished_at = datetime.now(timezone.utc)
    session.commit()
    return CollectionResponse(
        run_id=run_id, status=artifact["status"], sources_requested=request.source_ids,
        discovered_count=len(discovered_items), candidate_count=sum(item["candidate_status"] == CandidateStatus.CANDIDATE.value for item in prefilter_results),
        fetched_count=len(fetch_results), document_count=len(created_ids), duplicate_count=len(duplicates),
        filtered_count=len(filtered_items), error_count=len(failures), artifact_path=str(artifact_path),
        analyzed_document_ids=analyzed_ids,
    )
