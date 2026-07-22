import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import TrustTier
from app.core.settings import Settings, settings
from app.coverage.service import ensure_coverage
from app.manual.contracts import ManualDocumentCreate
from app.manual.service import analyze_manual_document, submit_manual_document
from app.models import Document, WebDiscoveryAudit
from app.source_collections.contracts import SourceRole
from app.web_discovery.contracts import (
    ClassifiedTier,
    DiscoveryRequest,
    DiscoveryResponse,
    DiscoveryStatus,
    FetchStatus,
    NormalizedSearchResult,
    SearchQuery,
)
from app.web_discovery.fetch import WebPageFetcher
from app.web_discovery.normalize import cluster_event_keys, event_cluster_key, normalize_search_pages
from app.web_discovery.ops import WebDiscoveryOpsChecker
from app.web_discovery.query_plan import build_query_plan
from app.web_discovery.search import WebSearchAdapter
from app.web_discovery.source_policy import load_source_policy
from app.web_discovery.triage import DiscoveryTriageAdapter


def _dump(path: Path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str), encoding="utf-8")


def _snapshot_thesis(thesis) -> dict:
    return {
        "statement": thesis.statement,
        "assumptions": thesis.assumptions,
        "disconfirming_conditions": thesis.disconfirming_conditions,
        "reverse_triggers": thesis.reverse_triggers,
        "version": thesis.version,
    }


def _as_tier(value: ClassifiedTier) -> TrustTier:
    return {ClassifiedTier.T1: TrustTier.T1, ClassifiedTier.T2: TrustTier.T2, ClassifiedTier.T3: TrustTier.T3}[value]


def discover_web(
    session: Session,
    request: DiscoveryRequest,
    *,
    app_settings: Settings = settings,
    search_adapter: WebSearchAdapter | None = None,
    news_adapter: WebSearchAdapter | None = None,
    triage_adapter: DiscoveryTriageAdapter | None = None,
    fetcher: WebPageFetcher | None = None,
    analyzer=analyze_manual_document,
) -> DiscoveryResponse:
    topic, thesis = ensure_coverage(session, app_settings.coverage_path)
    coverage = topic.config_snapshot
    thesis_snapshot = _snapshot_thesis(thesis)
    plan_version, queries = build_query_plan(
        app_settings.web_search_plan_path,
        coverage,
        max_queries=app_settings.web_search_max_queries_per_run,
    )
    news_queries = [
        item.model_copy(update={
            "query_id": f"news_{item.query_id}",
            "time_range": f"{app_settings.news_search_lookback_days}d",
        })
        for item in queries[: app_settings.news_search_max_queries]
    ]
    policy = load_source_policy(app_settings.web_source_policy_path)
    if policy.coverage_id != request.coverage_id:
        raise ValueError("source policy coverage does not match request")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-web-" + uuid4().hex[:8]
    run_dir = Path(app_settings.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    paths = [run_dir / name for name in (
        "00-search-plan.json", "01-search-results.json", "02-discovery-triage.json",
        "03-web-fetch.json", "04-discovery-decisions.json",
    )]
    path_strings = [str(path) for path in paths]
    limits = {
        "max_queries": app_settings.web_search_max_queries_per_run,
        "max_primary_backtraces": 1,
        "max_total_queries": app_settings.web_search_max_queries_per_run + 1,
        "max_results_per_query": app_settings.web_search_max_results_per_query,
        "max_news_queries": app_settings.news_search_max_queries,
        "max_news_results_per_query": app_settings.news_search_max_results_per_query,
        "max_news_pages_per_query": 1,
        "news_lookback_days": app_settings.news_search_lookback_days,
        "max_fetch": app_settings.web_fetch_max_pages_per_run,
        "max_triage": app_settings.web_triage_max_candidates_per_run,
        "max_analysis": app_settings.web_analyze_max_documents_per_run,
        "max_requests": (
            (app_settings.web_search_max_queries_per_run + 1) * 3
            + app_settings.news_search_max_queries * 3
            + app_settings.web_fetch_max_pages_per_run * 8
        ),
        "max_primary_backtraces_per_candidate": 1,
    }
    plan_payload = {
        "run_id": run_id,
        "request": request.model_dump(mode="json"),
        "query_plan_version": plan_version,
        "source_policy_version": policy.policy_version,
        "source_policy_snapshot": policy.model_dump(mode="json"),
        "coverage_id": topic.coverage_id,
        "coverage_version": topic.version,
        "coverage_snapshot": coverage,
        "thesis_version": thesis.version,
        "thesis_snapshot": thesis_snapshot,
        "query_plan": [item.model_dump(mode="json") for item in queries],
        "news_query_plan": [item.model_dump(mode="json") for item in news_queries],
        "provider": app_settings.web_search_provider,
        "news_provider": app_settings.news_search_provider,
        "fallbacks": [item.strip() for item in app_settings.web_search_fallbacks.split(",") if item.strip()],
        "limits": limits,
    }
    _dump(paths[0], plan_payload)
    audit = WebDiscoveryAudit(
        discovery_run_id=run_id,
        status=DiscoveryStatus.PLANNED.value,
        provider=app_settings.web_search_provider,
        adapter_version=getattr(search_adapter, "version", "not-invoked"),
        query_plan_version=plan_version,
        source_policy_version=policy.policy_version,
        coverage_id=topic.coverage_id,
        coverage_version=topic.version,
        thesis_version=thesis.version,
        query_plan=plan_payload["query_plan"],
        metrics={}, search_results=[], triage_results=[], source_chains=[],
        document_ids=[], analysis_run_ids=[], failures=[], artifact_paths=path_strings,
    )
    session.add(audit)
    session.commit()
    pages = []
    normalized: list[NormalizedSearchResult] = []
    triage_results = []
    fetch_results = []
    decisions: list[dict] = []
    failures: list[dict] = []
    source_chains: list[dict] = []
    analysis_run_ids: list[str] = []
    primary_backtrace_count = 0
    created_document_ids: list[int] = []
    duplicate_count = blocked_count = 0
    if request.search and not app_settings.web_discovery_enabled:
        failures.append({"stage": "search", "error_code": "WEB_DISCOVERY_DISABLED"})
    elif request.search and search_adapter is None:
        failures.append({"stage": "search", "error_code": "SEARCH_ADAPTER_REQUIRED"})
    elif request.search:
        for query in queries:
            try:
                pages.append(search_adapter.search(query, count=app_settings.web_search_max_results_per_query))
            except Exception as exc:
                failures.append({"stage": "search", "query_id": query.query_id, "error_code": getattr(exc, "code", type(exc).__name__)})
    if request.news and not app_settings.news_discovery_enabled:
        failures.append({"stage": "news_search", "error_code": "NEWS_DISCOVERY_DISABLED"})
    elif request.news and news_adapter is None:
        failures.append({"stage": "news_search", "error_code": "NEWS_ADAPTER_REQUIRED"})
    elif request.news:
        for query in news_queries:
            try:
                pages.append(
                    news_adapter.search(
                        query, count=app_settings.news_search_max_results_per_query
                    )
                )
            except Exception as exc:
                failures.append({
                    "stage": "news_search", "query_id": query.query_id,
                    "error_code": getattr(exc, "code", type(exc).__name__),
                })
    if pages:
        normalized = normalize_search_pages(pages, policy)
    existing_urls = {
        document.url: document
        for document in session.scalars(select(Document).where(Document.ingestion_id.is_not(None))).all()
        if document.url
    }
    active_candidates = []
    for candidate in normalized:
        existing = existing_urls.get(candidate.canonical_url)
        if existing is not None:
            duplicate_count += 1
            decisions.append({
                "candidate_id": candidate.candidate_id, "decision": "DUPLICATE", "reason": "CANONICAL_URL",
                "canonical_url": candidate.canonical_url, "source_tier": candidate.source_tier.value,
                "source_role": SourceRole.DIRECT.value, "query_ids": candidate.query_ids,
                "event_cluster_key": event_cluster_key(candidate.title), "document_id": existing.id,
                "content_hash": existing.content_hash, "analysis_run_id": None, "source_chain_ids": [],
            })
        else:
            active_candidates.append(candidate)
    if request.triage and triage_adapter is None:
        failures.append({"stage": "triage", "error_code": "TRIAGE_ADAPTER_REQUIRED"})
    elif request.triage:
        triage_candidates = sorted(
            active_candidates,
            key=lambda item: (
                {ClassifiedTier.T1: 0, ClassifiedTier.T2: 1,
                 ClassifiedTier.T3: 2, ClassifiedTier.UNVERIFIED: 3}[item.source_tier],
                item.best_rank, item.canonical_url,
            ),
        )[: limits["max_triage"]]
        for candidate in triage_candidates:
            try:
                result = triage_adapter.triage(
                    candidate,
                    coverage_snapshot=coverage,
                    thesis_snapshot=thesis_snapshot,
                    reverse_triggers=thesis.reverse_triggers,
                )
                triage_results.append(result)
                if result.contract.primary_source_needed and candidate.source_tier in {ClassifiedTier.T2, ClassifiedTier.T3, ClassifiedTier.UNVERIFIED}:
                    if (
                        primary_backtrace_count < limits["max_primary_backtraces"]
                        and result.contract.primary_search_terms
                        and search_adapter is not None
                    ):
                        primary_query = SearchQuery(
                            query_id=f"primary_{candidate.candidate_id.replace('-', '_')}",
                            text=(
                                result.contract.primary_search_terms[0]
                                + " (site:micron.com OR site:samsung.com OR site:skhynix.com"
                                + " OR site:nvidia.com OR site:tsmc.com OR site:sec.gov OR site:gov.cn)"
                            ), variable="primary source verification",
                            purpose="bounded primary-source backtrace", language="en", time_range="30d",
                            plan_version=plan_version,
                        )
                        try:
                            primary_backtrace_count += 1
                            page = search_adapter.search(primary_query, count=app_settings.web_search_max_results_per_query)
                            pages.append(page)
                            primary_results = normalize_search_pages([page], policy)
                            for primary in primary_results:
                                primary.primary_parent_id = candidate.candidate_id
                                if all(item.canonical_url != primary.canonical_url for item in normalized):
                                    normalized.append(primary)
                            t1_results = [item.candidate_id for item in primary_results if item.source_tier == ClassifiedTier.T1]
                            source_chains.append({
                                "lead_candidate_id": candidate.candidate_id,
                                "primary_query_id": primary_query.query_id,
                                "result_candidate_ids": [item.candidate_id for item in primary_results],
                                "verification_status": "VERIFIED_T1" if t1_results else "UNVERIFIED",
                                "t1_candidate_ids": t1_results,
                            })
                        except Exception as exc:
                            failures.append({"stage": "primary_search", "candidate_id": candidate.candidate_id, "error_code": getattr(exc, "code", type(exc).__name__)})
                            source_chains.append({
                                "lead_candidate_id": candidate.candidate_id,
                                "primary_query_id": primary_query.query_id,
                                "result_candidate_ids": [], "verification_status": "UNVERIFIED",
                                "t1_candidate_ids": [], "failure_code": getattr(exc, "code", type(exc).__name__),
                            })
                    else:
                        source_chains.append({
                            "lead_candidate_id": candidate.candidate_id,
                            "primary_query_id": None, "result_candidate_ids": [],
                            "verification_status": "UNVERIFIED", "t1_candidate_ids": [],
                            "failure_code": "PRIMARY_QUERY_BUDGET_TERM_OR_WEB_PROVIDER_UNAVAILABLE",
                        })
            except Exception as exc:
                failures.append({"stage": "triage", "candidate_id": candidate.candidate_id, "error_code": type(exc).__name__})
    triage_by_id = {item.candidate_id: item for item in triage_results}
    if source_chains:
        for candidate in normalized:
            if candidate.primary_parent_id and candidate.candidate_id not in triage_by_id:
                # A recovered T1 primary is fetched under the lead's bounded decision; it is not separately LLM-triaged.
                lead = triage_by_id.get(candidate.primary_parent_id)
                if lead and candidate.source_tier == ClassifiedTier.T1:
                    triage_by_id[candidate.candidate_id] = lead.model_copy(update={"candidate_id": candidate.candidate_id, "contract": lead.contract.model_copy(update={"candidate_id": candidate.candidate_id, "primary_source_needed": False, "should_fetch": True})})
    cluster_keys = cluster_event_keys(normalized)
    for decision in decisions:
        if decision["candidate_id"] in cluster_keys:
            decision["event_cluster_key"] = cluster_keys[decision["candidate_id"]]
    fetch_candidates = [
        item for item in normalized
        if item.candidate_id in triage_by_id
        and triage_by_id[item.candidate_id].contract.should_fetch
        and item.source_tier in {ClassifiedTier.T1, ClassifiedTier.T2}
    ]
    fetch_candidates.sort(key=lambda item: (-triage_by_id[item.candidate_id].contract.fetch_priority, item.best_rank, item.canonical_url))
    selected_fetch_ids = {item.candidate_id for item in fetch_candidates[: limits["max_fetch"]]}
    decided_ids = {item["candidate_id"] for item in decisions}
    for candidate in normalized:
        triage_result = triage_by_id.get(candidate.candidate_id)
        if candidate.candidate_id in selected_fetch_ids or candidate.candidate_id in decided_ids:
            continue
        if triage_result is None:
            if not request.triage:
                continue
            decisions.append({
                "candidate_id": candidate.candidate_id,
                "decision": "FILTERED", "reason": "TRIAGE_BUDGET",
                "canonical_url": candidate.canonical_url,
                "source_tier": candidate.source_tier.value,
                "source_role": SourceRole.DIRECT.value,
                "query_ids": candidate.query_ids,
                "event_cluster_key": cluster_keys.get(
                    candidate.candidate_id, event_cluster_key(candidate.title)
                ),
                "document_id": None, "content_hash": None,
                "analysis_run_id": None, "source_chain_ids": [],
            })
            continue
        if candidate.source_tier == ClassifiedTier.T3:
            decision, reason = "FILTERED", "T3_DISCOVERY_ONLY"
        elif candidate.source_tier == ClassifiedTier.UNVERIFIED:
            decision, reason = "UNVERIFIED", "UNKNOWN_DOMAIN_NOT_FETCHED"
            blocked_count += 1
        else:
            decision, reason = "FILTERED", "TRIAGE_FILTERED_OR_FETCH_BUDGET"
        decisions.append({
            "candidate_id": candidate.candidate_id, "decision": decision, "reason": reason,
            "canonical_url": candidate.canonical_url, "source_tier": candidate.source_tier.value,
            "source_role": triage_result.contract.source_role.value, "query_ids": candidate.query_ids,
            "event_cluster_key": cluster_keys.get(candidate.candidate_id, event_cluster_key(candidate.title)), "document_id": None,
            "content_hash": None, "analysis_run_id": None,
            "source_chain_ids": [candidate.primary_parent_id] if candidate.primary_parent_id else [],
        })
    if request.fetch and fetcher is None:
        failures.append({"stage": "fetch", "error_code": "FETCHER_REQUIRED"})
    elif request.fetch:
        allowed_urls = {item.canonical_url for item in normalized}
        for candidate in fetch_candidates[: limits["max_fetch"]]:
            fetched = fetcher.fetch(candidate, allowed_urls=allowed_urls)
            fetch_results.append(fetched)
            triage_result = triage_by_id[candidate.candidate_id]
            if fetched.status != FetchStatus.SUCCESS:
                blocked_count += 1
                decisions.append({
                    "candidate_id": candidate.candidate_id, "decision": "BLOCKED", "reason": fetched.error_code or fetched.status.value,
                    "canonical_url": candidate.canonical_url, "source_tier": candidate.source_tier.value,
                    "source_role": triage_result.contract.source_role.value, "query_ids": candidate.query_ids,
                    "event_cluster_key": cluster_keys.get(candidate.candidate_id, event_cluster_key(candidate.title)), "document_id": None,
                    "content_hash": None, "analysis_run_id": None, "source_chain_ids": [candidate.primary_parent_id] if candidate.primary_parent_id else [],
                })
                continue
            if candidate.source_tier == ClassifiedTier.UNVERIFIED:
                blocked_count += 1
                decisions.append({
                    "candidate_id": candidate.candidate_id, "decision": "UNVERIFIED", "reason": "UNKNOWN_DOMAIN_NOT_INGESTED",
                    "canonical_url": candidate.canonical_url, "source_tier": candidate.source_tier.value,
                    "source_role": triage_result.contract.source_role.value, "query_ids": candidate.query_ids,
                    "event_cluster_key": cluster_keys.get(candidate.candidate_id, event_cluster_key(fetched.title)), "document_id": None,
                    "content_hash": fetched.content_hash, "analysis_run_id": None, "source_chain_ids": [candidate.primary_parent_id] if candidate.primary_parent_id else [],
                })
                continue
            existing_content = session.scalar(select(Document).where(Document.content_hash == fetched.content_hash, Document.ingestion_id.is_not(None)).order_by(Document.created_at.asc()))
            if existing_content is not None:
                duplicate_count += 1
                decisions.append({
                    "candidate_id": candidate.candidate_id, "decision": "DUPLICATE", "reason": "CONTENT_HASH",
                    "canonical_url": candidate.canonical_url, "source_tier": candidate.source_tier.value,
                    "source_role": triage_result.contract.source_role.value, "query_ids": candidate.query_ids,
                    "event_cluster_key": cluster_keys.get(candidate.candidate_id, event_cluster_key(fetched.title)), "document_id": existing_content.id,
                    "content_hash": fetched.content_hash, "analysis_run_id": None, "source_chain_ids": [candidate.primary_parent_id] if candidate.primary_parent_id else [],
                })
                continue
            created = submit_manual_document(session, ManualDocumentCreate(
                title=fetched.title, url=fetched.final_url, source_name=candidate.source_domain,
                source_tier=_as_tier(candidate.source_tier),
                published_at=fetched.published_at or datetime.now(timezone.utc), language=fetched.language or "und",
                raw_text=fetched.raw_text,
                optional_notes=f"Web Discovery {run_id}; policy {policy.policy_version}.",
            ))
            document = session.get(Document, created.document_id)
            document.fixture_payload = {
                **(document.fixture_payload or {}),
                "web_discovery": {
                    "discovery_run_id": run_id, "candidate_id": candidate.candidate_id,
                    "query_ids": candidate.query_ids, "canonical_url": candidate.canonical_url,
                    "final_url": fetched.final_url, "source_tier": candidate.source_tier.value,
                    "source_policy_version": policy.policy_version,
                    "source_role": triage_result.contract.source_role.value,
                    "source_chain_ids": [candidate.primary_parent_id] if candidate.primary_parent_id else [],
                    "response_sha256": fetched.response_sha256, "response_bytes": fetched.response_bytes,
                    "security_checks": fetched.security_checks,
                },
            }
            session.commit()
            created_document_ids.append(document.id)
            decisions.append({
                "candidate_id": candidate.candidate_id, "decision": "CREATED", "reason": "FETCHED_PUBLIC_TEXT",
                "canonical_url": candidate.canonical_url, "source_tier": candidate.source_tier.value,
                "source_role": triage_result.contract.source_role.value, "query_ids": candidate.query_ids,
                "event_cluster_key": cluster_keys.get(candidate.candidate_id, event_cluster_key(fetched.title)), "document_id": document.id,
                "content_hash": document.content_hash, "analysis_run_id": None,
                "source_chain_ids": [candidate.primary_parent_id] if candidate.primary_parent_id else [],
            })
    base_bundle = {
        **plan_payload, "provider": app_settings.web_search_provider, "adapter_version": audit.adapter_version,
        "provider_pages": [item.model_dump(mode="json") for item in pages],
        "normalized_results": [item.model_dump(mode="json") for item in normalized],
        "triage_results": [item.model_dump(mode="json") for item in triage_results],
        "fetch_results": [item.model_dump(mode="json") for item in fetch_results],
        "document_decisions": decisions, "source_chains": source_chains,
        "failures": failures, "artifact_paths": path_strings, "email_attempted": False,
        "email_auto_send_effective": False,
        "provider_routing_audit": list(getattr(search_adapter, "audit", [])),
    }
    _dump(paths[1], {"run_id": run_id, "provider_pages": base_bundle["provider_pages"], "normalized_results": base_bundle["normalized_results"], "provider_routing_audit": base_bundle["provider_routing_audit"]})
    _dump(paths[2], {"run_id": run_id, "triage_results": base_bundle["triage_results"], "source_chains": source_chains})
    _dump(paths[3], {"run_id": run_id, "fetch_results": base_bundle["fetch_results"]})
    _dump(paths[4], {"run_id": run_id, "document_decisions": decisions, "failures": failures})
    ops = WebDiscoveryOpsChecker(policy).check(base_bundle)
    if request.analyze and ops["passed"]:
        eligible = [
            decision for decision in decisions
            if decision["decision"] == "CREATED" and decision["source_tier"] in {"T1", "T2"}
        ]
        eligible.sort(key=lambda item: (0 if item["source_tier"] == "T1" else 1, item["candidate_id"]))
        selected_clusters: set[str] = set()
        for decision in eligible:
            if len(analysis_run_ids) >= limits["max_analysis"]:
                break
            if decision["event_cluster_key"] in selected_clusters:
                continue
            selected_clusters.add(decision["event_cluster_key"])
            try:
                analysis_settings = app_settings.model_copy(update={"email_auto_send": False})
                response = analyzer(session, decision["document_id"], app_settings=analysis_settings)
                decision["analysis_run_id"] = response.run_id
                decision["analysis_ops_passed"] = bool(response.ops_check_status)
                analysis_run_ids.append(response.run_id)
                if not response.ops_check_status:
                    failures.append({"stage": "analysis_ops", "document_id": decision["document_id"], "run_id": response.run_id, "error_code": "ANALYSIS_OPS_FAILED"})
            except Exception as exc:
                failures.append({"stage": "analysis", "document_id": decision["document_id"], "error_code": type(exc).__name__})
        base_bundle["document_decisions"] = decisions
        base_bundle["failures"] = failures
        ops = WebDiscoveryOpsChecker(policy).check(base_bundle)
    _dump(paths[1], {"run_id": run_id, "provider_pages": base_bundle["provider_pages"], "normalized_results": base_bundle["normalized_results"], "provider_routing_audit": base_bundle["provider_routing_audit"]})
    _dump(paths[2], {"run_id": run_id, "triage_results": base_bundle["triage_results"], "source_chains": source_chains})
    _dump(paths[3], {"run_id": run_id, "fetch_results": base_bundle["fetch_results"]})
    _dump(paths[4], {"run_id": run_id, "document_decisions": decisions, "failures": failures, "ops_check": ops})
    if not ops["passed"]:
        status = DiscoveryStatus.FAILED_OPS_CHECK
    elif (request.search or request.news) and (not pages or failures):
        status = DiscoveryStatus.PARTIAL_SUCCESS if pages else DiscoveryStatus.FAILED
    elif not (request.search or request.news):
        status = DiscoveryStatus.PLANNED
    else:
        status = DiscoveryStatus.PASSED
    metrics = {
        "queries_planned": len(queries),
        "search_requests": (
            sum(item.get("request_count", 0) for item in getattr(search_adapter, "audit", []))
            + sum(item.request_count for item in pages if item.provider == "thenewsapi")
            or sum(item.request_count for item in pages)
        ),
        "search_retries": (
            sum(item.get("retry_count", 0) for item in getattr(search_adapter, "audit", []))
            + sum(item.retry_count for item in pages if item.provider == "thenewsapi")
            or sum(item.retry_count for item in pages)
        ),
        "search_results": len(normalized),
        "news_queries": len([item for item in pages if item.provider == "thenewsapi"]),
        "news_results": sum(
            len(item.results) for item in pages if item.provider == "thenewsapi"
        ),
        "triaged": len(triage_results), "fetched": sum(item.status == FetchStatus.SUCCESS for item in fetch_results),
        "primary_backtraces": primary_backtrace_count,
        "blocked": blocked_count, "duplicates": duplicate_count, "documents_created": len(created_document_ids),
        "analyzed": len(analysis_run_ids),
    }
    audit.status = status.value
    audit.metrics = metrics
    audit.search_results = base_bundle["normalized_results"]
    audit.triage_results = base_bundle["triage_results"]
    audit.source_chains = source_chains
    audit.document_ids = created_document_ids
    audit.analysis_run_ids = analysis_run_ids
    audit.failures = failures
    audit.ops_check = ops
    audit.finished_at = datetime.now(timezone.utc)
    session.commit()
    return DiscoveryResponse(
        run_id=run_id, status=status, plan_count=len(queries), search_result_count=len(normalized),
        triage_count=len(triage_results), fetched_count=metrics["fetched"], document_count=len(created_document_ids),
        duplicate_count=duplicate_count, analyzed_count=len(analysis_run_ids), blocked_count=blocked_count,
        artifact_paths=path_strings, ops_check=ops,
    )
