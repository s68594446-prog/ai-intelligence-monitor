import json

import httpx
import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.web_discovery.contracts import ProviderSearchHit, ProviderSearchPage, SearchQuery
from app.web_discovery.fetch import WebPageFetcher
from app.web_discovery.normalize import normalize_search_pages
from app.web_discovery.provider_registry import (
    PROVIDER_NAMES,
    RoutedWebSearchAdapter,
    build_provider,
    build_routed_provider,
)
from app.web_discovery.providers import (
    EXA_ENDPOINT,
    SEARXNG_ENDPOINT,
    TAVILY_ENDPOINT,
    YOU_ENDPOINT,
    ExaWebSearchAdapter,
    GeminiGoogleSearchAdapter,
    SearXNGWebSearchAdapter,
    TavilyWebSearchAdapter,
    YouWebSearchAdapter,
)
from app.web_discovery.search import WebSearchError
from app.web_discovery.source_policy import load_source_policy


def query():
    return SearchQuery(
        query_id="hbm", text="Micron HBM4 production official", variable="HBM order visibility",
        purpose="official production evidence", language="en", time_range="30d", plan_version="v1",
    )


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def page(provider="tavily", url="https://investors.micron.com/news"):
    return ProviderSearchPage(
        provider=provider, adapter_version=provider + "-v1", query_id="hbm", endpoint=TAVILY_ENDPOINT,
        request_count=1, retry_count=0, results=[ProviderSearchHit(title="Micron HBM", url=url, snippet="lead", rank=1)],
        raw_response={"results": []}, raw_response_sha256="a" * 64,
    )


def test_registry_is_fixed_and_unknown_fails_closed():
    assert PROVIDER_NAMES == ("brave", "searxng", "tavily", "exa", "you", "gemini_google")
    with pytest.raises(WebSearchError, match="UNKNOWN_PROVIDER"):
        build_provider("arbitrary", Settings(_env_file=None))


@pytest.mark.parametrize("name", ["brave", "tavily", "exa", "you", "gemini_google"])
def test_hosted_provider_requires_secret(name):
    with pytest.raises(WebSearchError, match="MISSING_API_KEY"):
        build_provider(name, Settings(_env_file=None))


def test_hosted_secrets_are_secretstr_and_do_not_appear_in_settings_repr():
    configured = Settings(
        _env_file=None,
        TAVILY_API_KEY="tavily-secret",
        EXA_API_KEY="exa-secret",
        YOU_API_KEY="you-secret",
        GEMINI_API_KEY="gemini-secret",
    )
    assert all(
        isinstance(value, SecretStr)
        for value in (
            configured.tavily_api_key,
            configured.exa_api_key,
            configured.you_api_key,
            configured.gemini_api_key,
        )
    )
    assert "tavily-secret" not in repr(configured)


def test_tavily_request_disables_answer_raw_and_advanced():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"request_id": "req", "answer": "discard", "results": [{"title": "A", "url": "https://micron.com/a", "content": "discovery", "score": 0.8, "raw_content": "discard"}]})
    adapter = TavilyWebSearchAdapter(api_key=SecretStr("secret"), client=client(handler), sleep_fn=lambda _: None)
    result = adapter.search(query(), count=3)
    payload = json.loads(seen[0].content)
    assert str(seen[0].url) == TAVILY_ENDPOINT
    assert payload["search_depth"] == "basic" and payload["auto_parameters"] is False
    assert payload["include_answer"] is False and payload["include_raw_content"] is False
    assert result.results[0].snippet == "discovery"
    assert "answer" not in result.raw_response and "raw_content" not in json.dumps(result.raw_response)
    assert "secret" not in result.model_dump_json()


def test_exa_search_does_not_request_or_retain_contents():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"requestId": "exa-req", "results": [{"title": "A", "url": "https://micron.com/a", "publishedDate": "2026-07-01T00:00:00Z", "text": "must not persist", "highlights": ["no"]}]})
    result = ExaWebSearchAdapter(api_key=SecretStr("secret"), client=client(handler), sleep_fn=lambda _: None).search(query(), count=2)
    assert str(seen[0].url) == EXA_ENDPOINT
    assert "contents" not in json.loads(seen[0].content)
    assert result.results[0].snippet == ""
    assert "must not persist" not in json.dumps(result.raw_response)


def test_you_simple_search_description_is_discovery_only():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": {"web": [{"title": "A", "url": "https://micron.com/a", "description": "provider description"}]}})
    result = YouWebSearchAdapter(api_key=SecretStr("secret"), client=client(handler), sleep_fn=lambda _: None).search(query(), count=2)
    assert seen[0].url.host == "ydc-index.io" and seen[0].url.path == "/v1/search"
    assert seen[0].headers["x-api-key"] == "secret"
    assert result.results[0].snippet == "provider description"
    assert result.results[0].source_metadata["snippet_policy"] == "DISCOVERY_ONLY"


def test_gemini_retains_only_grounding_urls_and_discards_answer():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "generated conclusion"}]}, "groundingMetadata": {"webSearchQueries": ["HBM4"], "groundingChunks": [{"web": {"uri": "https://micron.com/a", "title": "Micron"}}]}}]})
    adapter = GeminiGoogleSearchAdapter(api_key=SecretStr("secret"), model="gemini-2.5-flash-lite", client=client(handler), sleep_fn=lambda _: None)
    result = adapter.search(query(), count=3)
    payload = json.loads(seen[0].content)
    assert payload["tools"] == [{"google_search": {}}]
    assert "Thesis" not in json.dumps(payload)
    assert result.results[0].url == "https://micron.com/a"
    assert result.results[0].snippet == ""
    assert "generated conclusion" not in json.dumps(result.raw_response)


def test_gemini_model_and_endpoint_are_bounded():
    with pytest.raises(WebSearchError, match="INVALID_MODEL"):
        GeminiGoogleSearchAdapter(api_key=SecretStr("x"), model="../../other?key=x")


def test_searxng_uses_exact_loopback_json_endpoint():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [{"title": "A", "url": "https://micron.com/a", "content": "lead", "engines": ["google"]}]})
    result = SearXNGWebSearchAdapter(client=client(handler), sleep_fn=lambda _: None).search(query(), count=3)
    assert str(seen[0].url).startswith(SEARXNG_ENDPOINT + "?")
    assert seen[0].url.host == "127.0.0.1" and seen[0].url.port == 8080
    assert seen[0].url.params["format"] == "json"
    assert result.results[0].snippet == "lead"
    assert result.adapter_version.endswith("@UNVERIFIED")
    assert result.results[0].source_metadata["runtime_image_digest"] == "UNVERIFIED"


@pytest.mark.parametrize("status", [401, 403, 429])
def test_provider_auth_and_rate_errors_are_sanitized(status):
    calls = 0
    def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="secret upstream body")
    adapter = TavilyWebSearchAdapter(api_key=SecretStr("secret"), client=client(handler), sleep_fn=lambda _: None)
    with pytest.raises(WebSearchError) as caught:
        adapter.search(query(), count=1)
    assert calls == 1 and "secret" not in str(caught.value)


def test_provider_5xx_retries_are_bounded():
    calls = 0
    def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(503)
    adapter = ExaWebSearchAdapter(api_key=SecretStr("secret"), client=client(handler), max_retries=2, sleep_fn=lambda _: None)
    with pytest.raises(WebSearchError, match="UPSTREAM_5XX"):
        adapter.search(query(), count=1)
    assert calls == 3


def test_provider_timeout_is_sanitized_and_retryable():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("secret transport detail", request=request)
    adapter = TavilyWebSearchAdapter(
        api_key=SecretStr("secret"), client=client(handler), max_retries=0,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(WebSearchError) as caught:
        adapter.search(query(), count=1)
    assert caught.value.code == "NETWORK_ERROR"
    assert caught.value.retryable is True
    assert caught.value.request_count == 1
    assert "secret" not in str(caught.value)


def test_provider_response_size_is_bounded():
    response = httpx.Response(200, headers={"content-length": "101"}, content=b"x" * 101)
    adapter = YouWebSearchAdapter(api_key=SecretStr("secret"), client=client(lambda _: response), sleep_fn=lambda _: None)
    adapter.max_response_bytes = 100
    with pytest.raises(WebSearchError, match="RESPONSE_TOO_LARGE"):
        adapter.search(query(), count=1)


def test_router_fallbacks_once_only_for_retryable_failure():
    class Fake:
        def __init__(self, name, error=None): self.name, self.version, self.error, self.calls = name, name + "-v1", error, 0
        def search(self, query, *, count):
            self.calls += 1
            if self.error: raise self.error
            return page(self.name)
        def close(self): pass
    primary = Fake(
        "searxng",
        WebSearchError(
            "NETWORK_ERROR", retryable=True, request_count=3, retry_count=2
        ),
    )
    fallback = Fake("tavily")
    router = RoutedWebSearchAdapter(primary, [fallback, Fake("exa")], max_fallbacks=1)
    result = router.search(query(), count=2)
    assert primary.calls == 1 and fallback.calls == 1
    assert result.fallback_from == "searxng"
    assert len([item for item in router.audit if item["fallback"]]) == 1
    assert sum(item["request_count"] for item in router.audit) == 4


def test_router_removes_fallback_cycles_and_honors_one_fallback_limit():
    configured = Settings(
        _env_file=None,
        WEB_SEARCH_PROVIDER="tavily",
        WEB_SEARCH_FALLBACKS="tavily,exa,you,tavily",
        WEB_SEARCH_MAX_FALLBACKS=1,
        TAVILY_API_KEY="t",
        EXA_API_KEY="e",
        YOU_API_KEY="y",
    )
    router = build_routed_provider(configured)
    try:
        assert router.primary.name == "tavily"
        assert [item.name for item in router.fallbacks] == ["exa"]
    finally:
        router.close()


def test_router_does_not_fallback_on_auth_or_zero_results():
    class Fake:
        version = "v"
        def __init__(self, name, error=None, results=True): self.name, self.error, self.results, self.calls = name, error, results, 0
        def search(self, query, *, count):
            self.calls += 1
            if self.error: raise self.error
            value = page(self.name)
            return value if self.results else value.model_copy(update={"results": []})
        def close(self): pass
    fallback = Fake("tavily")
    with pytest.raises(WebSearchError, match="HTTP_401"):
        RoutedWebSearchAdapter(Fake("searxng", WebSearchError("HTTP_401")), [fallback]).search(query(), count=1)
    assert fallback.calls == 0
    empty_fallback = Fake("exa")
    result = RoutedWebSearchAdapter(Fake("searxng", results=False), [empty_fallback]).search(query(), count=1)
    assert result.results == [] and empty_fallback.calls == 0


def test_cross_provider_url_dedup_preserves_provider_provenance(app_settings):
    policy = load_source_policy(app_settings.web_source_policy_path)
    first, second = page("tavily"), page("exa")
    second.endpoint = EXA_ENDPOINT
    normalized = normalize_search_pages([first, second], policy)
    assert len(normalized) == 1
    assert {item["provider"] for item in normalized[0].provider_provenance} == {"tavily", "exa"}


def test_searxng_private_result_still_fails_general_fetch_ssrf(app_settings):
    policy = load_source_policy(app_settings.web_source_policy_path)
    unsafe = page("searxng", "https://internal.example/a")
    unsafe.endpoint = SEARXNG_ENDPOINT
    candidate = normalize_search_pages([unsafe], policy)[0]
    fetcher = WebPageFetcher(
        client=client(lambda _: pytest.fail("private URL must not reach transport")),
        resolver=lambda _: ["10.0.0.1"], minimum_interval_seconds=0,
    )
    fetched = fetcher.fetch(candidate, allowed_urls={candidate.canonical_url})
    assert fetched.error_code == "NON_PUBLIC_ADDRESS"
