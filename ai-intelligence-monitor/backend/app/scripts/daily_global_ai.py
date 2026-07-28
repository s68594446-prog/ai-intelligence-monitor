import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from app.core.settings import Settings
from app.daily_ai.analysis import DeepSeekAShareAiAnalysisAdapter
from app.daily_ai.contracts import AShareUniverse
from app.daily_ai.digest import render_a_share_section, select_a_share_digest
from app.daily_ai.discovery import keyword_prefilter, official_source
from app.daily_ai.global_analysis import (
    AnalysisOutputError,
    DeepSeekGlobalAiAnalysisAdapter,
    EvidenceMismatch,
    ProviderCallError,
)
from app.daily_ai.global_digest import render_global_digest, select_global_digest
from app.daily_ai.global_ops import advice_violations, check_global_channel, numeric_claim_cited
from app.daily_ai.juhe import JuheAiBriefAdapter, JuheError, JuheHeadlinesAdapter
from app.daily_ai.ops import check_a_share_channel
from app.daily_ai.pdf import SafeOfficialPdfFetcher
from app.daily_ai.quality import check_text_quality, out_of_scope_headline
from app.daily_ai.rss import RssDiscoveryAdapter, RssError
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
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/daily_global_ai_analysis_v7.txt"
FETCH_MAX_BYTES = 5_000_000
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
CN_BOARD_DOMAINS = {
    "36kr.com", "qbitai.com", "jiqizhixin.com", "leiphone.com", "caixin.com",
    "baidu.com", "aliyun.com", "tencent.com", "bytedance.com", "volcengine.com",
    "huawei.com", "iflytek.com", "minimaxi.com", "deepseek.com",
}


def _is_cn_domain(domain: str) -> bool:
    host = domain.casefold()
    return host.endswith(".cn") or any(host == d or host.endswith("." + d) for d in CN_BOARD_DOMAINS)

def _write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _args():
    parser = argparse.ArgumentParser(description="Daily AI digest channel (domestic network); default is offline dry-run, never sends email")
    parser.add_argument("--discover", action="store_true", help="call the local SearXNG provider (local machine only)")
    parser.add_argument("--rss", action="store_true", help="RSS feed discovery (works on GitHub Actions)")
    parser.add_argument("--juhe", action="store_true", help="Juhe ID235/ID850 discovery (needs enabled flags and keys)")
    parser.add_argument("--fetch", action="store_true", help="safe-fetch T1/T2 candidates (requires --discover or --rss)")
    parser.add_argument("--analyze", action="store_true", help="DeepSeek analysis of fetched text (requires --fetch)")
    parser.add_argument("--render", action="store_true", help="render the digest (requires --analyze; empty digest is legal)")
    parser.add_argument("--a-share", action="store_true", help="run the A-share official-announcement section (requires --fetch)")
    parser.add_argument("--send", action="store_true", help="email delivery; fails closed, not configured in this phase")
    parser.add_argument("--max-queries", type=int, default=6)
    parser.add_argument("--max-results", type=int, default=3)
    parser.add_argument("--max-fetch", type=int, default=3)
    parser.add_argument("--max-analyze", type=int, default=2)
    parser.add_argument("--lookback-hours", type=int, default=48, help="only items published within this window (unknown dates kept)")
    args = parser.parse_args()
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
        "query_count": len(queries), "budgets": {"max_queries": args.max_queries, "max_results": args.max_results, "max_fetch": args.max_fetch, "max_analyze": args.max_analyze},
        "queries": [q.model_dump(mode="json") for q in queries],
        "stages": {"discover": args.discover, "rss": args.rss, "juhe": args.juhe, "fetch": args.fetch, "analyze": args.analyze, "render": args.render, "a_share": args.a_share},
    })
    if not (args.discover or args.rss):
        print(json.dumps({"run_id": run_id, "status": "PLANNED", "plan": len(queries), "network_calls": 0, "deepseek_calls": 0}, sort_keys=True))
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
    # Fetch-budget balancing: reserve a China share so T1 global feeds cannot
    # crowd out the China board, and cap arXiv so papers cannot crowd out news.
    cn_pool = [item for item in candidates if _is_cn_domain(item.source_domain)]
    arxiv_pool = [item for item in candidates if item not in cn_pool and (item.source_domain == "arxiv.org" or item.source_domain.endswith(".arxiv.org"))]
    global_pool = [item for item in candidates if item not in cn_pool and item not in arxiv_pool]
    cn_budget = min(len(cn_pool), round(args.max_fetch * CHINA_FETCH_SHARE))
    arxiv_budget = min(len(arxiv_pool), ARXIV_FETCH_CAP)
    global_budget = args.max_fetch - cn_budget - arxiv_budget
    fetch_plan = global_pool[:global_budget] + cn_pool[:cn_budget] + arxiv_pool[:arxiv_budget]
    spare = args.max_fetch - len(fetch_plan)
    if spare > 0:  # hand unused share back to whichever pool has leftovers (papers stay capped)
        leftovers = [item for item in global_pool[global_budget:] + cn_pool[cn_budget:] if item not in fetch_plan]
        fetch_plan.extend(leftovers[:spare])
    _write(run_dir / "01-search-results.json", {
        "run_id": run_id, "pages": [p.model_dump(mode="json") for p in pages],
        "discovery_status": discovery_status,
        "normalized_count": len(normalized), "eligible_t1_t2": len(candidates),
        "out_of_scope_excluded": out_of_scope,
        "event_clusters": clusters,
        "tier_counts": {tier.value: sum(item.source_tier == tier for item in normalized) for tier in ClassifiedTier},
    })

    sources, blocked, analyses, analysis_failures = [], [], [], []
    analysis_urls: dict[str, str] = {}
    analyze_attempts = 0
    if args.fetch:
        fetch_client = httpx.Client(timeout=settings.web_search_timeout_seconds, follow_redirects=False, trust_env=False)
        fetcher = WebPageFetcher(client=fetch_client, max_bytes=FETCH_MAX_BYTES, max_retries=0, minimum_interval_seconds=1)
        allowed = {item.canonical_url for item in candidates}
        by_id = {item.candidate_id: item for item in candidates}
        planned_urls = {item.canonical_url for item in fetch_plan}
        analyzer = None
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
                sources.append({"url": candidate.canonical_url, "tier": candidate.source_tier.value, "title": result.title or candidate.title, "sha256": result.response_sha256, "raw_text": result.raw_text})
                # Budget counts DeepSeek attempts (cost), not accepted results:
                # a discarded article must not buy a free extra call.
                if args.analyze and analyze_attempts < args.max_analyze:
                    if analyzer is None:
                        analyzer = DeepSeekGlobalAiAnalysisAdapter(
                            api_key=settings.deepseek_api_key or "",
                            base_url=settings.deepseek_base_url, model=settings.deepseek_model,
                            prompt_path=PROMPT_PATH,
                            timeout_seconds=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries,
                        )
                    analyze_attempts += 1
                    # Fail-soft: any single-article failure is discarded with an
                    # audit record and the run continues. Nothing here may abort
                    # the whole run.
                    try:
                        analysis = analyzer.analyze(
                            metadata={"title": result.title or candidate.title, "source_domain": urlsplit(candidate.canonical_url).hostname or "", "source_tier": candidate.source_tier.value},
                            raw_text=result.raw_text, source_url=candidate.canonical_url,
                        )
                        quality_reasons = check_text_quality({
                            "headline": analysis.headline,
                            "what_happened": analysis.what_happened,
                            "why_it_matters": analysis.why_it_matters,
                            "transmission_path": analysis.transmission_path,
                        })
                        # Belt-and-suspenders: the model can rewrite an in-scope
                        # source title into a broker/quote headline. If it does,
                        # discard so the item never reaches the digest or ops.
                        scope_term = out_of_scope_headline(analysis.headline)
                        advice_terms = advice_violations(analysis)
                        if scope_term:
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "OUT_OF_SCOPE_MARKET_ACTION", "diagnostics": {"matched_term": scope_term, "headline": analysis.headline[:120]}})
                        elif advice_terms:
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "DISCIPLINE_ADVICE", "diagnostics": {"matched_terms": advice_terms, "headline": analysis.headline[:120]}})
                        elif quality_reasons:
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "LOW_TEXT_QUALITY", "diagnostics": {"reasons": quality_reasons, "headline": analysis.headline[:120]}})
                        # A number claimed without a quote that carries it is a
                        # single-item defect: drop this item with an audit record
                        # and keep the rest of the digest. Enforced here (not only
                        # in ops) because the ops check is a whole-digest boolean,
                        # so one offender used to block the entire email.
                        elif not numeric_claim_cited(analysis):
                            analysis_failures.append({"url": candidate.canonical_url, "error_code": "NUMERIC_CLAIM_UNCITED", "diagnostics": {"headline": analysis.headline[:120], "quotes": [e.quote[:160] for e in analysis.evidence]}})
                        else:
                            analyses.append(analysis)
                            analysis_urls[str(analysis.evidence[0].source_url)] = candidate.candidate_id
                    except EvidenceMismatch as exc:
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": "EVIDENCE_MISMATCH", "diagnostics": exc.diagnostics})
                    except (AnalysisOutputError, ProviderCallError) as exc:
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": exc.diagnostics.get("reason_code", type(exc).__name__), "diagnostics": exc.diagnostics})
                    except (LLMOutputValidationError, LLMAdapterTimeout, LLMProviderError) as exc:
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": type(exc).__name__, "detail": str(exc)[:300]})
                    except Exception as exc:  # absolute fail-soft guard
                        analysis_failures.append({"url": candidate.canonical_url, "error_code": f"UNEXPECTED_{type(exc).__name__}", "detail": str(exc)[:300]})
        finally:
            fetch_client.close()
    _write(run_dir / "02-fetch.json", {"run_id": run_id, "fetched": [{k: v for k, v in item.items() if k != "raw_text"} for item in sources], "blocked": blocked})
    _write(run_dir / "03-analysis-output.json", [item.model_dump(mode="json") for item in analyses])
    _write(run_dir / "analysis-failures.json", analysis_failures)

    cluster_by_url = {url: clusters.get(candidate_id, candidate_id) for url, candidate_id in analysis_urls.items()}
    digest_items = select_global_digest(analyses, cluster_by_url=cluster_by_url) if args.render else []
    a_share_items, a_share_ops, a_share_counts = [], None, {}
    if args.a_share:
        china_count = sum(item["section"] == "CHINA" for item in digest_items)
        a_share_items, a_share_ops, a_share_counts = _a_share_stage(
            settings=settings, run_dir=run_dir, now=now, analyze=args.analyze, china_existing_count=china_count,
        )
    if args.render:
        digest_text = render_global_digest(digest_items)
        if args.a_share:
            digest_text += "\n\n" + render_a_share_section(a_share_items)
        (run_dir / "04-digest.txt").write_text(digest_text + "\n", encoding="utf-8")
    raw_by_url = {item["url"]: item["raw_text"] for item in sources}
    ops = check_global_channel(
        analyses=analyses, raw_text_by_url=raw_by_url, digest_items=digest_items, policy=policy,
        discarded_evidence_failures=sum(item["error_code"] == "EVIDENCE_MISMATCH" for item in analysis_failures),
    )
    _write(run_dir / "ops.json", ops.model_dump(mode="json"))
    _write(run_dir / "source-bundle.json", {
        "run_id": run_id, "plan_version": plan.plan_version, "policy_version": policy.policy_version,
        "search_pages": [hashlib.sha256(json.dumps(p.model_dump(mode="json"), sort_keys=True, default=str).encode()).hexdigest() for p in pages],
        "sources": sources, "blocked": blocked, "event_clusters": clusters,
    })
    summary = {
        "run_id": run_id, "status": "COMPLETED", "queries": len(queries),
        "discovery_status": discovery_status,
        "search_results": sum(len(p.results) for p in pages), "normalized": len(normalized),
        "eligible_t1_t2": len(candidates), "stale_dropped": stale_dropped,
        "fetch_plan": {"global": len([c for c in fetch_plan if c in global_pool]), "china": len([c for c in fetch_plan if c in cn_pool]), "arxiv": len([c for c in fetch_plan if c in arxiv_pool])},
        "fetched": len(sources), "blocked": len(blocked),
        "analyze_attempts": analyze_attempts, "analyzed": len(analyses),
        "analysis_failures": len(analysis_failures), "digest_items": len(digest_items),
        "ops_passed": ops.passed,
        **a_share_counts,
    }
    if a_share_ops is not None:
        summary["a_share_digest_items"] = len(a_share_items)
        summary["a_share_ops_passed"] = a_share_ops.passed
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    run()
