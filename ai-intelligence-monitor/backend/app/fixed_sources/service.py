import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import FactType, FetchStatus, PipelineState, VerificationStatus
from app.core.settings import Settings, settings
from app.fixed_sources.collector import ControlledSourceCollector, FixedSourceError
from app.fixed_sources.contracts import (
    FixedFetchResponse,
    FixedSourceConfig,
    FixedSourceSummary,
    OfficialSourceRegistry,
)
from app.models import Document, Source, SourceFetchAudit


def load_source_registry(path: Path) -> OfficialSourceRegistry:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    registry = OfficialSourceRegistry.model_validate(payload)
    keys = [source.key for source in registry.sources]
    if len(keys) != len(set(keys)):
        raise ValueError("fixed-source registry contains duplicate keys")
    return registry


def source_by_key(path: Path, source_key: str) -> FixedSourceConfig:
    registry = load_source_registry(path)
    source = next((item for item in registry.sources if item.key == source_key), None)
    if source is None or not source.active:
        raise KeyError(f"fixed source not found or inactive: {source_key}")
    return source


def list_fixed_sources(app_settings: Settings = settings) -> list[FixedSourceSummary]:
    registry = load_source_registry(app_settings.fixed_sources_path)
    return [
        FixedSourceSummary(
            key=source.key,
            name=source.name,
            trust_tier=source.trust_tier,
            source_type=source.source_type,
            mode=source.mode,
            index_url=str(source.index_url),
            active=source.active,
            default_limit=source.default_limit,
        )
        for source in registry.sources
    ]


def _previous_cache_headers(session: Session, source_key: str) -> tuple[str | None, str | None]:
    previous = session.scalar(
        select(SourceFetchAudit)
        .where(
            SourceFetchAudit.source_key == source_key,
            SourceFetchAudit.status.in_([FetchStatus.PASSED, FetchStatus.NOT_MODIFIED]),
        )
        .order_by(SourceFetchAudit.started_at.desc())
    )
    return (previous.etag, previous.last_modified) if previous else (None, None)


def _ensure_source(session: Session, config: FixedSourceConfig) -> Source:
    source = session.scalar(
        select(Source).where(Source.name == config.name, Source.url == str(config.index_url))
    )
    if source is None:
        source = Source(
            name=config.name,
            source_type=config.source_type,
            trust_tier=config.trust_tier,
            url=str(config.index_url),
            active=True,
        )
        session.add(source)
        session.flush()
    return source


def _write_manifest(directory: Path, fetch_id: str, payload: dict) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fetch_id}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return str(path)


def fetch_fixed_source(
    session: Session,
    source_key: str,
    *,
    limit: int | None = None,
    app_settings: Settings = settings,
    client: httpx.Client | None = None,
) -> FixedFetchResponse:
    config = source_by_key(app_settings.fixed_sources_path, source_key)
    effective_limit = limit or config.default_limit
    if effective_limit < 1 or effective_limit > 10:
        raise ValueError("fixed-source limit must be between 1 and 10")
    fetch_id = "fetch-" + uuid4().hex
    audit = SourceFetchAudit(
        fetch_id=fetch_id,
        source_key=config.key,
        source_name=config.name,
        status=FetchStatus.RUNNING,
        request_url=str(config.index_url),
        response_bytes=0,
        candidates_found=0,
        documents_created=0,
        duplicates_found=0,
        errors=[],
        item_results=[],
    )
    session.add(audit)
    session.commit()
    collector = ControlledSourceCollector(
        config,
        timeout_seconds=app_settings.fixed_fetch_timeout_seconds,
        max_bytes=app_settings.fixed_fetch_max_bytes,
        user_agent=app_settings.fixed_fetch_user_agent,
        client=client,
    )
    try:
        etag, last_modified = _previous_cache_headers(session, config.key)
        result = collector.collect(effective_limit, etag=etag, last_modified=last_modified)
        audit.status = result.status
        audit.request_url = result.request_url
        audit.http_status = result.http_status
        audit.etag = result.etag
        audit.last_modified = result.last_modified
        audit.response_sha256 = result.response_sha256
        audit.response_bytes = result.response_bytes
        audit.candidates_found = len(result.candidates)
        audit.item_results = list(result.item_results)
        created_ids: list[int] = []
        duplicate_ids: list[int] = []
        if result.status == FetchStatus.PASSED:
            source = _ensure_source(session, config)
            for candidate in result.candidates:
                content_hash = hashlib.sha256(candidate.raw_text.encode("utf-8")).hexdigest()
                existing = session.scalar(
                    select(Document)
                    .where(Document.content_hash == content_hash, Document.ingestion_id.is_not(None))
                    .order_by(Document.created_at.asc())
                )
                if existing is not None:
                    duplicate_ids.append(existing.id)
                    audit.item_results = list(audit.item_results) + [
                        {
                            "title": candidate.title,
                            "url": str(candidate.url),
                            "status": "DUPLICATE_DOCUMENT",
                            "document_id": existing.id,
                        }
                    ]
                    continue
                ingestion_id = f"fixed-{config.key}-{uuid4().hex}"
                document = Document(
                    run_id=ingestion_id,
                    ingestion_id=ingestion_id,
                    source_id=source.id,
                    title=candidate.title,
                    url=str(candidate.url),
                    published_at=candidate.published_at.astimezone(timezone.utc),
                    fetched_at=datetime.now(timezone.utc),
                    language=candidate.language,
                    raw_text=candidate.raw_text,
                    content_hash=content_hash,
                    fact_type=FactType.UNKNOWN,
                    verification_status=VerificationStatus.UNVERIFIED,
                    pipeline_state=PipelineState.COLLECTED,
                    event_key_hint=f"fixed-{content_hash[:24]}",
                    fixture_payload={
                        "fetch_id": fetch_id,
                        "source_key": config.key,
                        "source_registry_version": load_source_registry(
                            app_settings.fixed_sources_path
                        ).version,
                        "response_sha256": candidate.response_sha256,
                        "response_bytes": candidate.response_bytes,
                    },
                    optional_notes=f"Collected from approved fixed source {config.key}; analysis remains manual.",
                )
                session.add(document)
                session.flush()
                created_ids.append(document.id)
            audit.documents_created = len(created_ids)
            audit.duplicates_found = len(duplicate_ids)
        audit.finished_at = datetime.now(timezone.utc)
        manifest = {
            "fetch_id": fetch_id,
            "source_key": config.key,
            "source_registry": str(app_settings.fixed_sources_path),
            "source_registry_version": load_source_registry(app_settings.fixed_sources_path).version,
            "collector_version": collector.version,
            "status": audit.status.value,
            "request_url": audit.request_url,
            "http_status": audit.http_status,
            "etag": audit.etag,
            "last_modified": audit.last_modified,
            "response_sha256": audit.response_sha256,
            "response_bytes": audit.response_bytes,
            "candidates_found": audit.candidates_found,
            "documents_created": audit.documents_created,
            "duplicates_found": audit.duplicates_found,
            "document_ids": created_ids,
            "duplicate_document_ids": duplicate_ids,
            "item_results": audit.item_results,
            "errors": audit.errors,
        }
        audit.artifact_path = _write_manifest(app_settings.fetch_audits_dir, fetch_id, manifest)
        session.commit()
        return FixedFetchResponse(
            fetch_id=fetch_id,
            source_key=config.key,
            status=audit.status,
            candidates_found=audit.candidates_found,
            documents_created=audit.documents_created,
            duplicates_found=audit.duplicates_found,
            document_ids=created_ids,
            duplicate_document_ids=duplicate_ids,
            artifact_path=audit.artifact_path,
            errors=audit.errors,
        )
    except Exception as exc:
        session.rollback()
        failed = session.scalar(select(SourceFetchAudit).where(SourceFetchAudit.fetch_id == fetch_id))
        if failed is not None:
            metadata = collector.last_response
            failed.status = FetchStatus.FAILED
            failed.request_url = str(metadata.get("request_url") or failed.request_url)
            failed.http_status = metadata.get("http_status")
            failed.etag = metadata.get("etag")
            failed.last_modified = metadata.get("last_modified")
            failed.response_sha256 = metadata.get("response_sha256")
            failed.response_bytes = int(metadata.get("response_bytes") or 0)
            failed.errors = [f"{type(exc).__name__}: {exc}"]
            failed.finished_at = datetime.now(timezone.utc)
            manifest = {
                "fetch_id": fetch_id,
                "source_key": config.key,
                "source_registry_version": load_source_registry(
                    app_settings.fixed_sources_path
                ).version,
                "collector_version": collector.version,
                "status": failed.status.value,
                "request_url": failed.request_url,
                "http_status": failed.http_status,
                "response_sha256": failed.response_sha256,
                "response_bytes": failed.response_bytes,
                "errors": failed.errors,
            }
            failed.artifact_path = _write_manifest(app_settings.fetch_audits_dir, fetch_id, manifest)
            session.commit()
        if isinstance(exc, (FixedSourceError, ValueError)):
            raise
        raise FixedSourceError("fixed-source collection failed") from exc
    finally:
        collector.close()
