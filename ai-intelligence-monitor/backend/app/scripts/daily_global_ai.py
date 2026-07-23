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
from app.daily_ai.global_analysis import DeepSeekGlobalAiAnalysisAdapter
from app.daily_ai.global_digest import render_global_digest, select_global_digest
from app.daily_ai.global_ops import check_global_channel
from app.daily_ai.juhe import JuheAiBriefAdapter, JuheError, JuheHeadlinesAdapter
from app.daily_ai.ops import check_a_share_channel
from app.daily_ai.pdf import SafeOfficialPdfFetcher
from app.daily_ai.rss import RssDiscoveryAdapter, RssError
from app.scripts.daily_a_share_ai import KEYWORDS as A_SHARE_KEYWORDS
from app.scripts.daily_a_share_ai import SITES as A_SHARE_SITES
from app.scripts.daily_a_share_ai import _candidate as a_share_candidate
from app.web_discovery.contracts import ClassifiedTier, SearchQuery
from app.web_discovery.fetch import WebPageFetcher
from app.web_discovery.normalize import cluster_event_keys, normalize_search_pages
from app.web_discovery.providers import SearXNGWebSearchAdapter
from app.web_discovery.query_plan import load_query_plan
from app.web_discovery.source_policy import load_source_policy


CONFIG_DIR = Path(__file__).resolve().parents[3] / "config/daily_ai"
QUERY_PLAN_PATH = CONFIG_DIR / "cn_ai_queries.yaml"
SOURCE_POLICY_PATH = CONFIG_DIR / "daily_ai_source_policy.yaml"
RSS_SOURCES_PATH = CONFIG_DIR / "rss_sources.yaml"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts/daily_global_ai_analysis_v1.txt"

# Bilingual AI relevance terms for general (non-AI-dedicated) discovery
# sources; AI-dedicated feeds are marked prefilter:false in rss_sources.yaml.
EN_KEYWORDS = [
    "artificial intelligence", "machine learning", "large language model", "llm",
    "gpt", "chatgpt", "openai", "anthropic", "claude", "gemini", "deepmind",
    "llama", "mistral", "neural", "transformer", "gpu", "semiconductor",
    "robot", "autonomous", "ai model", "ai chip", "ai agent", "data center", "datacenter",
]


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
    if not 1 <= args.max_queries <= 13 or not 1 <= args.max_results <= 5 or not 1 <= args.max_fetch <= 10 or not 1 <= args.max_analyze <= 8:
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
                analyses.append(analyzer.analyze(metadata={"company_name": company.company_name, "security_code": company.security_code, "exchange": company.exchange, "title": candidate.title}, raw_text=raw_text, source_url=candidate.canonical_url))
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

    def passes_prefilter(item) -> bool:
        # AI-dedicated feeds skip keyword matching; general sources require a
        # bilingual keyword hit. Non-RSS discovery defaults to filtering.
        if any(not feed_prefilter.get(query_id, True) for query_id in item.query_ids):
            return True
        return keyword_prefilter(item.title + " " + item.snippet, A_SHARE_KEYWORDS + EN_KEYWORDS)

    candidates = [
        item for item in normalized
        if item.source_tier in {ClassifiedTier.T1, ClassifiedTier.T2} and passes_prefilter(item)
    ]
    candidates.sort(key=lambda item: (0 if item.source_tier == ClassifiedTier.T1 else 1, item.best_rank))
    _write(run_dir / "01-search-results.json", {
        "run_id": run_id, "pages": [p.model_dump(mode="json") for p in pages],
        "discovery_status": discovery_status,
        "normalized_count": len(normalized), "eligible_t1_t2": len(candidates),
        "event_clusters": clusters,
        "tier_counts": {tier.value: sum(item.source_tier == tier for item in normalized) for tier in ClassifiedTier},
    })

    sources, blocked, analyses = [], [], []
    if args.fetch:
        fetch_client = httpx.Client(timeout=settings.web_search_timeout_seconds, follow_redirects=False, trust_env=False)
        fetcher = WebPageFetcher(client=fetch_client, max_bytes=2_000_000, max_retries=0, minimum_interval_seconds=1)
        allowed = {item.canonical_url for item in candidates}
        analyzer = None
        try:
            for candidate in candidates[: args.max_fetch]:
                result = fetcher.fetch(candidate, allowed_urls=allowed)
                if result.status.value != "SUCCESS":
                    blocked.append({"url": candidate.canonical_url, "tier": candidate.source_tier.value, "status": result.status.value, "error_code": result.error_code})
                    continue
                sources.append({"url": candidate.canonical_url, "tier": candidate.source_tier.value, "title": result.title or candidate.title, "sha256": result.response_sha256, "raw_text": result.raw_text})
                if args.analyze and len(analyses) < args.max_analyze:
                    if analyzer is None:
                        analyzer = DeepSeekGlobalAiAnalysisAdapter(
                            api_key=settings.deepseek_api_key or "",
                            base_url=settings.deepseek_base_url, model=settings.deepseek_model,
                            prompt_path=PROMPT_PATH,
                            timeout_seconds=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries,
                        )
                    analyses.append(analyzer.analyze(
                        metadata={"title": result.title or candidate.title, "source_domain": urlsplit(candidate.canonical_url).hostname or "", "source_tier": candidate.source_tier.value},
                        raw_text=result.raw_text, source_url=candidate.canonical_url,
                    ))
        finally:
            fetch_client.close()
    _write(run_dir / "02-fetch.json", {"run_id": run_id, "fetched": [{k: v for k, v in item.items() if k != "raw_text"} for item in sources], "blocked": blocked})
    _write(run_dir / "03-analysis-output.json", [item.model_dump(mode="json") for item in analyses])

    digest_items = select_global_digest(analyses) if args.render else []
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
    ops = check_global_channel(analyses=analyses, raw_text_by_url=raw_by_url, digest_items=digest_items, policy=policy)
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
        "eligible_t1_t2": len(candidates), "fetched": len(sources), "blocked": len(blocked),
        "analyzed": len(analyses), "digest_items": len(digest_items),
        "ops_passed": ops.passed,
        **a_share_counts,
    }
    if a_share_ops is not None:
        summary["a_share_digest_items"] = len(a_share_items)
        summary["a_share_ops_passed"] = a_share_ops.passed
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    run()
