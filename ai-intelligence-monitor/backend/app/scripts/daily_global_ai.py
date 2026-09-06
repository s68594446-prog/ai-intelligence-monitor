import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import yaml

from app.core.settings import Settings
from app.daily_ai.analysis import DeepSeekAShareAiAnalysisAdapter
from app.daily_ai.contracts import AShareUniverse
from app.daily_ai.digest import render_a_share_section, select_a_share_digest
from app.daily_ai.discovery import keyword_prefilter, official_source
from app.daily_ai.global_analysis import (
    AnalysisOutputError,
    EvidenceMismatch,
    ProviderCallError,
)
from app.daily_ai.global_digest import render_global_digest, select_global_digest
from app.daily_ai.department_harness import (
    CONTENT_DEPARTMENTS,
    DepartmentCandidate,
    DepartmentId,
    DepartmentInput,
    DepartmentManifest,
    DepartmentManifestEntry,
    DepartmentOpsResult,
    DepartmentStatus,
    ProviderUsage,
    allocate_department_request_budgets,
    artifact_sha256,
    delivery_gate,
    department_status,
    evidence_review,
    write_content_department_artifacts,
    write_stage_department_artifacts,
)
from app.daily_ai.gemini_analysis import (
    GeminiGlobalAiAnalysisAdapter,
    GeminiModelController,
    GeminiRequestGovernor,
)
from app.daily_ai.global_ops import (
    advice_violations,
    check_global_channel,
    neutrality_violations,
    numeric_claim_cited,
    output_language_violations,
    output_quality_violations,
    sourcing_matches_tier,
    sourcing_tier_diagnostics,
)
from app.daily_ai.juhe import JuheAiBriefAdapter, JuheError, JuheHeadlinesAdapter
from app.daily_ai.ops import check_a_share_channel
from app.daily_ai.pdf import SafeOfficialPdfFetcher
from app.daily_ai.quality import out_of_scope_headline
from app.daily_ai.rss import RssDiscoveryAdapter, RssError
from app.daily_ai.source_history import load_recent_source_history
from app.daily_ai.us_macro_discovery import (
    MacroDiscoveryError,
    TreasuryPressReleaseDiscoveryAdapter,
)
from app.pipeline.errors import LLMAdapterTimeout, LLMOutputValidationError, LLMProviderError
from app.scripts.daily_a_share_ai import KEYWORDS as A_SHARE_KEYWORDS
from app.scripts.daily_a_share_ai import SITES as A_SHARE_SITES
from app.scripts.daily_a_share_ai import _candidate as a_share_candidate
from app.web_discovery.contracts import ClassifiedTier, SearchQuery
from app.web_discovery.fetch import WebPageFetcher
from app.web_discovery.normalize import cluster_event_keys, normalize_search_pages
from app.web_discovery.providers import SearXNGWebSearchAdapter
from app.web_discovery.query_plan import load_query_plan
from app.web_discovery.security import canonicalize_url
from app.web_discovery.source_policy import load_source_policy


CONFIG_DIR = Path(__file__).resolve().parents[3] / "config/daily_ai"
QUERY_PLAN_PATH = CONFIG_DIR / "cn_ai_queries.yaml"
SOURCE_POLICY_PATH = CONFIG_DIR / "daily_ai_source_policy.yaml"
RSS_SOURCES_PATH = CONFIG_DIR / "rss_sources.yaml"
KEYWORDS_PATH = CONFIG_DIR / "industry_keywords.yaml"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/daily_global_ai_analysis_v9.txt"
CHINA_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/china_supply_chain_department_v1.txt"
GLOBAL_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/global_supply_chain_department_v1.txt"
MACRO_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/daily_us_macro_analysis_v1.txt"
PROJECT_DIR = Path(__file__).resolve().parents[3]
SOURCE_HISTORY_PATH = PROJECT_DIR.parent / "digests/.source-history.jsonl"
FETCH_MAX_BYTES = 5_000_000
MIN_PROVIDER_RESPONSE_COVERAGE = 0.80
# A T1 page we cannot fetch (403 / access challenge) may still be reachable
# through a T2 retelling of the SAME event cluster. That substitute is fetched
# normally and analysed as SECONDHAND_REPORT with its original source named —
# no anti-bot bypass, no relaxed gate.
BLOCKED_FALLBACK_CODES = {"HTTP_403", "ACCESS_CHALLENGE", "HTTP_401", "HTTP_429"}


def load_industry_keywords() -> list[str]:
    data = yaml.safe_load(KEYWORDS_PATH.read_text(encoding="utf-8"))
    return list(data["zh"]) + list(data["en"])

# Board-balancing is a fetch-budget concern only; the final GLOBAL/CHINA
# section of each item is still decided by the analysis contract from the
# event's actors, never by the article's origin.
CHINA_FETCH_SHARE = 0.5
ARXIV_FETCH_CAP = 3
MACRO_FETCH_CAP = 4
MACRO_ANALYZE_CAP = 4
DOMAIN_FETCH_CAP = 6
US_MACRO_DOMAINS = {
    "home.treasury.gov",
    "treasurydirect.gov",
    "federalreserve.gov",
}
PUBLISHER_SUFFIXES = {
    "nvidia.com",
    "google.com",
    "microsoft.com",
    "openai.com",
    "treasury.gov",
    "federalreserve.gov",
    "treasurydirect.gov",
}
CN_BOARD_DOMAINS = {
    "36kr.com", "qbitai.com", "jiqizhixin.com", "leiphone.com", "caixin.com",
    "baidu.com", "aliyun.com", "tencent.com", "bytedance.com", "volcengine.com",
    "huawei.com", "iflytek.com", "minimaxi.com", "deepseek.com",
}


def _is_cn_domain(domain: str) -> bool:
    host = domain.casefold()
    return host.endswith(".cn") or any(host == d or host.endswith("." + d) for d in CN_BOARD_DOMAINS)


def _is_us_macro_domain(domain: str) -> bool:
    host = domain.casefold()
    return any(host == value or host.endswith("." + value) for value in US_MACRO_DOMAINS)


def _department_for_candidate(candidate) -> DepartmentId:
    if _is_us_macro_domain(candidate.source_domain):
        return DepartmentId.MACRO
    if _is_cn_domain(candidate.source_domain):
        return DepartmentId.CHINA
    return DepartmentId.GLOBAL


def _publisher_key(domain: str) -> str:
    host = domain.casefold().strip(".")
    return next(
        (suffix for suffix in PUBLISHER_SUFFIXES if host == suffix or host.endswith("." + suffix)),
        host,
    )


def _take_domain_diverse(
    candidates: list,
    limit: int,
    *,
    cap: int = DOMAIN_FETCH_CAP,
    existing: list | None = None,
) -> list:
    """Bound one publisher without changing source-tier or evidence rules."""

    if limit <= 0 or cap <= 0:
        return []
    selected = []
    counts: dict[str, int] = {}
    for item in existing or []:
        domain = _publisher_key(item.source_domain)
        counts[domain] = counts.get(domain, 0) + 1
    for item in candidates:
        domain = _publisher_key(item.source_domain)
        if counts.get(domain, 0) >= cap:
            continue
        selected.append(item)
        counts[domain] = counts.get(domain, 0) + 1
        if len(selected) >= limit:
            break
    return selected

def _write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def source_acquisition_checks(*, discovery_status: dict[str, str], fetch_requested: bool,
                              planned_fetch_count: int, fetched_count: int) -> dict[str, bool]:
    """An unavailable discovery/fetch stage is not a healthy empty news day.

    One optional feed failing still does not block other usable sources. A
    successful discovery with no eligible candidates remains a legal empty day.
    """
    return {
        "source_discovery_usable": any(status == "SUCCESS" for status in discovery_status.values()),
        "source_fetch_usable": not fetch_requested or planned_fetch_count == 0 or fetched_count > 0,
    }


def provider_response_coverage_sufficient(*, attempts: int, validated: int) -> bool:
    """Allow isolated terminal transport failures, block broadly unusable output.

    Evidence/language rejections happen after schema validation and have their
    own gates. This ratio measures provider transport/structure usability only.
    """
    return attempts == 0 or validated / attempts >= MIN_PROVIDER_RESPONSE_COVERAGE


def _args():
    parser = argparse.ArgumentParser(description="Daily AI digest channel (domestic network); default is offline dry-run, never sends email")
    parser.add_argument("--discover", action="store_true", help="call the local SearXNG provider (local machine only)")
    parser.add_argument("--rss", action="store_true", help="RSS feed discovery (works on GitHub Actions)")
    parser.add_argument("--juhe", action="store_true", help="Juhe ID235/ID850 discovery (needs enabled flags and keys)")
    parser.add_argument("--fetch", action="store_true", help="safe-fetch T1/T2 candidates (requires --discover or --rss)")
    parser.add_argument("--analyze", action="store_true", help="Gemini analysis of fetched text (requires --fetch)")
    parser.add_argument("--render", action="store_true", help="render the digest (requires --analyze; empty digest is legal)")
    parser.add_argument("--a-share", action="store_true", help="run the A-share official-announcement section (requires --fetch)")
    parser.add_argument("--send", action="store_true", help="email delivery; fails closed, not configured in this phase")
    parser.add_argument("--max-queries", type=int, default=6)
    parser.add_argument("--max-results", type=int, default=3)
    parser.add_argument("--max-fetch", type=int, default=3)
    parser.add_argument("--max-analyze", type=int, default=2)
    parser.add_argument(
        "--max-provider-requests", type=int, default=None,
        help="explicit total Gemini HTTP ceiling including retries; defaults to legacy --max-analyze",
    )
    parser.add_argument("--lookback-hours", type=int, default=48, help="only items published within this window (unknown dates kept)")
    args = parser.parse_args()
    if args.max_provider_requests is None:
        args.max_provider_requests = args.max_analyze
    if args.send:
        print("status=BLOCKED")
        print("error_code=EMAIL_NOT_CONFIGURED")
        raise SystemExit(2)
    if args.fetch and not (args.discover or args.rss):
        raise SystemExit("--fetch requires --discover or --rss")
    if args.analyze and not args.fetch:
        raise SystemExit("--analyze requires --fetch")
    if args.render and not args.analyze:
        raise SystemExit("--render requires --analyze")
    if args.a_share and not args.fetch:
        raise SystemExit("--a-share requires --fetch")
    if not 1 <= args.max_queries <= 13 or not 1 <= args.max_results <= 5 or not 1 <= args.max_fetch <= 60 or not 1 <= args.max_analyze <= 40 or not 1 <= args.lookback_hours <= 96:
        raise SystemExit("budgets out of bounds")
    if not 1 <= args.max_provider_requests <= 80:
        raise SystemExit("provider request budget out of bounds (1-80)")
    return args


def _a_share_stage(*, settings, run_dir, now, analyze: bool, china_existing_count: int):
    universe = AShareUniverse.model_validate_json((CONFIG_DIR / "a_share_universe.json").read_text())
    search = SearXNGWebSearchAdapter(image_digest=settings.searxng_image_digest, timeout_seconds=settings.web_search_timeout_seconds, max_retries=0)
    after = (now - timedelta(hours=48)).date().isoformat()
    compact_terms = "(人工智能 OR 大模型 OR 算力 OR AI芯片 OR 机器人)"
    pages, candidates = [], {}
    try:
        for name, domain in A_SHARE_SITES.items():
            query = SearchQuery(query_id=f"a_share_{name}", text=f"site:{domain} {compact_terms} after:{after}", variable="a_share_ai_announcements", purpose="official_announcement_discovery", language="zh", time_range="48h", plan_version="a-share-ai-query-v1")
            page = search.search(query, count=3)
            pages.append(page)
            for hit in page.results:
                if official_source(hit.url) and keyword_prefilter(hit.title + " " + hit.snippet, A_SHARE_KEYWORDS):
                    item = a_share_candidate(hit, query.query_id)
                    candidates.setdefault(item.canonical_url, item)
    finally:
        search.close()
    fetch_client = httpx.Client(timeout=settings.web_search_timeout_seconds, follow_redirects=False, trust_env=False)
    html_fetcher = WebPageFetcher(client=fetch_client, max_bytes=2_000_000, max_retries=0, minimum_interval_seconds=1)
    pdf_fetcher = SafeOfficialPdfFetcher(client=fetch_client)
    sources, blocked, analyses = [], [], []
    company_by_name = sorted(universe.constituents, key=lambda item: -len(item.company_name))
    try:
        for candidate in list(candidates.values())[:3]:
            if candidate.canonical_url.lower().split("?", 1)[0].endswith(".pdf"):
                result = pdf_fetcher.fetch(candidate.canonical_url)
                if result.status != "SUCCESS":
                    blocked.append({"url": candidate.canonical_url, "error_code": result.error_code})
                    continue
                raw_text = result.text
            else:
                result = html_fetcher.fetch(candidate, allowed_urls=set(candidates))
                if result.status.value != "SUCCESS":
                    blocked.append({"url": candidate.canonical_url, "error_code": result.error_code})
                    continue
                raw_text = result.raw_text
            sources.append({"url": candidate.canonical_url, "title": candidate.title, "raw_text": raw_text})
            company = next((item for item in company_by_name if item.company_name in raw_text or item.company_name in candidate.title), None)
            if company and analyze and settings.deepseek_api_key and len(analyses) < 1:
                analyzer = DeepSeekAShareAiAnalysisAdapter(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url, model=settings.deepseek_model, timeout_seconds=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries)
                try:
                    analyses.append(analyzer.analyze(metadata={"company_name": company.company_name, "security_code": company.security_code, "exchange": company.exchange, "title": candidate.title}, raw_text=raw_text, source_url=candidate.canonical_url))
                except Exception as exc:  # fail-soft: A-share analysis failure never aborts the run
                    blocked.append({"url": candidate.canonical_url, "error_code": f"ANALYSIS_{type(exc).__name__}", "detail": str(exc)[:300]})
    finally:
        fetch_client.close()
    items = select_a_share_digest(analyses, china_existing_count=china_existing_count)
    ops = check_a_share_channel(tian=None, analyses=analyses, raw_text_by_url={item["url"]: item["raw_text"] for item in sources}, digest_items=items)
    _write(run_dir / "05-a-share.json", {
        "universe_status": universe.status, "search_queries": len(pages),
        "search_results": sum(len(p.results) for p in pages), "candidates": len(candidates),
        "fetched": [{k: v for k, v in item.items() if k != "raw_text"} for item in sources],
        "blocked": blocked, "analyzed": len(analyses),
        "analyses": [item.model_dump(mode="json") for item in analyses],
        "digest_items": items, "ops": ops.model_dump(mode="json"),
    })
    return items, ops, {"a_share_results": sum(len(p.results) for p in pages), "a_share_candidates": len(candidates), "a_share_fetched": len(sources), "a_share_blocked": len(blocked), "a_share_analyzed": len(analyses)}


def run():
    args = _args()
    settings = Settings()
    now = datetime.now(timezone.utc)
    report_day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    source_history = load_recent_source_history(
        SOURCE_HISTORY_PATH,
        current_day=report_day,
    )
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-daily-ai"
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    plan = load_query_plan(QUERY_PLAN_PATH)
    policy = load_source_policy(SOURCE_POLICY_PATH)
    queries = [
        SearchQuery(query_id=item.id, text=item.query, variable=item.variable, purpose=item.purpose,
                    language=item.language, time_range=item.time_range or plan.default_time_range,
                    plan_version=plan.plan_version)
        for item in plan.queries[: args.max_queries]
    ]
    _write(run_dir / "00-search-plan.json", {
        "run_id": run_id, "plan_version": plan.plan_version, "policy_version": policy.policy_version,
        "query_count": len(queries), "budgets": {"max_queries": args.max_queries, "max_results": args.max_results, "max_fetch": args.max_fetch, "max_analyze": args.max_analyze, "max_provider_requests": args.max_provider_requests},
        "queries": [q.model_dump(mode="json") for q in queries],
        "stages": {"discover": args.discover, "rss": args.rss, "juhe": args.juhe, "fetch": args.fetch, "analyze": args.analyze, "render": args.render, "a_share": args.a_share},
        "source_history": {
            "path": str(SOURCE_HISTORY_PATH),
            "records_loaded": source_history.records_loaded,
            "malformed_lines": source_history.malformed_lines,
            "window_days": 3,
        },
    })
    if not (args.discover or args.rss):
        print(json.dumps({"run_id": run_id, "status": "PLANNED", "plan": len(queries), "network_calls": 0, "gemini_calls": 0}, sort_keys=True))
        return

    pages = []
    discovery_status: dict[str, str] = {}
    if args.discover:
        search = SearXNGWebSearchAdapter(image_digest=settings.searxng_image_digest, timeout_seconds=settings.web_search_timeout_seconds, max_retries=0)
        try:
            for query in queries:
                pages.append(search.search(query, count=args.max_results))
            discovery_status["searxng"] = "SUCCESS"
        finally:
            search.close()
    feed_prefilter: dict[str, bool] = {}
    if args.rss:
        feeds = yaml.safe_load(RSS_SOURCES_PATH.read_text(encoding="utf-8"))["feeds"]
        rss = RssDiscoveryAdapter(timeout_seconds=settings.web_search_timeout_seconds)
        try:
            for feed in feeds:
                feed_prefilter[f"rss_{feed['name']}"] = bool(feed.get("prefilter", True))
                query = SearchQuery(query_id=f"rss_{feed['name']}", text=f"rss:{feed['url']}", variable="daily_cn_ai", purpose="rss_discovery", language="zh", time_range="48h", plan_version=plan.plan_version)
                try:
                    pages.append(rss.fetch_feed(feed_name=feed["name"], feed_url=feed["url"], max_items=int(feed.get("max_items", 10)), query=query))
                    discovery_status[f"rss_{feed['name']}"] = "SUCCESS"
                except RssError as exc:
                    discovery_status[f"rss_{feed['name']}"] = f"{exc.code.value}:{exc.detail}" if exc.detail else exc.code.value
        finally:
            rss.close()
        # Treasury has no reliable all-press-release RSS feed. Discover only
        # bounded debt-management titles from its fixed official listing; the
        # listing is never evidence and qualifying article pages are fetched
        # and validated through the normal path below.
        macro_query = SearchQuery(
            query_id="official_treasury_press_releases",
            text="official treasury financing releases",
            variable="us_treasury_financing",
            purpose="official debt-management events",
            language="en",
            time_range="48h",
            plan_version=plan.plan_version,
        )
        feed_prefilter[macro_query.query_id] = False
        treasury_releases = TreasuryPressReleaseDiscoveryAdapter(
            timeout_seconds=settings.web_search_timeout_seconds
        )
        try:
            pages.append(treasury_releases.search(macro_query, max_items=4))
            discovery_status[macro_query.query_id] = "SUCCESS"
        except MacroDiscoveryError as exc:
            discovery_status[macro_query.query_id] = (
                f"{exc.code.value}:{exc.detail}" if exc.detail else exc.code.value
            )
        finally:
            treasury_releases.close()
    if args.juhe:
        juhe_query = SearchQuery(query_id="daily_cn_ai_juhe", text="中国 人工智能 科技", variable="daily_cn_ai", purpose="discovery", language="zh", time_range="24h", plan_version=plan.plan_version)
        for name, adapter_cls, enabled, key in (
            ("juhe_headlines", JuheHeadlinesAdapter, settings.juhe_headlines_enabled, settings.juhe_headlines_api_key),
            ("juhe_ai_brief", JuheAiBriefAdapter, settings.juhe_ai_brief_enabled, settings.juhe_ai_brief_api_key),
        ):
            if not enabled:
                discovery_status[name] = "DISABLED"
                continue
            try:
                adapter = adapter_cls(api_key=key, timeout_seconds=settings.web_search_timeout_seconds)
                pages.append(adapter.search(juhe_query, count=3))
                adapter.close()
                discovery_status[name] = "SUCCESS"
            except JuheError as exc:
                discovery_status[name] = exc.code.value
    normalized = normalize_search_pages(pages, policy)
    clusters = cluster_event_keys(normalized)

    industry_keywords = load_industry_keywords()

    def passes_prefilter(item) -> bool:
        # Sector-dedicated feeds skip keyword matching; general sources require
        # a bilingual industry keyword hit. Non-RSS discovery defaults to filtering.
        if any(not feed_prefilter.get(query_id, True) for query_id in item.query_ids):
            return True
        return keyword_prefilter(item.title + " " + item.snippet, industry_keywords)

    # Freshness window: drop candidates whose feed-reported publish time falls
    # outside the lookback window; items without a parseable date are kept
    # (feeds are chronological and per-feed max_items already bounds staleness).
    cutoff = now - timedelta(hours=args.lookback_hours)
    published_by_url: dict[str, datetime] = {}
    for page in pages:
        for hit in page.results:
            if hit.published_at is not None:
                try:
                    published_by_url.setdefault(canonicalize_url(hit.url), hit.published_at)
                except ValueError:
                    continue

    def is_fresh(item) -> bool:
        published = published_by_url.get(item.canonical_url)
        return published is None or published >= cutoff

    # Source-level OUT_OF_SCOPE exclusion: stock-quote and broker/banking news
    # is dropped by its discovery title BEFORE fetch and analysis, so it never
    # enters the pipeline, never spends budget, and never becomes an offender.
    out_of_scope = []
    def in_scope(item) -> bool:
        term = out_of_scope_headline(item.title)
        if term:
            out_of_scope.append({"title": item.title[:120], "matched_term": term, "url": item.canonical_url})
            return False
        return True

    candidates = [
        item for item in normalized
        if item.source_tier in {ClassifiedTier.T1, ClassifiedTier.T2}
        and passes_prefilter(item) and is_fresh(item) and in_scope(item)
    ]
    stale_dropped = sum(
        1 for item in normalized
        if item.source_tier in {ClassifiedTier.T1, ClassifiedTier.T2} and passes_prefilter(item) and not is_fresh(item)
    )
    candidates.sort(key=lambda item: (0 if item.source_tier == ClassifiedTier.T1 else 1, item.best_rank))
    # Fetch-budget balancing: reserve a small official U.S. macro slice and a
    # China share, cap arXiv, and cap any one publisher.  These are discovery
    # diversity controls only; every item still faces the same evidence and
    # importance gates, so no positive/negative quota is manufactured.
    macro_pool = [item for item in candidates if _is_us_macro_domain(item.source_domain)]
    cn_pool = [item for item in candidates if _is_cn_domain(item.source_domain)]
    arxiv_pool = [item for item in candidates if item not in cn_pool and item not in macro_pool and (item.source_domain == "arxiv.org" or item.source_domain.endswith(".arxiv.org"))]
    global_pool = [item for item in candidates if item not in cn_pool and item not in arxiv_pool and item not in macro_pool]
    macro_budget = min(len(macro_pool), MACRO_FETCH_CAP, args.max_fetch)
    non_macro_budget = args.max_fetch - macro_budget
    cn_budget = min(len(cn_pool), round(non_macro_budget * CHINA_FETCH_SHARE))
    arxiv_budget = min(
        len(arxiv_pool),
        ARXIV_FETCH_CAP,
        max(0, non_macro_budget - cn_budget),
    )
    global_budget = max(0, non_macro_budget - cn_budget - arxiv_budget)
    fetch_plan = (
        _take_domain_diverse(macro_pool, macro_budget, cap=2)
        + _take_domain_diverse(global_pool, global_budget)
        + _take_domain_diverse(cn_pool, cn_budget)
        + arxiv_pool[:arxiv_budget]
    )
    spare = args.max_fetch - len(fetch_plan)
    if spare > 0:  # hand unused share back to whichever pool has leftovers (papers stay capped)
        leftovers = [item for item in global_pool + cn_pool if item not in fetch_plan]
        fetch_plan.extend(_take_domain_diverse(leftovers, spare, existing=fetch_plan))
    _write(run_dir / "01-search-results.json", {
        "run_id": run_id, "pages": [p.model_dump(mode="json") for p in pages],
        "discovery_status": discovery_status,
        "normalized_count": len(normalized), "eligible_t1_t2": len(candidates),
        "out_of_scope_excluded": out_of_scope,
        "event_clusters": clusters,
        "tier_counts": {tier.value: sum(item.source_tier == tier for item in normalized) for tier in ClassifiedTier},
    })

    sources, blocked, analyses, analysis_failures = [], [], [], []
    cross_day_duplicates = []
    analysis_urls: dict[str, str] = {}
    analyze_attempts = 0
    analysis_candidate_count = 0
    provider_validated_outputs = 0
    macro_analyze_attempts = 0
    macro_provider_validated_outputs = 0
    provider_request_attempts = 0
    provider_halted = False
    provider_budget_exhausted = False
    department_by_url: dict[str, DepartmentId] = {}
    department_candidates: dict[DepartmentId, list[DepartmentCandidate]] = {
        department: [] for department in CONTENT_DEPARTMENTS
    }
    department_attempts = {department: 0 for department in CONTENT_DEPARTMENTS}
    department_validated = {department: 0 for department in CONTENT_DEPARTMENTS}
    department_failures: dict[DepartmentId, list[str]] = {
        department: [] for department in CONTENT_DEPARTMENTS
    }
    department_halted = {department: False for department in CONTENT_DEPARTMENTS}
    model_controller = GeminiModelController(
        primary_model=settings.gemini_analysis_model,
        fallback_model=settings.gemini_fallback_model,
        fallback_enabled=settings.gemini_quota_fallback_enabled,
    )
    department_budgets = {department: 0 for department in CONTENT_DEPARTMENTS}
    analysis_jobs = []
    analyzers: dict[DepartmentId, GeminiGlobalAiAnalysisAdapter | None] = {
        department: None for department in CONTENT_DEPARTMENTS
    }
    analyzer_errors: dict[DepartmentId, dict | None] = {
        department: None for department in CONTENT_DEPARTMENTS
    }
    if args.fetch:
        fetch_client = httpx.Client(timeout=settings.web_search_timeout_seconds, follow_redirects=False, trust_env=False)
        fetcher = WebPageFetcher(client=fetch_client, max_bytes=FETCH_MAX_BYTES, max_retries=0, minimum_interval_seconds=1)
        allowed = {item.canonical_url for item in candidates}
        by_id = {item.candidate_id: item for item in candidates}
        planned_urls = {item.canonical_url for item in fetch_plan}
        try:
            for candidate in list(fetch_plan):
                result = fetcher.fetch(candidate, allowed_urls=allowed)
                if result.status.value != "SUCCESS" and result.error_code in BLOCKED_FALLBACK_CODES:
                    cluster = clusters.get(candidate.candidate_id)
                    substitute = next(
                        (
                            by_id[other_id] for other_id, key in clusters.items()
                            if key == cluster and other_id != candidate.candidate_id
                            and other_id in by_id
                            and by_id[other_id].source_tier == ClassifiedTier.T2
                            and by_id[other_id].canonical_url not in planned_urls
                        ),
                        None,
                    )
                    if substitute is not None:
                        blocked.append({"url": candidate.canonical_url, "tier": candidate.source_tier.value, "status": result.status.value, "error_code": result.error_code, "fallback_to": substitute.canonical_url})
                        planned_urls.add(substitute.canonical_url)
                        candidate = substitute
                        result = fetcher.fetch(candidate, allowed_urls=allowed)
                if result.status.value != "SUCCESS":
                    blocked.append({"url": candidate.canonical_url, "tier": candidate.source_tier.value, "status": result.status.value, "error_code": result.error_code})
                    continue
                sources.append({"url": candidate.canonical_url, "tier": candidate.source_tier.value, "title": result.title or candidate.title, "sha256": result.response_sha256, "content_sha256": result.content_hash, "raw_text": result.raw_text})
                # Compare the safely fetched text hash with the committed
                # three-day publication history before spending Gemini quota.
                # A changed page is analyzed normally; only the same canonical
                # URL with the same content hash is skipped.
                if source_history.has_same_content(
                    candidate.canonical_url,
                    result.content_hash,
                ):
                    cross_day_duplicates.append(
                        {
                            "url": candidate.canonical_url,
                            "content_sha256": result.content_hash,
                            "reason_code": "CROSS_DAY_SAME_URL_AND_CONTENT",
                        }
                    )
                    continue
                active_department = _department_for_candidate(candidate)
                department_by_url[candidate.canonical_url] = active_department
                department_candidates[active_department].append(
                    DepartmentCandidate(
                        candidate_id=candidate.candidate_id,
                        canonical_url=candidate.canonical_url,
                        title=result.title or candidate.title,
                        source_domain=candidate.source_domain,
                        source_tier=candidate.source_tier.value,
                        event_cluster_key=clusters.get(
                            candidate.candidate_id, candidate.candidate_id
                        ),
                        content_sha256=result.content_hash,
                        raw_text=result.raw_text,
                    )
                )
                analysis_jobs.append((candidate, result, active_department))

            if args.analyze:
                analysis_candidate_count = len(analysis_jobs)
                department_budgets = allocate_department_request_budgets(
                    args.max_provider_requests,
                    candidate_counts={
                        department: len(department_candidates[department])
                        for department in CONTENT_DEPARTMENTS
                    },
                )
                governor = GeminiRequestGovernor(
                    minimum_interval_seconds=settings.gemini_min_request_interval_seconds,
                    max_run_seconds=settings.gemini_max_run_seconds,
                )
                supplemental_prompts = {
                    DepartmentId.CHINA: CHINA_PROMPT_PATH,
                    DepartmentId.GLOBAL: GLOBAL_PROMPT_PATH,
                    DepartmentId.MACRO: MACRO_PROMPT_PATH,
                }
                for department in CONTENT_DEPARTMENTS:
                    request_budget = department_budgets[department]
                    if not request_budget:
                        continue
                    try:
                        analyzers[department] = GeminiGlobalAiAnalysisAdapter(
                            api_key=settings.gemini_api_key,
                            base_url=settings.gemini_base_url,
                            model=settings.gemini_analysis_model,
                            prompt_path=PROMPT_PATH,
                            supplemental_prompt_path=supplemental_prompts[department],
                            timeout_seconds=settings.llm_timeout_seconds,
                            max_retries=settings.llm_max_retries,
                            max_provider_requests=request_budget,
                            governor=governor,
                            model_controller=model_controller,
                            rate_limit_cooldown_seconds=settings.gemini_rate_limit_cooldown_seconds,
                        )
                    except ValueError:
                        analyzer_errors[department] = {
                            "reason_code": "GEMINI_CONFIGURATION_ERROR",
                            "error_type": "ValueError",
                        }

                for candidate, result, active_department in analysis_jobs:
                    if provider_halted:
                        break
                    active_analyzer = analyzers[active_department]
                    request_budget = department_budgets[active_department]
                    if (
                        department_halted[active_department]
                        or request_budget == 0
                        or (
                            active_analyzer is not None
                            and active_analyzer.request_count >= request_budget
                        )
                    ):
                        if not department_halted[active_department]:
                            analysis_failures.append({
                                "url": candidate.canonical_url,
                                "error_code": "GEMINI_REQUEST_BUDGET_EXHAUSTED",
                                "diagnostics": {
                                    "reason_code": "GEMINI_REQUEST_BUDGET_EXHAUSTED",
                                    "halt_department": active_department.value,
                                },
                            })
                        department_halted[active_department] = True
                        continue

                    analyze_attempts += 1
                    department_attempts[active_department] += 1
                    is_macro_candidate = active_department == DepartmentId.MACRO
                    if is_macro_candidate:
                        macro_analyze_attempts += 1
                    if active_analyzer is None:
                        analysis_failures.append({
                            "url": candidate.canonical_url,
                            "error_code": "GEMINI_CONFIGURATION_ERROR",
                            "diagnostics": analyzer_errors[active_department] or {
                                "reason_code": "GEMINI_CONFIGURATION_ERROR"
                            },
                        })
                        department_halted[active_department] = True
                        continue
                    try:
                        analysis = active_analyzer.analyze(
                            metadata={
                                "title": result.title or candidate.title,
                                "source_domain": urlsplit(candidate.canonical_url).hostname or "",
                                "source_tier": candidate.source_tier.value,
                            },
                            raw_text=result.raw_text,
                            source_url=candidate.canonical_url,
                        )
                        provider_validated_outputs += 1
                        department_validated[active_department] += 1
                        if is_macro_candidate:
                            macro_provider_validated_outputs += 1
                        language_fields = output_language_violations(analysis)
                        quality_reasons = output_quality_violations(analysis)
                        scope_term = out_of_scope_headline(analysis.headline)
                        advice_terms = advice_violations(analysis)
                        non_neutral_terms = neutrality_violations(analysis)
                        if scope_term:
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "OUT_OF_SCOPE_MARKET_ACTION", "diagnostics": {"matched_term": scope_term, "headline": analysis.headline[:120]}})
                        elif advice_terms:
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "DISCIPLINE_ADVICE", "diagnostics": {"matched_terms": advice_terms, "headline": analysis.headline[:120]}})
                        elif non_neutral_terms:
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "NON_NEUTRAL_FRAMING", "diagnostics": {"matched_terms": non_neutral_terms, "headline": analysis.headline[:120]}})
                        elif language_fields:
                            analysis_failures.append({
                                "url": candidate.canonical_url,
                                "error_code": "OUTPUT_LANGUAGE_INVALID",
                                "diagnostics": {"fields": language_fields, "headline": analysis.headline[:120]},
                            })
                        elif quality_reasons:
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "LOW_TEXT_QUALITY", "diagnostics": {"reasons": quality_reasons, "headline": analysis.headline[:120]}})
                        elif not numeric_claim_cited(analysis):
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "NUMERIC_CLAIM_UNCITED", "diagnostics": {"headline": analysis.headline[:120], "quotes": [e.quote[:160] for e in analysis.evidence]}})
                        elif not sourcing_matches_tier(analysis, policy):
                            analysis_failures.append({
                                "url": candidate.canonical_url,
                                "error_code": "SOURCING_TIER_MISMATCH",
                                "diagnostics": sourcing_tier_diagnostics(analysis, policy),
                            })
                        else:
                            analyses.append(analysis)
                            analysis_urls[str(analysis.evidence[0].source_url)] = candidate.candidate_id
                    except EvidenceMismatch as exc:
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": "EVIDENCE_MISMATCH", "diagnostics": exc.diagnostics})
                    except AnalysisOutputError as exc:
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": exc.diagnostics.get("reason_code", type(exc).__name__), "diagnostics": exc.diagnostics})
                    except ProviderCallError as exc:
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": exc.diagnostics.get("reason_code", type(exc).__name__), "diagnostics": exc.diagnostics})
                        if exc.diagnostics.get("halt_provider_for_run"):
                            reason = exc.diagnostics.get("reason_code")
                            if reason == "GEMINI_REQUEST_BUDGET_EXHAUSTED":
                                department_halted[active_department] = True
                            else:
                                provider_halted = True
                    except (LLMOutputValidationError, LLMAdapterTimeout, LLMProviderError) as exc:
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": type(exc).__name__})
                    except Exception as exc:  # absolute fail-soft guard
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": f"UNEXPECTED_{type(exc).__name__}"})
        finally:
            provider_request_attempts = sum(
                item.request_count
                for item in analyzers.values()
                if item is not None
            )
            provider_budget_exhausted = provider_request_attempts >= args.max_provider_requests
            for item in analyzers.values():
                if item is not None:
                    item.close()
            fetch_client.close()
    provider_halted = provider_halted or any(department_halted.values())
    for failure in analysis_failures:
        department = department_by_url.get(failure.get("url", ""))
        if department is not None:
            department_failures[department].append(failure["error_code"])
    _write(run_dir / "02-fetch.json", {"run_id": run_id, "fetched": [{k: v for k, v in item.items() if k != "raw_text"} for item in sources], "blocked": blocked, "cross_day_duplicates": cross_day_duplicates})
    _write(run_dir / "03-analysis-output.json", [item.model_dump(mode="json") for item in analyses])
    _write(run_dir / "analysis-failures.json", analysis_failures)
    # Recovered request failures remain auditable without misclassifying a
    # successfully completed candidate as an unresolved provider failure.
    _write(run_dir / "provider-request-events.json", [
        {"department": department.value, **event}
        for department, analyzer in analyzers.items()
        if analyzer is not None
        for event in analyzer.request_events
    ])

    raw_by_url = {item["url"]: item["raw_text"] for item in sources}
    evidence_decisions = evidence_review(
        analyses,
        candidate_id_by_url=analysis_urls,
        raw_text_by_url=raw_by_url,
    )
    evidence_pass_urls = {
        decision.source_url
        for decision in evidence_decisions
        if decision.decision == "PASS"
    }
    analyses = [
        analysis
        for analysis in analyses
        if str(analysis.evidence[0].source_url) in evidence_pass_urls
    ]
    prompt_versions = {
        DepartmentId.CHINA: "china-supply-chain-department-v1",
        DepartmentId.GLOBAL: "global-supply-chain-department-v1",
        DepartmentId.MACRO: "daily-us-macro-analysis-v1",
    }
    content_statuses: dict[DepartmentId, DepartmentStatus] = {}
    department_accepted: dict[DepartmentId, int] = {}
    department_handoffs = []
    for department in CONTENT_DEPARTMENTS:
        accepted = [
            analysis
            for analysis in analyses
            if department_by_url.get(str(analysis.evidence[0].source_url)) == department
        ]
        department_accepted[department] = len(accepted)
        response_coverage_ok = provider_response_coverage_sufficient(
            attempts=department_attempts[department],
            validated=department_validated[department],
        )
        affects_completion = department_halted[department] or (
            provider_halted
            and department_attempts[department] < len(department_candidates[department])
        )
        status = department_status(
            analysis_candidate_count=len(department_candidates[department]),
            candidate_attempts=department_attempts[department],
            accepted_count=len(accepted),
            provider_halted=affects_completion,
            # A small number of terminal per-item transport failures is
            # fail-soft once the desk processed its pool with sufficient
            # validated response coverage. The complete audit is still kept.
            failure_codes=(department_failures[department]
                           if not response_coverage_ok else []),
        )
        content_statuses[department] = status
        active = analyzers[department]
        usage = ProviderUsage(
            request_budget=department_budgets[department],
            request_count=active.request_count if active else 0,
            candidate_attempts=department_attempts[department],
            validated_outputs=department_validated[department],
            models_used=tuple(active.models_used if active else ()),
            model_transitions=tuple(model_controller.transitions),
            provider_halted=affects_completion,
            failure_codes=tuple(sorted(set(department_failures[department]))),
        )
        department_input = DepartmentInput(
            run_id=run_id,
            department_id=department,
            department_prompt_version=prompt_versions[department],
            model_name=settings.gemini_analysis_model,
            request_budget=department_budgets[department],
            candidates=tuple(department_candidates[department]),
        )
        department_handoffs.append(
            write_content_department_artifacts(
                run_dir=run_dir,
                department_input=department_input,
                discovery=[
                    {
                        "candidate_id": candidate.candidate_id,
                        "canonical_url": candidate.canonical_url,
                        "source_tier": candidate.source_tier,
                    }
                    for candidate in department_candidates[department]
                ],
                fetches=[
                    {key: value for key, value in source.items() if key != "raw_text"}
                    for source in sources
                    if department_by_url.get(source["url"]) == department
                ],
                analyses=[analysis.model_dump(mode="json") for analysis in accepted],
                evidence=[
                    decision
                    for decision in evidence_decisions
                    if department_by_url.get(decision.source_url) == department
                ],
                provider_usage=usage,
                status=status,
            )
        )

    evidence_status = (
        DepartmentStatus.FAILED
        if any(decision.decision == "FAIL" for decision in evidence_decisions)
        else DepartmentStatus.HEALTHY
        if evidence_decisions
        else DepartmentStatus.HEALTHY_EMPTY
    )
    evidence_ops = DepartmentOpsResult(
        department_id=DepartmentId.EVIDENCE_REVIEW,
        status=evidence_status,
        passed=evidence_status in {DepartmentStatus.HEALTHY, DepartmentStatus.HEALTHY_EMPTY},
        checks={
            "analysis_immutable": True,
            "all_items_reviewed": len(evidence_decisions) == len(analyses),
            "all_selected_evidence_passed": not any(
                decision.decision == "FAIL" for decision in evidence_decisions
            ),
        },
        errors=tuple(
            decision.candidate_id
            for decision in evidence_decisions
            if decision.decision == "FAIL"
        ),
    )
    evidence_handoff = write_stage_department_artifacts(
        run_dir=run_dir,
        run_id=run_id,
        department_id=DepartmentId.EVIDENCE_REVIEW,
        status=evidence_status,
        input_payload={
            "contract_version": "phase3c-department-contract-v1",
            "upstream_handoffs": [handoff.model_dump(mode="json") for handoff in department_handoffs],
        },
        output_payload=[decision.model_dump(mode="json") for decision in evidence_decisions],
        ops=evidence_ops,
        downstream_consumer=DepartmentId.EDITORIAL.value,
        department_prompt_version="evidence-review-department-v1",
    )

    cluster_by_url = {url: clusters.get(candidate_id, candidate_id) for url, candidate_id in analysis_urls.items()}
    digest_items = select_global_digest(analyses, cluster_by_url=cluster_by_url) if args.render else []
    editorial_checks = {
        "evidence_handoff_accepted": evidence_status
        in {DepartmentStatus.HEALTHY, DepartmentStatus.HEALTHY_EMPTY},
        "sources_preserved": all(
            item.get("url") in evidence_pass_urls for item in digest_items
        ),
        "importance_threshold_preserved": all(
            int(item.get("importance_score", 0)) >= 60 for item in digest_items
        ),
    }
    editorial_status = (
        DepartmentStatus.BLOCKED
        if not editorial_checks["evidence_handoff_accepted"]
        else DepartmentStatus.HEALTHY
        if digest_items
        else DepartmentStatus.HEALTHY_EMPTY
    )
    editorial_ops = DepartmentOpsResult(
        department_id=DepartmentId.EDITORIAL,
        status=editorial_status,
        passed=editorial_status
        in {DepartmentStatus.HEALTHY, DepartmentStatus.HEALTHY_EMPTY}
        and all(editorial_checks.values()),
        checks=editorial_checks,
        errors=tuple(name for name, passed in editorial_checks.items() if not passed),
    )
    editorial_handoff = write_stage_department_artifacts(
        run_dir=run_dir,
        run_id=run_id,
        department_id=DepartmentId.EDITORIAL,
        status=editorial_status,
        input_payload={
            "contract_version": "phase3c-department-contract-v1",
            "evidence_handoff": evidence_handoff.model_dump(mode="json"),
            "analysis_sha256": [
                artifact_sha256(analysis.model_dump(mode="json"))
                for analysis in analyses
            ],
        },
        output_payload=digest_items,
        ops=editorial_ops,
        downstream_consumer=DepartmentId.DELIVERY.value,
        department_prompt_version="editorial-department-v1",
    )
    a_share_items, a_share_ops, a_share_counts = [], None, {}
    if args.a_share:
        china_count = sum(item["section"] == "CHINA" for item in digest_items)
        a_share_items, a_share_ops, a_share_counts = _a_share_stage(
            settings=settings, run_dir=run_dir, now=now, analyze=args.analyze, china_existing_count=china_count,
        )
    if args.render:
        actual_models = model_controller.models_used or [settings.gemini_analysis_model]
        analysis_model_label = " → ".join(actual_models)
        if model_controller.transitions:
            analysis_model_label += "（额度降级）"
        digest_text = render_global_digest(
            digest_items,
            analysis_model=analysis_model_label,
            prompt_version=GeminiGlobalAiAnalysisAdapter.prompt_version,
        )
        if args.a_share:
            digest_text += "\n\n" + render_a_share_section(a_share_items)
        (run_dir / "04-digest.txt").write_text(digest_text + "\n", encoding="utf-8")
    content_hash_by_url = {
        item["url"]: item.get("content_sha256") for item in sources
    }
    _write(
        run_dir / "published-source-history.json",
        [
            {
                "day": report_day.isoformat(),
                "canonical_url": item["url"],
                "content_sha256": content_hash_by_url[item["url"]],
            }
            for item in digest_items
            if content_hash_by_url.get(item["url"])
        ],
    )
    raw_by_url = {item["url"]: item["raw_text"] for item in sources}
    ops = check_global_channel(
        analyses=analyses, raw_text_by_url=raw_by_url, digest_items=digest_items, policy=policy,
        discarded_evidence_failures=sum(item["error_code"] == "EVIDENCE_MISMATCH" for item in analysis_failures),
        analysis_provider_required=args.analyze,
        analysis_attempts=analyze_attempts,
        provider_validated_outputs=provider_validated_outputs,
        fetched_count=len(sources),
        analysis_candidate_count=analysis_candidate_count,
        provider_halted=provider_halted,
        provider_failure_codes=[item["error_code"] for item in analysis_failures],
    )
    actual_models = model_controller.models_used or [settings.gemini_analysis_model]
    analysis_model_label = " → ".join(actual_models)
    fallback_used = bool(model_controller.transitions)
    if fallback_used:
        analysis_model_label += "（额度降级）"

    # Each content desk must report its own usable provider and completion
    # state. One successful desk cannot mask a halted or unprocessed peer.
    additional_checks = {
        f"{department.value}_analysis_provider_usable": (
            department_attempts[department] == 0
            or department_validated[department] > 0
        )
        for department in CONTENT_DEPARTMENTS
    }
    additional_checks.update(source_acquisition_checks(
        discovery_status=discovery_status, fetch_requested=args.fetch,
        planned_fetch_count=len(fetch_plan), fetched_count=len(sources),
    ))
    additional_checks.update(
        {
            f"{department.value}_provider_response_coverage_sufficient":
            provider_response_coverage_sufficient(
                attempts=department_attempts[department],
                validated=department_validated[department],
            )
            for department in CONTENT_DEPARTMENTS
        }
    )
    additional_checks.update(
        {
            f"{department.value}_analysis_execution_complete": not any(
                code.startswith("UNEXPECTED_")
                or code in {"LLMAdapterTimeout", "LLMProviderError"}
                for code in department_failures[department]
            )
            for department in CONTENT_DEPARTMENTS
        }
    )
    additional_checks.update(
        {
            f"{department.value}_analysis_language_usable": (
                department_validated[department] == 0
                or department_failures[department].count("OUTPUT_LANGUAGE_INVALID")
                < department_validated[department]
            )
            for department in CONTENT_DEPARTMENTS
        }
    )
    additional_checks.update(
        {
            f"{department.value}_analysis_semantic_output_usable": (
                department_attempts[department] == 0
                or department_accepted[department] > 0
            )
            for department in CONTENT_DEPARTMENTS
        }
    )
    additional_checks.update(
        {
            f"{department.value}_department_complete": (
                content_statuses[department]
                in {DepartmentStatus.HEALTHY, DepartmentStatus.HEALTHY_EMPTY}
            )
            for department in CONTENT_DEPARTMENTS
        }
    )
    additional_checks.update(
        {
            "evidence_review_complete": evidence_status
            in {DepartmentStatus.HEALTHY, DepartmentStatus.HEALTHY_EMPTY},
            "editorial_complete": editorial_status
            in {DepartmentStatus.HEALTHY, DepartmentStatus.HEALTHY_EMPTY},
            "editorial_ops_passed": editorial_ops.passed,
        }
    )
    pre_delivery_checks = {**ops.checks, **additional_checks}
    delivery_ops = delivery_gate(
        content_statuses=content_statuses,
        evidence_status=evidence_status,
        editorial_status=editorial_status,
        editorial_ops_passed=editorial_ops.passed and all(pre_delivery_checks.values()),
        provider_halted=provider_halted,
        unprocessed_candidate_count=max(
            0, analysis_candidate_count - analyze_attempts
        ),
        render_passed=bool(
            args.render and (run_dir / "04-digest.txt").is_file()
        ),
        idempotency_passed=True,
        smtp_configuration_valid=settings.smtp_configuration_prevalidated,
    )
    combined_checks = {
        **pre_delivery_checks,
        "department_delivery_ready": delivery_ops.passed,
    }
    ops = ops.model_copy(
        update={
            "passed": all(combined_checks.values()),
            "checks": combined_checks,
            "errors": [name for name, passed in combined_checks.items() if not passed],
            "offenders": dict(ops.offenders),
            "stats": {
                **ops.stats,
                **{
                    f"{department.value}_analysis_attempts": department_attempts[department]
                    for department in CONTENT_DEPARTMENTS
                },
                **{
                    f"{department.value}_provider_validated_outputs": department_validated[department]
                    for department in CONTENT_DEPARTMENTS
                },
            },
            "audit": {
                **ops.audit,
                "provider_request_budget": args.max_provider_requests,
                "provider_request_attempts": provider_request_attempts,
                "analysis_model": analysis_model_label,
                "primary_model": settings.gemini_analysis_model,
                "configured_fallback_model": settings.gemini_fallback_model,
                "fallback_used": fallback_used,
                "models_used": actual_models,
                "model_transitions": model_controller.transitions,
                "prompt_version": GeminiGlobalAiAnalysisAdapter.prompt_version,
                "department_statuses": {
                    department.value: content_statuses[department].value
                    for department in CONTENT_DEPARTMENTS
                },
                "smtp_configuration_checked": True,
                "smtp_configuration_prevalidated": settings.smtp_configuration_prevalidated,
            },
        }
    )
    delivery_handoff = write_stage_department_artifacts(
        run_dir=run_dir,
        run_id=run_id,
        department_id=DepartmentId.DELIVERY,
        status=delivery_ops.status,
        input_payload={
            "contract_version": "phase3c-department-contract-v1",
            "editorial_handoff": editorial_handoff.model_dump(mode="json"),
            "content_statuses": {
                department.value: content_statuses[department].value
                for department in CONTENT_DEPARTMENTS
            },
        },
        output_payload={
            "delivery_allowed": ops.passed,
            "smtp_configuration_valid": settings.smtp_configuration_prevalidated,
            "real_email_sent": False,
        },
        ops=delivery_ops,
        downstream_consumer="existing-email-delivery-adapter",
        department_prompt_version="delivery-watchdog-v1",
    )
    all_handoffs = [
        *department_handoffs,
        evidence_handoff,
        editorial_handoff,
        delivery_handoff,
    ]
    manifest = DepartmentManifest(
        run_id=run_id,
        entries=tuple(
            DepartmentManifestEntry(
                department_id=handoff.department_id,
                status=handoff.status,
                handoff_path=f"departments/{handoff.department_id.value}/handoff.json",
                handoff_sha256=artifact_sha256(handoff),
            )
            for handoff in all_handoffs
        ),
        delivery_allowed=ops.passed,
    )
    _write(run_dir / "department-manifest.json", manifest.model_dump(mode="json"))
    _write(run_dir / "ops.json", ops.model_dump(mode="json"))
    _write(run_dir / "source-bundle.json", {
        "run_id": run_id, "plan_version": plan.plan_version, "policy_version": policy.policy_version,
        "search_pages": [hashlib.sha256(json.dumps(p.model_dump(mode="json"), sort_keys=True, default=str).encode()).hexdigest() for p in pages],
        "sources": sources, "blocked": blocked, "event_clusters": clusters,
        "analysis_runtime": {
            "primary_model": settings.gemini_analysis_model,
            "configured_fallback_model": settings.gemini_fallback_model,
            "models_used": actual_models,
            "fallback_used": fallback_used,
            "model_transitions": model_controller.transitions,
            "prompt_version": GeminiGlobalAiAnalysisAdapter.prompt_version,
        },
    })
    summary = {
        "run_id": run_id, "status": "COMPLETED", "queries": len(queries),
        "discovery_status": discovery_status,
        "search_results": sum(len(p.results) for p in pages), "normalized": len(normalized),
        "eligible_t1_t2": len(candidates), "stale_dropped": stale_dropped,
        "fetch_plan": {"global": len([c for c in fetch_plan if c in global_pool]), "china": len([c for c in fetch_plan if c in cn_pool]), "macro": len([c for c in fetch_plan if c in macro_pool]), "arxiv": len([c for c in fetch_plan if c in arxiv_pool])},
        "fetched": len(sources), "blocked": len(blocked),
        "analyze_attempts": analyze_attempts, "analyzed": len(analyses),
        "provider_request_attempts": provider_request_attempts,
        "provider_request_budget": args.max_provider_requests,
        "provider_halted": provider_halted,
        "analysis_candidate_count": analysis_candidate_count,
        "unprocessed_candidate_count": max(0, analysis_candidate_count - analyze_attempts),
        "provider_budget_exhausted": provider_budget_exhausted,
        "provider_validated_outputs": provider_validated_outputs,
        "analysis_model": analysis_model_label,
        "models_used": actual_models,
        "fallback_used": fallback_used,
        "model_transitions": model_controller.transitions,
        "department_statuses": {
            department.value: content_statuses[department].value
            for department in CONTENT_DEPARTMENTS
        },
        "department_request_budgets": {
            department.value: department_budgets[department]
            for department in CONTENT_DEPARTMENTS
        },
        "macro_analyze_attempts": macro_analyze_attempts,
        "macro_provider_validated_outputs": macro_provider_validated_outputs,
        "analysis_failures": len(analysis_failures), "digest_items": len(digest_items),
        "cross_day_duplicates_skipped": len(cross_day_duplicates),
        "ops_passed": ops.passed,
        **a_share_counts,
    }
    if a_share_ops is not None:
        summary["a_share_digest_items"] = len(a_share_items)
        summary["a_share_ops_passed"] = a_share_ops.passed
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    run()
