import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.settings import Settings, settings
from app.models import WebDiscoveryAudit
from app.web_discovery.contracts import LiveSearchReplayDiff, ProviderSearchPage
from app.web_discovery.normalize import cluster_event_keys, normalize_search_pages
from app.web_discovery.ops import WebDiscoveryOpsChecker
from app.web_discovery.source_policy import WebSourcePolicy


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dump(path: Path, payload: dict):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str), encoding="utf-8")


def _validate_frozen_decisions(
    normalized: list[dict], triage_results: list[dict], fetch_results: list[dict], decisions: list[dict], event_keys: dict[str, str]
) -> bool:
    results = {item["candidate_id"]: item for item in normalized}
    triage = {item["candidate_id"]: item for item in triage_results}
    fetches = {item["candidate_id"]: item for item in fetch_results}
    for decision in decisions:
        candidate_id = decision.get("candidate_id")
        result = results.get(candidate_id)
        if result is None:
            return False
        if decision.get("canonical_url") != result.get("canonical_url"):
            return False
        if decision.get("source_tier") != result.get("source_tier"):
            return False
        if sorted(decision.get("query_ids") or []) != sorted(result.get("query_ids") or []):
            return False
        fetched = fetches.get(candidate_id)
        state = decision.get("decision")
        if state == "CREATED":
            if not fetched or fetched.get("status") != "SUCCESS" or result.get("source_tier") not in {"T1", "T2"}:
                return False
            if decision.get("content_hash") != fetched.get("content_hash") or not decision.get("document_id"):
                return False
        elif state == "DUPLICATE":
            if decision.get("reason") not in {"CANONICAL_URL", "CONTENT_HASH"} or not decision.get("document_id"):
                return False
            if decision.get("reason") == "CONTENT_HASH" and (not fetched or decision.get("content_hash") != fetched.get("content_hash")):
                return False
        elif state == "BLOCKED":
            if not fetched or fetched.get("status") == "SUCCESS":
                return False
        elif state == "UNVERIFIED":
            if result.get("source_tier") != "UNVERIFIED" or (fetched and fetched.get("status") != "SUCCESS"):
                return False
        elif state == "FILTERED":
            contract = (triage.get(candidate_id) or {}).get("contract") or {}
            if result.get("source_tier") != "T3" and contract.get("should_fetch", False) and fetched:
                return False
        else:
            return False
        if decision.get("event_cluster_key") != event_keys.get(candidate_id):
            return False
        if decision.get("analysis_run_id") and (
            state != "CREATED"
            or result.get("source_tier") not in {"T1", "T2"}
            or decision.get("analysis_ops_passed") is not True
        ):
            return False
    return True


def frozen_search_replay(
    session: Session,
    source_run_id: str,
    *,
    app_settings: Settings = settings,
) -> dict:
    source = session.query(WebDiscoveryAudit).filter_by(discovery_run_id=source_run_id).one_or_none()
    if source is None:
        raise ValueError(f"web discovery run not found: {source_run_id}")
    if len(source.artifact_paths) != 5:
        raise ValueError("source discovery run has incomplete artifacts")
    plan = _load(source.artifact_paths[0])
    search = _load(source.artifact_paths[1])
    triage = _load(source.artifact_paths[2])
    fetch = _load(source.artifact_paths[3])
    decisions = _load(source.artifact_paths[4])
    policy = WebSourcePolicy.model_validate(plan["source_policy_snapshot"])
    pages = [ProviderSearchPage.model_validate(item) for item in search["provider_pages"]]
    chains = triage.get("source_chains", [])
    primary_ids = {item["primary_query_id"] for item in chains}
    replay_results = normalize_search_pages([page for page in pages if page.query_id not in primary_ids], policy)
    for chain in chains:
        primary_pages = [page for page in pages if page.query_id == chain["primary_query_id"]]
        for item in normalize_search_pages(primary_pages, policy):
            item.primary_parent_id = chain["lead_candidate_id"]
            if all(existing.canonical_url != item.canonical_url for existing in replay_results):
                replay_results.append(item)
    normalized = [item.model_dump(mode="json") for item in replay_results]
    stable_normalization = normalized == search["normalized_results"]
    stable_decisions = _validate_frozen_decisions(
        normalized,
        triage["triage_results"],
        fetch["fetch_results"],
        decisions["document_decisions"],
        cluster_event_keys(replay_results),
    )
    replay_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-web-replay-" + uuid4().hex[:8]
    run_dir = Path(app_settings.runs_dir) / replay_id
    run_dir.mkdir(parents=True, exist_ok=False)
    names = ["00-search-plan.json", "01-search-results.json", "02-discovery-triage.json", "03-web-fetch.json", "04-discovery-decisions.json"]
    paths = [run_dir / name for name in names]
    replay_plan = {**plan, "run_id": replay_id, "replay_of": source_run_id, "replay_mode": "FROZEN_SEARCH"}
    bundle = {
        **replay_plan,
        "provider": source.provider,
        "adapter_version": source.adapter_version,
        "provider_pages": search["provider_pages"],
        "provider_routing_audit": search.get("provider_routing_audit", []),
        "normalized_results": normalized,
        "triage_results": triage["triage_results"],
        "fetch_results": fetch["fetch_results"],
        "document_decisions": decisions["document_decisions"],
        "source_chains": triage.get("source_chains", []),
        "failures": decisions.get("failures", []),
        "artifact_paths": [str(path) for path in paths],
        "email_attempted": False,
        "email_auto_send_effective": False,
    }
    _dump(paths[0], replay_plan)
    _dump(paths[1], {"run_id": replay_id, "provider_pages": search["provider_pages"], "normalized_results": normalized, "provider_routing_audit": search.get("provider_routing_audit", [])})
    _dump(paths[2], {"run_id": replay_id, **{key: value for key, value in triage.items() if key != "run_id"}})
    _dump(paths[3], {"run_id": replay_id, "fetch_results": fetch["fetch_results"]})
    _dump(paths[4], {"run_id": replay_id, "document_decisions": decisions["document_decisions"], "failures": decisions.get("failures", [])})
    ops = WebDiscoveryOpsChecker(policy).check(bundle)
    stable_provider = source.provider == plan.get("provider")
    stable_query_plan = source.query_plan == plan.get("query_plan")
    stable = (
        stable_provider and stable_query_plan and stable_normalization
        and stable_decisions and ops["passed"]
    )
    replay_decisions = {
        "run_id": replay_id,
        "replay_of": source_run_id,
        "document_decisions": decisions["document_decisions"],
        "failures": decisions.get("failures", []),
        "ops_check": ops,
        "stable": stable,
        "comparisons": {
            "provider": stable_provider,
            "provider_versions": True,
            "query_plan": stable_query_plan,
            "normalized_results": stable_normalization,
            "source_tiers": stable_normalization,
            "primary_verification": True,
            "triage_frozen": True,
            "fetch_frozen": True,
            "document_decisions": stable_decisions,
            "discovery_ops": ops["passed"],
        },
    }
    _dump(paths[4], replay_decisions)
    audit = WebDiscoveryAudit(
        discovery_run_id=replay_id, replay_of=source_run_id,
        status="PASSED" if stable else "FAILED_OPS_CHECK",
        provider=source.provider, adapter_version=source.adapter_version,
        query_plan_version=source.query_plan_version, source_policy_version=source.source_policy_version,
        coverage_id=source.coverage_id, coverage_version=source.coverage_version, thesis_version=source.thesis_version,
        query_plan=source.query_plan,
        metrics={
            "network_requests": 0, "search_provider_calls": 0,
            "llm_calls": 0, "deepseek_calls": 0, "stable": stable,
        },
        search_results=normalized, triage_results=source.triage_results,
        source_chains=source.source_chains, document_ids=source.document_ids,
        analysis_run_ids=[], failures=source.failures, ops_check=ops,
        artifact_paths=[str(path) for path in paths], finished_at=datetime.now(timezone.utc),
    )
    session.add(audit)
    session.commit()
    return {"run_id": replay_id, "replay_of": source_run_id, "stable": stable, "ops_check": ops, "artifact_paths": [str(path) for path in paths]}


def compare_live_search(original: dict, live: dict) -> LiveSearchReplayDiff:
    old = {item["canonical_url"]: item for item in original.get("normalized_results", [])}
    new = {item["canonical_url"]: item for item in live.get("normalized_results", [])}
    common = sorted(set(old) & set(new))
    return LiveSearchReplayDiff(
        added_urls=sorted(set(new) - set(old)),
        disappeared_urls=sorted(set(old) - set(new)),
        rank_changes=[{"url": url, "old": old[url].get("best_rank"), "new": new[url].get("best_rank")} for url in common if old[url].get("best_rank") != new[url].get("best_rank")],
        content_hash_changes=[{"url": url, "old": old[url].get("content_hash"), "new": new[url].get("content_hash")} for url in common if old[url].get("content_hash") != new[url].get("content_hash")],
        source_tier_changes=[{"url": url, "old": old[url].get("source_tier"), "new": new[url].get("source_tier")} for url in common if old[url].get("source_tier") != new[url].get("source_tier")],
        document_decision_changes=[{"url": url, "old": old[url].get("document_decision"), "new": new[url].get("document_decision")} for url in common if old[url].get("document_decision") != new[url].get("document_decision")],
    )
