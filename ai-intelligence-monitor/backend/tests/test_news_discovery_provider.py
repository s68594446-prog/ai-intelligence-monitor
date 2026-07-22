import json
import traceback
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.web_discovery.contracts import (
    DiscoveryRequest,
    ProviderSearchHit,
    ProviderSearchPage,
    SearchQuery,
)
from app.web_discovery.normalize import cluster_event_keys, normalize_search_pages
from app.web_discovery.provider_registry import (
    NEWS_PROVIDER_NAMES,
    build_news_provider,
)
from app.web_discovery.providers import (
    TAVILY_ENDPOINT,
    THENEWSAPI_ENDPOINT,
    TheNewsAPIAdapter,
)
from app.web_discovery.replay import frozen_search_replay
from app.web_discovery.search import WebSearchError
from app.web_discovery.service import discover_web
from app.web_discovery.source_policy import load_source_policy


FIXED_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def news_query():
    return SearchQuery(
        query_id="news_hbm", text="HBM high bandwidth memory Micron Samsung SK hynix NVIDIA",
        variable="HBM order visibility", purpose="recent news discovery", language="en",
        time_range="7d", plan_version="v1",
    )


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def response_data(url="https://investors.micron.com/news-releases/news-release-details/hbm"):
    return {
        "meta": {"found": 1, "returned": 1, "limit": 3, "page": 1},
        "data": [{
            "uuid": "article-uuid-1", "title": "Micron HBM update", "url": url,
            "description": "Discovery description", "snippet": "Short provider snippet",
            "keywords": "HBM, DRAM", "language": "en",
            "published_at": "2026-07-21T10:00:00.000000Z",
            "source": "investors.micron.com", "categories": ["business", "tech"],
            "relevance_score": 0.8,
        }],
    }


def adapter(handler, **kwargs):
    return TheNewsAPIAdapter(
        api_key=SecretStr("news-test-secret"), client=client(handler),
        sleep_fn=lambda _: None, clock=lambda: FIXED_NOW, **kwargs,
    )


def test_news_registry_is_separate_default_off_and_unknown_fails_closed():
    configured = Settings(_env_file=None)
    assert NEWS_PROVIDER_NAMES == ("thenewsapi",)
    assert configured.news_discovery_enabled is False
    assert configured.news_search_max_queries == 2
    assert configured.news_search_max_results_per_query == 3
    with pytest.raises(WebSearchError, match="UNKNOWN_NEWS_PROVIDER"):
        build_news_provider("tavily", configured)
    with pytest.raises(WebSearchError, match="MISSING_API_KEY"):
        build_news_provider("thenewsapi", configured)


def test_news_key_is_secretstr_and_not_in_settings_repr():
    configured = Settings(_env_file=None, THENEWSAPI_KEY="news-test-secret")
    assert isinstance(configured.thenewsapi_key, SecretStr)
    assert "news-test-secret" not in repr(configured)


def test_news_request_is_fixed_bounded_and_tokenless_in_artifact():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json=response_data(),
            headers={"X-UsageLimit-Remaining": "42", "X-Request-ID": "news-req"},
        )
    page = adapter(handler).search(news_query(), count=3)
    request = seen[0]
    assert request.url.scheme == "https" and request.url.host == "api.thenewsapi.com"
    assert request.url.path == "/v1/news/all"
    assert request.url.params["api_token"] == "news-test-secret"
    assert request.url.params["limit"] == "3" and request.url.params["page"] == "1"
    assert request.url.params["sort"] == "published_at"
    assert request.url.params["published_after"] == "2026-07-15T12:00:00"
    assert request.url.params["published_before"] == "2026-07-22T12:00:00"
    serialized = page.model_dump_json()
    assert "news-test-secret" not in serialized and "api_token" not in serialized
    assert page.endpoint == THENEWSAPI_ENDPOINT
    assert page.provider_request_id == "news-req"
    assert page.request_metadata["usage_headers_present"] is True
    assert page.request_metadata["remaining_quota_parsed"] is True


def test_news_article_contract_is_discovery_only_and_uuid_is_provenance():
    page = adapter(lambda _: httpx.Response(200, json=response_data())).search(news_query(), count=3)
    hit = page.results[0]
    assert hit.source_metadata["article_uuid"] == "article-uuid-1"
    assert hit.source_metadata["language"] == "en"
    assert hit.source_metadata["categories"] == ["business", "tech"]
    assert hit.source_metadata["snippet_policy"] == "DISCOVERY_ONLY"
    assert hit.source_metadata["keywords_policy"] == "DISCOVERY_ONLY"
    assert hit.source_metadata["full_text_available"] is False
    assert hit.snippet == "Discovery description"
    assert "Short provider snippet" not in json.dumps(page.raw_response)


def test_news_source_tier_comes_from_article_url_not_provider(app_settings):
    policy = load_source_policy(app_settings.web_source_policy_path)
    t1_page = adapter(lambda _: httpx.Response(200, json=response_data())).search(news_query(), count=3)
    unverified_page = adapter(
        lambda _: httpx.Response(200, json=response_data("https://unknown-news.invalid/hbm"))
    ).search(news_query(), count=3)
    t1 = normalize_search_pages([t1_page], policy)[0]
    unknown = normalize_search_pages([unverified_page], policy)[0]
    assert t1.source_tier.value == "T1"
    assert unknown.source_tier.value == "UNVERIFIED"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "INVALID_PARAMETERS"), (401, "HTTP_401"),
        (402, "USAGE_LIMIT_REACHED"), (403, "HTTP_403"),
        (404, "INVALID_PROVIDER_ENDPOINT"), (429, "RATE_LIMIT_REACHED"),
    ],
)
def test_news_nonretryable_errors_are_sanitized(status, code):
    calls = 0
    def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "news-test-secret"}})
    with pytest.raises(WebSearchError) as caught:
        adapter(handler).search(news_query(), count=3)
    assert calls == 1 and caught.value.code == code
    assert caught.value.retryable is False
    assert "news-test-secret" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_news_redirect_is_blocked_without_exposing_token():
    with pytest.raises(WebSearchError) as caught:
        adapter(
            lambda _: httpx.Response(302, headers={"location": "https://example.com/"})
        ).search(news_query(), count=3)
    assert caught.value.code == "PROVIDER_REDIRECT"
    assert "news-test-secret" not in str(caught.value)
    rendered = "".join(
        traceback.TracebackException.from_exception(
            caught.value, capture_locals=True
        ).format()
    )
    assert "news-test-secret" not in rendered


def test_news_5xx_and_timeout_retries_are_bounded_and_sanitized():
    calls = 0
    def server_error(_):
        nonlocal calls
        calls += 1
        return httpx.Response(503)
    with pytest.raises(WebSearchError) as caught:
        adapter(server_error, max_retries=2).search(news_query(), count=3)
    assert calls == 3 and caught.value.code == "UPSTREAM_5XX"

    def timeout(request):
        raise httpx.ConnectTimeout("news-test-secret", request=request)
    with pytest.raises(WebSearchError) as timeout_error:
        adapter(timeout, max_retries=0).search(news_query(), count=3)
    assert timeout_error.value.code == "NETWORK_ERROR"
    assert timeout_error.value.__cause__ is None
    assert "news-test-secret" not in str(timeout_error.value)


def test_news_limit_and_pagination_fail_closed():
    with pytest.raises(WebSearchError, match="NEWS_RESULT_LIMIT_INVALID"):
        adapter(lambda _: pytest.fail("must not call network")).search(news_query(), count=4)
    invalid = response_data()
    invalid["meta"]["page"] = 2
    with pytest.raises(WebSearchError, match="UNEXPECTED_PAGINATION"):
        adapter(lambda _: httpx.Response(200, json=invalid)).search(news_query(), count=3)


def test_news_and_web_same_url_merge_provenance_and_event_cluster(app_settings):
    policy = load_source_policy(app_settings.web_source_policy_path)
    news = adapter(lambda _: httpx.Response(200, json=response_data())).search(news_query(), count=3)
    web = ProviderSearchPage(
        provider="tavily", adapter_version="tavily-v1", query_id="web_hbm",
        endpoint=TAVILY_ENDPOINT, request_count=1, retry_count=0,
        results=[ProviderSearchHit(
            title="Micron HBM update", url=news.results[0].url,
            snippet="web lead", rank=1,
            source_metadata={"snippet_policy": "DISCOVERY_ONLY"},
        )], raw_response={"results": []},
    )
    normalized = normalize_search_pages([news, web], policy)
    assert len(normalized) == 1
    assert {item["provider"] for item in normalized[0].provider_provenance} == {
        "thenewsapi", "tavily",
    }
    assert len(set(cluster_event_keys(normalized).values())) == 1


class FakeNewsAdapter:
    name = "thenewsapi"
    version = "thenewsapi-all-news-v1"

    def __init__(self):
        self.calls = 0

    def search(self, query, *, count):
        self.calls += 1
        return ProviderSearchPage(
            provider=self.name, adapter_version=self.version, query_id=query.query_id,
            query_text_hash="a" * 64, endpoint=THENEWSAPI_ENDPOINT,
            request_count=1, retry_count=0, raw_response_sha256="b" * 64,
            request_metadata={
                "request_fingerprint": "c" * 64, "page": 1, "limit": count,
                "language": "en", "published_after": "2026-07-15T12:00:00",
                "published_before": "2026-07-22T12:00:00", "sort": "published_at",
                "search_query_hash": "d" * 64, "usage_headers_present": True,
                "remaining_quota_parsed": True, "remaining_quota": 42,
            },
            results=[ProviderSearchHit(
                title="Unverified HBM news", url="https://unknown-news.invalid/hbm",
                snippet="discovery only", rank=1,
                source_metadata={
                    "article_uuid": "uuid", "snippet_policy": "DISCOVERY_ONLY",
                    "keywords_policy": "DISCOVERY_ONLY", "full_text_available": False,
                },
            )],
            raw_response={"meta": {"page": 1}, "data": []},
        )

    def close(self):
        pass


def test_news_discovery_is_explicit_no_email_and_frozen_replay_is_offline(
    session, app_settings,
):
    fake = FakeNewsAdapter()
    enabled = app_settings.model_copy(update={
        "news_discovery_enabled": True,
        "news_search_max_queries": 1,
        "news_search_max_results_per_query": 3,
        "news_search_lookback_days": 7,
    })
    result = discover_web(
        session, DiscoveryRequest(news=True), app_settings=enabled, news_adapter=fake
    )
    assert fake.calls == 1
    assert result.ops_check["passed"] is True
    decisions = json.loads(open(result.artifact_paths[4], encoding="utf-8").read())
    assert decisions["document_decisions"] == []
    source_run_id = result.run_id
    fake.calls = 0
    replay = frozen_search_replay(session, source_run_id, app_settings=enabled)
    assert replay["stable"] is True and fake.calls == 0
    assert "api_token" not in json.dumps(
        [json.loads(open(path, encoding="utf-8").read()) for path in replay["artifact_paths"]]
    ).casefold()


def test_news_disabled_fails_closed_without_call_or_email(session, app_settings):
    fake = FakeNewsAdapter()
    result = discover_web(
        session, DiscoveryRequest(news=True), app_settings=app_settings,
        news_adapter=fake,
    )
    assert fake.calls == 0
    assert result.status.value == "FAILED"
    assert result.ops_check["checks"]["discovery_has_no_email"] is True
