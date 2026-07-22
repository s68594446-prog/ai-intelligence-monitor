import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.core.settings import Settings
from app.daily_ai.analysis import DeepSeekAShareAiAnalysisAdapter
from app.daily_ai.contracts import AShareUniverse
from app.daily_ai.digest import render_a_share_section, select_a_share_digest
from app.daily_ai.discovery import OFFICIAL_DOMAINS, keyword_prefilter, official_source
from app.daily_ai.ops import check_a_share_channel
from app.daily_ai.juhe import JuheAiBriefAdapter, JuheError, JuheHeadlinesAdapter
from app.daily_ai.pdf import SafeOfficialPdfFetcher
from app.daily_ai.replay import frozen_output_replay, frozen_source_replay
from app.daily_ai.tianapi import TianApiAiNewsAdapter, TianApiError
from app.web_discovery.contracts import ClassifiedTier, NormalizedSearchResult, SearchQuery
from app.web_discovery.fetch import WebPageFetcher
from app.web_discovery.providers import SearXNGWebSearchAdapter
from app.web_discovery.normalize import cluster_event_keys, normalize_search_pages
from app.web_discovery.security import canonicalize_url
from app.web_discovery.source_policy import load_source_policy


KEYWORDS = ["人工智能", "大模型", "生成式AI", "算力", "智算中心", "GPU", "AI芯片", "推理芯片", "服务器", "数据中心", "机器人", "机器视觉", "自动驾驶", "AI Agent", "模型训练", "云计算"]
SITES = {
    "sse": "www.sse.com.cn",
    "szse": "www.szse.cn",
    "bse": "www.bse.cn",
    "cninfo": "www.cninfo.com.cn",
}


def _write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _args():
    parser = argparse.ArgumentParser(description="Controlled daily A-share AI discovery; never sends email")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--juhe-smoke", action="store_true")
    parser.add_argument("--replay", type=Path)
    return parser.parse_args()


def _candidate(hit, query_id):
    url = canonicalize_url(hit.url)
    return NormalizedSearchResult(candidate_id="a-share-" + hashlib.sha256(url.encode()).hexdigest()[:16], canonical_url=url, title=hit.title, snippet=hit.snippet, query_ids=[query_id], best_rank=hit.rank, source_tier=ClassifiedTier.T1, source_policy_version="a-share-primary-sources-v1", source_domain=urlsplit(url).hostname or "")


def run():
    args = _args()
    if args.replay:
        source = json.loads((args.replay / "source-bundle.json").read_text())
        outputs = json.loads((args.replay / "analysis-output.json").read_text())
        print(json.dumps({"source": frozen_source_replay(source, source), "output": frozen_output_replay(outputs, outputs)}, sort_keys=True))
        return
    if not args.discover and not args.juhe_smoke:
        raise SystemExit("--discover, --juhe-smoke, or --replay is required")
    settings = Settings()
    if settings.juhe_stock_enabled:
        raise SystemExit("JUHE_STOCK_ENABLED must remain false in this phase")
    if args.juhe_smoke:
        smoke_query = SearchQuery(query_id="juhe_smoke", text="中国 人工智能 科技", variable="daily_cn_ai", purpose="provider_smoke", language="zh", time_range="24h", plan_version="daily-ai-v1")
        results = {}
        for name, adapter_cls, enabled, key in (
            ("juhe_headlines", JuheHeadlinesAdapter, settings.juhe_headlines_enabled, settings.juhe_headlines_api_key),
            ("juhe_ai_brief", JuheAiBriefAdapter, settings.juhe_ai_brief_enabled, settings.juhe_ai_brief_api_key),
        ):
            if not enabled:
                results[name] = {"status": "DISABLED", "request_count": 0, "result_count": 0, "business_status_code": None, "quota_status": "UNAVAILABLE"}
                continue
            try:
                adapter = adapter_cls(api_key=key, timeout_seconds=settings.web_search_timeout_seconds)
                page = adapter.search(smoke_query, count=3); adapter.close()
                results[name] = {"status": "SUCCESS", "request_count": page.request_count, "result_count": len(page.results), "business_status_code": page.request_metadata["business_status_code"], "quota_status": page.request_metadata["remaining_quota_status"]}
            except JuheError as exc:
                results[name] = {"status": exc.code.value, "request_count": 0 if exc.code.value == "MISSING_API_KEY" else 1, "result_count": 0, "business_status_code": exc.business_code, "quota_status": "UNAVAILABLE"}
        print(json.dumps({"providers": results, "secret_leak": False}, sort_keys=True))
        return
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-a-share-ai"
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    universe = AShareUniverse.model_validate_json((Path(__file__).resolve().parents[3] / "config/daily_ai/a_share_universe.json").read_text())
    tian = None
    tian_status = "DISABLED"
    if settings.tianapi_enabled:
        try:
            adapter = TianApiAiNewsAdapter(api_key=settings.tianapi_api_key, timeout_seconds=settings.web_search_timeout_seconds)
            tian = adapter.discover(num=3); adapter.close(); tian_status = tian.status.value
        except TianApiError as exc:
            tian_status = exc.code.value
    search = SearXNGWebSearchAdapter(image_digest=settings.searxng_image_digest, timeout_seconds=settings.web_search_timeout_seconds, max_retries=0)
    pages = []
    candidates = {}
    after = (now - timedelta(hours=48)).date().isoformat()
    compact_terms = "(人工智能 OR 大模型 OR 算力 OR AI芯片 OR 机器人)"
    for index, (name, domain) in enumerate(SITES.items(), 1):
        query = SearchQuery(query_id=f"a_share_{name}", text=f"site:{domain} {compact_terms} after:{after}", variable="a_share_ai_announcements", purpose="official_announcement_discovery", language="zh", time_range="48h", plan_version="a-share-ai-query-v1")
        page = search.search(query, count=3); pages.append(page)
        for hit in page.results:
            if official_source(hit.url) and keyword_prefilter(hit.title + " " + hit.snippet, KEYWORDS):
                item = _candidate(hit, query.query_id)
                candidates.setdefault(item.canonical_url, item)
    search.close()
    juhe_status = {}
    juhe_query = SearchQuery(query_id="daily_cn_ai", text="中国 人工智能 科技", variable="daily_cn_ai", purpose="discovery", language="zh", time_range="24h", plan_version="daily-ai-v1")
    for name, adapter_cls, enabled, key in (
        ("juhe_headlines", JuheHeadlinesAdapter, settings.juhe_headlines_enabled, settings.juhe_headlines_api_key),
        ("juhe_ai_brief", JuheAiBriefAdapter, settings.juhe_ai_brief_enabled, settings.juhe_ai_brief_api_key),
    ):
        if not enabled:
            juhe_status[name] = "DISABLED"
            continue
        try:
            adapter = adapter_cls(api_key=key, timeout_seconds=settings.web_search_timeout_seconds)
            page = adapter.search(juhe_query, count=3); adapter.close()
            pages.append(page); juhe_status[name] = "SUCCESS"
        except JuheError as exc:
            juhe_status[name] = exc.code.value
    normalized = normalize_search_pages(pages, load_source_policy(settings.web_source_policy_path))
    clusters = cluster_event_keys(normalized)
    for item in normalized:
        if official_source(item.canonical_url) and keyword_prefilter(item.title + " " + item.snippet, KEYWORDS):
            candidates.setdefault(item.canonical_url, item.model_copy(update={"source_tier": ClassifiedTier.T1, "source_policy_version": "a-share-primary-sources-v1"}))
    fetch_client = httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
    html_fetcher = WebPageFetcher(client=fetch_client, max_bytes=2_000_000, max_retries=0, minimum_interval_seconds=0)
    pdf_fetcher = SafeOfficialPdfFetcher(client=fetch_client)
    sources = []
    analyses = []
    blocked = []
    company_by_name = sorted(universe.constituents, key=lambda item: -len(item.company_name))
    for candidate in list(candidates.values())[:3]:
        if candidate.canonical_url.lower().split("?", 1)[0].endswith(".pdf"):
            result = pdf_fetcher.fetch(candidate.canonical_url)
            if result.status != "SUCCESS": blocked.append({"url": candidate.canonical_url, "error_code": result.error_code}); continue
            raw_text = result.text
        else:
            result = html_fetcher.fetch(candidate, allowed_urls=set(candidates))
            if result.status != "SUCCESS": blocked.append({"url": candidate.canonical_url, "error_code": result.error_code}); continue
            raw_text = result.raw_text
        sources.append({"url": candidate.canonical_url, "title": candidate.title, "raw_text": raw_text})
        company = next((item for item in company_by_name if item.company_name in raw_text or item.company_name in candidate.title), None)
        if company and settings.deepseek_api_key and len(analyses) < 1:
            analyzer = DeepSeekAShareAiAnalysisAdapter(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url, model=settings.deepseek_model, timeout_seconds=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries)
            analyses.append(analyzer.analyze(metadata={"company_name": company.company_name, "security_code": company.security_code, "exchange": company.exchange, "title": candidate.title}, raw_text=raw_text, source_url=candidate.canonical_url))
    fetch_client.close()
    digest_items = select_a_share_digest(analyses)
    raw_by_url = {item["url"]: item["raw_text"] for item in sources}
    ops = check_a_share_channel(tian=tian, analyses=analyses, raw_text_by_url=raw_by_url, digest_items=digest_items)
    source_bundle = {"run_id": run_id, "universe_status": universe.status, "universe_sources": [s.model_dump(mode="json") for s in universe.sources], "search_pages": [p.model_dump(mode="json") for p in pages], "normalized_result_count": len(normalized), "event_clusters": clusters, "juhe_status": juhe_status, "sources": sources, "blocked": blocked, "tian": {"status": tian_status, "result_count": tian.result_count if tian else 0, "urls": [str(item.url) for item in tian.items] if tian else []}}
    outputs = [item.model_dump(mode="json") for item in analyses]
    _write(run_dir / "source-bundle.json", source_bundle)
    _write(run_dir / "analysis-output.json", outputs)
    _write(run_dir / "ops.json", ops.model_dump(mode="json"))
    (run_dir / "test-digest.txt").write_text(render_a_share_section(digest_items) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": run_id, "tian_status": tian_status, "juhe_status": juhe_status, "search_queries": len(pages), "search_results": sum(len(p.results) for p in pages), "normalized_results": len(normalized), "event_clusters": len(set(clusters.values())), "official_candidates": len(candidates), "fetched": len(sources), "blocked": len(blocked), "analyzed": len(analyses), "digest_items": len(digest_items), "ops_passed": ops.passed}, sort_keys=True))


if __name__ == "__main__":
    run()
