import argparse

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import build_engine
from app.core.settings import settings
from app.web_discovery.contracts import DiscoveryRequest
from app.web_discovery.fetch import WebPageFetcher
from app.pipeline.errors import LLMConfigurationError
from app.web_discovery.provider_registry import (
    NEWS_PROVIDER_NAMES,
    PROVIDER_NAMES,
    build_news_provider,
    build_routed_provider,
)
from app.web_discovery.search import WebSearchError
from app.web_discovery.service import discover_web
from app.web_discovery.triage import DeepSeekDiscoveryTriageAdapter


def main():
    parser = argparse.ArgumentParser(description="Bounded DRAM/HBM web discovery; default is offline plan only")
    parser.add_argument("--search", action="store_true", help="call the configured registered Web Search provider")
    parser.add_argument("--triage", action="store_true", help="call Discovery Triage after search or news")
    parser.add_argument("--fetch", action="store_true", help="fetch only approved search-result URLs")
    parser.add_argument("--analyze", action="store_true", help="send eligible Documents through the existing Harness")
    parser.add_argument("--provider", choices=PROVIDER_NAMES, help="select one registered search provider")
    parser.add_argument("--news", action="store_true", help="explicitly run separately budgeted news discovery")
    parser.add_argument("--news-provider", choices=NEWS_PROVIDER_NAMES, help="select one registered news provider")
    args = parser.parse_args()
    request = DiscoveryRequest(
        search=args.search, news=args.news, triage=args.triage,
        fetch=args.fetch, analyze=args.analyze,
    )
    setting_updates = {}
    if args.provider:
        setting_updates["web_search_provider"] = args.provider
    if args.news:
        setting_updates["news_discovery_enabled"] = True
    if args.news_provider:
        setting_updates["news_search_provider"] = args.news_provider
    run_settings = settings.model_copy(update=setting_updates) if setting_updates else settings
    search_adapter = news_adapter = triage_adapter = fetcher = fetch_client = None
    try:
        if args.search:
            search_adapter = build_routed_provider(run_settings, selected=args.provider)
        if args.news:
            news_adapter = build_news_provider(
                args.news_provider or run_settings.news_search_provider, run_settings
            )
        if args.triage:
            triage_adapter = DeepSeekDiscoveryTriageAdapter(
                api_key=run_settings.deepseek_api_key, base_url=run_settings.deepseek_base_url,
                model=run_settings.deepseek_model, timeout_seconds=run_settings.llm_timeout_seconds,
                max_retries=run_settings.llm_max_retries, prompt_path=run_settings.discovery_triage_prompt_path,
            )
    except WebSearchError as exc:
        if search_adapter is not None:
            search_adapter.close()
        if news_adapter is not None:
            news_adapter.close()
        print("status=BLOCKED")
        print(f"error_code={exc.code}")
        raise SystemExit(2) from None
    except LLMConfigurationError:
        if search_adapter is not None:
            search_adapter.close()
        if news_adapter is not None:
            news_adapter.close()
        print("status=BLOCKED")
        print("error_code=DEEPSEEK_CONFIGURATION_ERROR")
        raise SystemExit(2) from None
    if args.fetch:
        fetch_client = httpx.Client(
            timeout=run_settings.web_search_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        fetcher = WebPageFetcher(client=fetch_client, max_bytes=run_settings.fixed_fetch_max_bytes)
    engine = build_engine(run_settings.database_url)
    maker = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    try:
        with maker() as session:
            result = discover_web(
                session, request, app_settings=run_settings, search_adapter=search_adapter,
                news_adapter=news_adapter, triage_adapter=triage_adapter, fetcher=fetcher,
            )
        print(f"run_id={result.run_id}")
        print(f"status={result.status.value}")
        print(f"plan={result.plan_count} results={result.search_result_count} triaged={result.triage_count} fetched={result.fetched_count}")
        print(f"documents={result.document_count} duplicates={result.duplicate_count} analyzed={result.analyzed_count} blocked={result.blocked_count}")
        print(f"ops_check={result.ops_check.get('passed', False)}")
        if result.ops_check.get("errors"):
            print("blocked_reasons=" + "; ".join(result.ops_check["errors"]))
    finally:
        if search_adapter is not None:
            search_adapter.close()
        if news_adapter is not None:
            news_adapter.close()
        if fetch_client is not None:
            fetch_client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
