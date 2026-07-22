import httpx
import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.daily_ai.contracts import StableStatus
from app.daily_ai.juhe import (
    JUHE_AI_BRIEF_ENDPOINT,
    JUHE_HEADLINES_ENDPOINT,
    JuheAiBriefAdapter,
    JuheError,
    JuheHeadlinesAdapter,
)
from app.web_discovery.contracts import SearchQuery
from app.web_discovery.normalize import normalize_search_pages
from app.web_discovery.source_policy import WebSourcePolicy


def query():
    return SearchQuery(query_id="daily_cn_ai", text="中国 人工智能 科技", variable="daily_cn_ai", purpose="discovery", language="zh", time_range="24h", plan_version="daily-ai-v1")


def test_settings_default_juhe_disabled_and_stock_never_enabled(monkeypatch):
    for name in ("JUHE_HEADLINES_ENABLED", "JUHE_HEADLINES_API_KEY", "JUHE_AI_BRIEF_ENABLED", "JUHE_AI_BRIEF_API_KEY", "JUHE_STOCK_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert not settings.juhe_headlines_enabled
    assert not settings.juhe_ai_brief_enabled
    assert not settings.juhe_stock_enabled


def test_headlines_fixed_endpoint_technology_first_page_and_discovery_only():
    captured = {}
    def handler(request):
        captured["url"] = request.url
        return httpx.Response(200, json={"error_code": 0, "reason": "success", "result": {"data": [{"uniquekey": "h1", "title": "科技新闻", "date": "2026-07-22", "category": "科技", "author_name": "source", "url": "https://example.com/a"}]}})
    adapter = JuheHeadlinesAdapter(api_key=SecretStr("headlines-secret"), client=httpx.Client(transport=httpx.MockTransport(handler)))
    page = adapter.search(query(), count=3)
    assert str(captured["url"]).startswith(JUHE_HEADLINES_ENDPOINT)
    assert captured["url"].params["type"] == "keji"
    assert captured["url"].params["page"] == "1"
    assert captured["url"].params["page_size"] == "3"
    assert page.request_count == 1 and page.results[0].source_metadata["eligible_as_evidence"] is False
    assert "headlines-secret" not in page.model_dump_json()


def test_ai_brief_fixed_current_endpoint_and_summary_is_discovery_only():
    captured = {}
    def handler(request):
        captured["url"] = request.url
        return httpx.Response(200, json={"error_code": 0, "reason": "success", "result": {"list": [{"id": "b1", "title": "AI新闻", "summary": "generated summary", "type": "tech", "publish_date": "2026-07-22", "url": "https://example.com/b"}]}})
    adapter = JuheAiBriefAdapter(api_key=SecretStr("brief-secret"), client=httpx.Client(transport=httpx.MockTransport(handler)))
    page = adapter.search(query(), count=2)
    assert str(captured["url"]).startswith(JUHE_AI_BRIEF_ENDPOINT)
    assert captured["url"].params["type"] == "tech"
    assert captured["url"].params["page"] == "1"
    assert page.results[0].snippet == "generated summary"
    assert page.results[0].source_metadata["snippet_policy"] == "DISCOVERY_ONLY"
    assert "brief-secret" not in page.model_dump_json()


def test_keys_are_independent_and_not_mixed():
    seen = []
    def handler(request):
        seen.append((request.url.host, request.url.params["key"]))
        payload = {"data": []} if request.url.host == "v.juhe.cn" else {"list": []}
        return httpx.Response(200, json={"error_code": 0, "result": payload})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    JuheHeadlinesAdapter(api_key=SecretStr("headlines-only"), client=client).search(query())
    JuheAiBriefAdapter(api_key=SecretStr("brief-only"), client=client).search(query())
    assert seen == [("v.juhe.cn", "headlines-only"), ("apis.juhe.cn", "brief-only")]


@pytest.mark.parametrize("code,status", [(10001, StableStatus.AUTHENTICATION_ERROR), (10002, StableStatus.PERMISSION_DENIED), (10003, StableStatus.KEY_EXPIRED), (10012, StableStatus.QUOTA_EXCEEDED), (10020, StableStatus.MAINTENANCE), (10021, StableStatus.ENDPOINT_DISABLED)])
def test_http_200_business_errors_map_to_stable_codes_without_secret(code, status):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"error_code": code, "reason": "provider detail"})))
    adapter = JuheHeadlinesAdapter(api_key=SecretStr("never-leak-secret"), client=client)
    with pytest.raises(JuheError) as caught:
        adapter.search(query())
    assert caught.value.code == status
    assert caught.value.business_code == code
    assert "never-leak-secret" not in repr(caught.value)


def test_missing_key_rate_limit_invalid_schema_and_count_limit_fail_closed():
    with pytest.raises(JuheError) as caught:
        JuheHeadlinesAdapter(api_key=None)
    assert caught.value.code == StableStatus.MISSING_API_KEY
    rate = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429)))
    with pytest.raises(JuheError) as caught:
        JuheHeadlinesAdapter(api_key=SecretStr("x"), client=rate).search(query())
    assert caught.value.code == StableStatus.RATE_LIMITED
    invalid = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"error_code": 0, "result": {"data": "wrong"}})))
    with pytest.raises(JuheError) as caught:
        JuheHeadlinesAdapter(api_key=SecretStr("x"), client=invalid).search(query())
    assert caught.value.code == StableStatus.INVALID_SCHEMA
    with pytest.raises(ValueError):
        JuheHeadlinesAdapter(api_key=SecretStr("x"), client=invalid).search(query(), count=6)


def test_cross_provider_canonical_url_dedup_keeps_provenance():
    def response(item_key):
        result = {item_key: [{"id": "x", "uniquekey": "x", "title": "Same event", "summary": "discovery", "url": "https://example.com/event?utm_source=x"}]}
        return httpx.Response(200, json={"error_code": 0, "result": result})
    headlines = JuheHeadlinesAdapter(api_key=SecretStr("h"), client=httpx.Client(transport=httpx.MockTransport(lambda request: response("data")))).search(query())
    brief = JuheAiBriefAdapter(api_key=SecretStr("b"), client=httpx.Client(transport=httpx.MockTransport(lambda request: response("list")))).search(query())
    policy = WebSourcePolicy(policy_version="test", coverage_id="daily_ai", t1_domains=[], t2_domains=[], t3_domains=[], rules={})
    normalized = normalize_search_pages([headlines, brief], policy)
    assert len(normalized) == 1
    assert {item["provider"] for item in normalized[0].provider_provenance} == {"juhe_headlines", "juhe_ai_brief"}
