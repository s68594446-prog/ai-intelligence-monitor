from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Document, RunAudit
from app.source_collections.contracts import ExtractionStatus, SourceRegistry
from app.source_collections.registry import get_source
from app.source_collections.security import CollectionSecurityError, validate_url


class SourceCollectionOpsChecker:
    checks = 16

    def __init__(self, session: Session):
        self.session = session

    def check(self, artifact: dict) -> dict:
        errors: list[str] = []
        try:
            registry = SourceRegistry.model_validate(artifact.get("registry_snapshot"))
        except Exception:
            return {"passed": False, "errors": ["source registry snapshot is missing or invalid"], "checks": self.checks}
        if not artifact.get("registry_version"):
            errors.append("registry version was not recorded")
        if not artifact.get("coverage_version"):
            errors.append("coverage version was not recorded")
        analyzed = set(artifact.get("analyzed_document_ids", []))
        duplicate_ids = {item.get("document_id") for item in artifact.get("duplicates", [])}
        filtered_urls = {item.get("canonical_url") for item in artifact.get("filtered_items", [])}
        successful_documents: dict[int, dict] = {}
        for source_result in artifact.get("per_source_results", []):
            source_id = source_result.get("source_id")
            try:
                source = get_source(registry, source_id)
            except (KeyError, ValueError) as exc:
                errors.append(str(exc))
                continue
            if source_result.get("source_tier") != source.source_tier.value:
                errors.append(f"source tier does not match registry: {source_id}")
            for url in source_result.get("requested_urls", []):
                try:
                    validate_url(url, source)
                except CollectionSecurityError as exc:
                    errors.append(f"disallowed requested URL for {source_id}: {exc}")
            for fetch in source_result.get("fetch_results", []):
                try:
                    validate_url(fetch["canonical_url"], source, article=True)
                    validate_url(fetch["final_url"], source, article=True)
                except (KeyError, CollectionSecurityError) as exc:
                    errors.append(f"fetch URL failed allowlist for {source_id}: {exc}")
                if fetch.get("extraction_status") == ExtractionStatus.SUCCESS.value:
                    if not fetch.get("raw_text"):
                        errors.append("successful extraction omitted raw_text")
                if fetch.get("http_status") == 403 and fetch.get("extraction_status") == ExtractionStatus.SUCCESS.value:
                    errors.append("HTTP 403 was represented as successful extraction")
                if not fetch.get("fetched_at") or "http_status" not in fetch or not fetch.get("extraction_status"):
                    errors.append("fetch metadata is incomplete")
        for decision in artifact.get("document_decisions", []):
            document_id = decision.get("document_id")
            if decision.get("decision") == "CREATED":
                document = self.session.get(Document, document_id)
                if document is None or not document.raw_text:
                    errors.append(f"created document is missing full raw_text: {document_id}")
                else:
                    successful_documents[document_id] = decision
            if decision.get("decision") == "DUPLICATE" and document_id in analyzed:
                errors.append(f"duplicate document entered analysis: {document_id}")
        for document_id in analyzed:
            if document_id not in successful_documents:
                errors.append(f"analyzed document is not traceable to a successful new fetch: {document_id}")
            document = self.session.get(Document, document_id)
            if document is None:
                continue
            if document.url in filtered_urls:
                errors.append(f"filtered document entered analysis: {document_id}")
            # Analysis itself is verified by the unchanged Phase 1 Ops Check stored on its RunAudit.
            if document.run_id:
                from sqlalchemy import select
                analysis_run = self.session.scalar(select(RunAudit).where(RunAudit.run_id == document.run_id))
                if analysis_run and (not analysis_run.ops_check or not analysis_run.ops_check.get("passed")):
                    errors.append(f"analysis run did not pass Phase 1 Ops Check: {document_id}")
        if len(artifact.get("documents_created", [])) > artifact.get("limits", {}).get("max_documents", 10):
            errors.append("document creation budget exceeded")
        if len(analyzed) > artifact.get("limits", {}).get("max_analysis", 3):
            errors.append("analysis budget exceeded")
        if artifact.get("request_count", 0) > artifact.get("limits", {}).get("max_requests", 100):
            errors.append("request budget exceeded")
        if duplicate_ids.intersection(analyzed):
            errors.append("duplicate entered analysis")
        return {"passed": not errors, "errors": errors, "checks": self.checks}
