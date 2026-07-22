import json
from datetime import datetime
from pathlib import Path

from app.web_discovery.contracts import ClassifiedTier, FetchStatus
from app.web_discovery.search import BRAVE_WEB_ENDPOINT
from app.web_discovery.provider_registry import PROVIDER_NAMES
from app.web_discovery.providers import (
    EXA_ENDPOINT,
    GEMINI_BASE,
    SEARXNG_ENDPOINT,
    TAVILY_ENDPOINT,
    THENEWSAPI_ENDPOINT,
    YOU_ENDPOINT,
)
from app.web_discovery.source_policy import WebSourcePolicy, classify_url


class WebDiscoveryOpsChecker:
    version = "web-discovery-ops-v1"

    def __init__(self, policy: WebSourcePolicy):
        self.policy = policy

    def check(self, bundle: dict) -> dict:
        errors: list[str] = []
        checks: dict[str, bool] = {}

        def require(name: str, condition: bool, message: str):
            checks[name] = bool(condition)
            if not condition:
                errors.append(message)

        plan = bundle.get("query_plan") or []
        pages = bundle.get("provider_pages") or []
        results = bundle.get("normalized_results") or []
        triage = bundle.get("triage_results") or []
        fetches = bundle.get("fetch_results") or []
        decisions = bundle.get("document_decisions") or []
        limits = bundle.get("limits") or {}
        require("provider_recorded", bundle.get("provider") in PROVIDER_NAMES and bool(bundle.get("adapter_version")), "provider or adapter version missing")
        require("plan_version_recorded", bool(bundle.get("query_plan_version")), "query plan version missing")
        require("coverage_recorded", bool(bundle.get("coverage_version") and bundle.get("thesis_version")), "coverage or thesis version missing")
        require("unique_queries", len({item["query_id"] for item in plan}) == len(plan), "duplicate query in run")
        executed_queries = {item.get("query_id") for item in pages}
        primary_queries = {query_id for query_id in executed_queries if str(query_id).startswith("primary_")}
        require(
            "query_budget",
            len(plan) <= limits.get("max_queries", 0)
            and len(executed_queries) <= (
                limits.get("max_total_queries", limits.get("max_queries", 0))
                + limits.get("max_news_queries", 0)
            ),
            "query budget exceeded",
        )
        require("primary_backtrace_budget", len(primary_queries) <= limits.get("max_primary_backtraces", 0), "primary-source backtrace budget exceeded")
        require("result_budget", all(len(item.get("results", [])) <= limits.get("max_results_per_query", 0) for item in pages), "result budget exceeded")
        require(
            "triage_budget",
            len(triage) <= limits.get("max_triage", 0),
            "triage candidate budget exceeded",
        )
        require("fetch_budget", len(fetches) <= limits.get("max_fetch", 0), "fetch budget exceeded")
        routing = bundle.get("provider_routing_audit") or []
        news_pages = [item for item in pages if item.get("provider") == "thenewsapi"]
        non_news_pages = [item for item in pages if item.get("provider") != "thenewsapi"]
        search_request_count = (
            (
                sum(item.get("request_count", 0) for item in routing)
                if routing
                else sum(item.get("request_count", 0) for item in non_news_pages)
            )
            + sum(item.get("request_count", 0) for item in news_pages)
        )
        request_count = search_request_count + sum(item.get("request_count", 0) for item in fetches)
        require("request_budget", request_count <= limits.get("max_requests", 0), "request budget exceeded")
        fixed_endpoints = {
            "brave": BRAVE_WEB_ENDPOINT, "tavily": TAVILY_ENDPOINT, "exa": EXA_ENDPOINT,
            "you": YOU_ENDPOINT, "searxng": SEARXNG_ENDPOINT,
            "thenewsapi": THENEWSAPI_ENDPOINT,
        }
        def approved_endpoint(item):
            provider, endpoint = item.get("provider"), item.get("endpoint", "")
            if provider == "gemini_google":
                return endpoint.startswith(GEMINI_BASE + "/") and endpoint.endswith(":generateContent") and "?" not in endpoint
            return provider in fixed_endpoints and endpoint == fixed_endpoints[provider]
        require("approved_search_endpoint", all(approved_endpoint(item) for item in pages), "search request used unapproved endpoint")
        fallback_counts: dict[str, int] = {}
        for item in routing:
            if item.get("fallback"):
                fallback_counts[item.get("query_id", "")] = fallback_counts.get(item.get("query_id", ""), 0) + 1
        require("fallback_budget", all(count <= 1 for count in fallback_counts.values()), "provider fallback budget exceeded")
        require("routing_providers_known", all(item.get("provider") in PROVIDER_NAMES for item in routing), "unknown provider in routing audit")
        news_requested = bool((bundle.get("request") or {}).get("news"))
        require(
            "news_provider_recorded",
            not news_requested or bundle.get("news_provider") == "thenewsapi",
            "news provider missing or unknown",
        )
        require(
            "news_query_budget",
            len(news_pages) <= limits.get("max_news_queries", 0),
            "news query budget exceeded",
        )
        require(
            "news_result_budget",
            all(
                len(item.get("results", [])) <= limits.get("max_news_results_per_query", 0)
                for item in news_pages
            ),
            "news result budget exceeded",
        )
        require(
            "news_single_page",
            all((item.get("request_metadata") or {}).get("page") == 1 for item in news_pages),
            "news provider pagination is not fixed to page one",
        )

        def valid_news_window(item):
            metadata = item.get("request_metadata") or {}
            try:
                start = datetime.fromisoformat(str(metadata["published_after"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(metadata["published_before"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                return False
            days = limits.get("news_lookback_days", 0)
            return start < end and 0 < (end - start).total_seconds() <= days * 86400

        require(
            "news_date_window",
            all(valid_news_window(item) for item in news_pages),
            "news query date window missing or unbounded",
        )
        serialized_bundle = json.dumps(bundle, ensure_ascii=False, sort_keys=True, default=str)
        require(
            "news_token_absent",
            "api_token" not in serialized_bundle.casefold(),
            "news API token parameter leaked into audit bundle",
        )
        require(
            "news_snippet_discovery_only",
            all(
                (hit.get("source_metadata") or {}).get("snippet_policy") == "DISCOVERY_ONLY"
                and (hit.get("source_metadata") or {}).get("full_text_available") is False
                for page in news_pages for hit in page.get("results", [])
            ),
            "news snippet or full-text boundary invalid",
        )
        require("snippet_not_evidence", all(item.get("contract", {}).get("snippet_is_evidence") is False for item in triage), "search snippet treated as evidence")
        allowed_urls = {item["canonical_url"] for item in results}
        require("fetch_origin", all(item.get("canonical_url") in allowed_urls for item in fetches), "fetched URL missing search provenance")
        successful = [item for item in fetches if item.get("status") == FetchStatus.SUCCESS.value]
        require("fetch_security", all(all(item.get("security_checks", {}).values()) for item in successful), "successful fetch missing security checks")
        require("successful_fetch_has_text", all(bool(item.get("raw_text") and item.get("content_hash")) for item in successful), "successful fetch lacks complete text or content hash")
        require(
            "deterministic_tiers",
            all(classify_url(item["canonical_url"], self.policy)[0].value == item["source_tier"] for item in results),
            "source tier does not match policy",
        )
        decisions_by_candidate = {item["candidate_id"]: item for item in decisions}
        failed_candidates = {item["candidate_id"] for item in fetches if item.get("status") != FetchStatus.SUCCESS.value}
        require("failed_fetch_not_ingested", all(decisions_by_candidate.get(candidate, {}).get("decision") != "CREATED" for candidate in failed_candidates), "failed fetch entered Document ingestion")
        tier_by_candidate = {item["candidate_id"]: item["source_tier"] for item in results}
        fetched_candidates = {item["candidate_id"] for item in fetches}
        require("t3_discovery_only", all(candidate not in fetched_candidates for candidate, tier in tier_by_candidate.items() if tier == ClassifiedTier.T3.value), "T3 lead was fetched instead of remaining discovery-only")
        require("unverified_not_fetched", all(candidate not in fetched_candidates for candidate, tier in tier_by_candidate.items() if tier == ClassifiedTier.UNVERIFIED.value), "unverified domain was fetched")
        require("t3_not_analyzed", all(not item.get("analysis_run_id") for item in decisions if tier_by_candidate.get(item["candidate_id"]) in {ClassifiedTier.T3.value, ClassifiedTier.UNVERIFIED.value}), "T3 or unverified result entered Analysis")
        require("duplicates_not_analyzed", all(not item.get("analysis_run_id") for item in decisions if item.get("decision") == "DUPLICATE"), "duplicate entered Analysis")
        analyzed_clusters = [item.get("event_cluster_key") for item in decisions if item.get("analysis_run_id")]
        require("one_analysis_per_event_cluster", len(analyzed_clusters) == len(set(analyzed_clusters)), "event cluster entered Analysis more than once")
        require("analysis_has_provenance", all(item.get("document_id") and item.get("query_ids") for item in decisions if item.get("analysis_run_id")), "Analysis link lacks query and Document provenance")
        require("analysis_ops_passed", all(item.get("analysis_ops_passed") is True for item in decisions if item.get("analysis_run_id")), "linked Analysis Run did not pass its own Ops Check")
        allowed_payload_keys = {"candidate", "coverage_snapshot", "reverse_triggers", "thesis_snapshot"}
        require("triage_payload_scope", all(set(item.get("authorized_payload_keys", [])) == allowed_payload_keys for item in triage), "Discovery Triage payload exceeded authorization")
        require("discovery_has_no_email", not bundle.get("email_attempted", False) and bundle.get("email_auto_send_effective") is False, "Discovery attempted or enabled email delivery")
        artifact_paths = bundle.get("artifact_paths") or []
        require("artifacts_complete", len(artifact_paths) == 5 and all(Path(path).is_file() for path in artifact_paths), "required discovery artifacts missing")
        return {"passed": not errors, "version": self.version, "checks": checks, "errors": errors}
