import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import routes
from app.core.database import get_session
from app.core.enums import PipelineState
from app.main import app
from app.manual.contracts import ManualDocumentCreate
from app.manual.service import submit_manual_document
from app.models import Document
from app.source_collections.contracts import (
    CandidateStatus,
    CollectionRequest,
    CollectionResponse,
    DiscoveredItem,
    DiscoveryMethod,
    ExtractionStatus,
)
from app.source_collections.discovery import HTMLIndexSourceDiscovery, RSSSourceDiscovery
from app.source_collections.fetch import HTTPArticleFetcher
from app.source_collections.ops import SourceCollectionOpsChecker
from app.source_collections.prefilter import prefilter
from app.source_collections.registry import get_source, load_registry
from app.source_collections.replay import replay_frozen_source
from app.source_collections.security import (
    CollectionHTTPError,
    CollectionSecurityError,
    SafeHttpClient,
    canonicalize_url,
    validate_url,
)
from app.source_collections.service import collect_sources


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>NVIDIA Rubin AI factory expands HBM deployment</title>
<link>https://nvidianews.nvidia.com/news/rubin-hbm</link>
<description>Rubin AI servers increase memory bandwidth requirements.</description>
<pubDate>Thu, 16 Jul 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Rubin HBM platform update</title><link href="https://nvidianews.nvidia.com/news/rubin-atom"/>
<summary>AI factory memory bandwidth</summary><updated>2026-07-16T08:00:00Z</updated></entry></feed>"""

MICRON_INDEX = b"""<html><body><a href="/news-releases/news-release-details/hbm-production-update">
Micron HBM mass production and capacity update</a></body></html>"""

MICRON_INDEX_TWO = b"""<html><body>
<a href="/news-releases/news-release-details/hbm-production-update">Micron HBM production update</a>
<a href="/news-releases/news-release-details/hbm-production-update-copy">Micron HBM capacity update</a>
</body></html>"""

FILTERED_INDEX = b"""<html><body><a href="/news-releases/news-release-details/board-appointment">
Micron board appointment</a></body></html>"""

ARTICLE = b"""<html lang="en"><head>
<meta property="og:title" content="Micron HBM Production Update"/>
<meta property="article:published_time" content="2026-07-16T21:00:00Z"/></head>
<body><nav>Navigation noise</nav><article><p>Micron stated that HBM memory capacity entered mass production after customer qualification.</p>
<p>Exact customer order volume, pricing, yield and conventional DRAM allocation remain undisclosed.</p></article>
<script>ignore_secret()</script><footer>Footer noise</footer></body></html>"""


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def registry(app_settings):
    return load_registry(app_settings.source_registry_path)


def micron(app_settings):
    return get_source(registry(app_settings), "micron_ir_latest_news")


def nvidia(app_settings):
    return get_source(registry(app_settings), "nvidia_press_release_rss")


def safe(source, handler):
    return SafeHttpClient(source, user_agent="AIM-Test/1.0", client=client(handler), sleep_fn=lambda _: None)


def discovered(source_id="micron_ir_latest_news", title="Micron HBM capacity update", summary="HBM memory capacity"):
    return DiscoveredItem(
        source_id=source_id,
        title=title,
        canonical_url="https://investors.micron.com/news-releases/news-release-details/hbm-production-update",
        published_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        summary=summary,
        discovered_at=datetime.now(timezone.utc),
        discovery_method=DiscoveryMethod.HTML_INDEX,
    )


def micron_handler(index=MICRON_INDEX, article=ARTICLE):
    def handler(request):
        if request.url.path == "/latest-news-english":
            return httpx.Response(200, content=index, headers={"content-type": "text/html"}, request=request)
        if request.url.path.startswith("/news-releases/news-release-details/"):
            return httpx.Response(200, content=article, headers={"content-type": "text/html"}, request=request)
        return httpx.Response(404, request=request)
    return handler


def test_registry_read_and_version(app_settings):
    value = registry(app_settings)
    assert value.registry_version == "dram-hbm-sources-v1"
    assert value.coverage_id == "dram_hbm"


def test_registry_contains_exact_source_set(app_settings):
    assert [item.source_id for item in registry(app_settings).sources] == [
        "micron_ir_latest_news", "samsung_semiconductor_newsroom",
        "nvidia_press_release_rss", "sk_hynix_press_release",
    ]


def test_nonapproved_source_rejected(app_settings):
    with pytest.raises(KeyError, match="not approved"):
        get_source(registry(app_settings), "google_news")


def test_disabled_source_rejected(app_settings):
    with pytest.raises(ValueError, match="disabled"):
        get_source(registry(app_settings), "sk_hynix_press_release")


def test_rss_discovery_parses_metadata(app_settings):
    source = nvidia(app_settings)
    transport = safe(source, lambda req: httpx.Response(200, content=RSS, headers={"content-type": "application/rss+xml"}, request=req))
    items = RSSSourceDiscovery().discover(source, transport)
    assert items[0].title.startswith("NVIDIA Rubin")
    assert items[0].summary.startswith("Rubin AI servers")


def test_atom_discovery_parses_metadata(app_settings):
    source = nvidia(app_settings)
    transport = safe(source, lambda req: httpx.Response(200, content=ATOM, headers={"content-type": "application/atom+xml"}, request=req))
    assert RSSSourceDiscovery().discover(source, transport)[0].canonical_url.endswith("rubin-atom")


def test_html_index_discovery(app_settings):
    source = micron(app_settings)
    transport = safe(source, micron_handler())
    items = HTMLIndexSourceDiscovery().discover(source, transport)
    assert len(items) == 1
    assert items[0].canonical_url.endswith("hbm-production-update")


def test_html_index_challenge_is_classified_as_blocked(app_settings):
    source = micron(app_settings)
    challenge = b"<html><script>triggerInterstitialChallenge(); /* bm-verify */</script></html>"
    transport = safe(source, lambda req: httpx.Response(200, content=challenge, headers={"content-type": "text/html"}, request=req))
    with pytest.raises(CollectionHTTPError) as caught:
        HTMLIndexSourceDiscovery().discover(source, transport)
    assert caught.value.code == "BLOCKED_PAGE"


def test_canonical_url_removes_tracking_and_fragment():
    assert canonicalize_url("HTTPS://Example.COM/a//b?utm_source=x&b=2#a") == "https://example.com/a/b?b=2"


def test_allowed_domain_passes(app_settings):
    assert validate_url("https://investors.micron.com/latest-news-english", micron(app_settings)).startswith("https://")


def test_private_and_local_urls_blocked(app_settings):
    source = micron(app_settings).model_copy(update={"allowed_domains": ["127.0.0.1", "localhost"]})
    with pytest.raises(CollectionSecurityError):
        validate_url("https://127.0.0.1/article", source)
    with pytest.raises(CollectionSecurityError):
        validate_url("https://localhost/article", source)


def test_disallowed_redirect_blocked(app_settings):
    source = micron(app_settings)
    transport = safe(source, lambda req: httpx.Response(302, headers={"location": "https://evil.example/a"}, request=req))
    with pytest.raises(CollectionHTTPError, match="outside") as caught:
        transport.get(source.discovery_url)
    assert caught.value.code == "DISALLOWED_REDIRECT"


def test_403_is_not_retried(app_settings):
    calls = 0
    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(403, request=req)
    transport = safe(micron(app_settings), handler)
    with pytest.raises(CollectionHTTPError) as caught:
        transport.get(micron(app_settings).discovery_url)
    assert caught.value.code == "HTTP_403"
    assert calls == 1


def test_safe_client_requests_identity_encoding(app_settings):
    observed = {}
    def handler(req):
        observed["accept_encoding"] = req.headers.get("accept-encoding")
        return httpx.Response(200, content=MICRON_INDEX, request=req)
    transport = safe(micron(app_settings), handler)
    transport.get(micron(app_settings).discovery_url)
    assert observed["accept_encoding"] == "identity"


def test_5xx_retries_at_most_twice(app_settings):
    calls = 0
    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=req)
    transport = safe(micron(app_settings), handler)
    with pytest.raises(CollectionHTTPError) as caught:
        transport.get(micron(app_settings).discovery_url)
    assert caught.value.code == "HTTP_5XX"
    assert calls == 3 and transport.retry_count == 2


def test_article_extraction_success(app_settings):
    source = micron(app_settings)
    result = HTTPArticleFetcher().fetch(discovered(), source, safe(source, micron_handler()))
    assert result.extraction_status == ExtractionStatus.SUCCESS
    assert "mass production" in result.raw_text


def test_article_extraction_removes_page_noise(app_settings):
    source = micron(app_settings)
    text = HTTPArticleFetcher().fetch(discovered(), source, safe(source, micron_handler())).raw_text
    assert "Navigation noise" not in text
    assert "ignore_secret" not in text
    assert "Footer noise" not in text


def test_article_too_short_is_blocked(app_settings):
    source = micron(app_settings)
    tiny = b"<html><article><p>HBM update.</p></article></html>"
    result = HTTPArticleFetcher().fetch(discovered(), source, safe(source, micron_handler(article=tiny)))
    assert result.extraction_status == ExtractionStatus.TOO_SHORT


def test_metadata_only_does_not_succeed(app_settings):
    source = micron(app_settings)
    empty = b"<html><script>nothing()</script></html>"
    result = HTTPArticleFetcher().fetch(discovered(), source, safe(source, micron_handler(article=empty)))
    assert result.extraction_status == ExtractionStatus.METADATA_ONLY


def test_direct_relevance_prefilter(app_settings):
    result = prefilter(discovered(), micron(app_settings), {"products": ["HBM"], "monitoring_variables": []})
    assert result.candidate_status == CandidateStatus.CANDIDATE
    assert "hbm" in [item.lower() for item in result.matched_terms]


def test_dependency_sentinel_prefilter(app_settings):
    item = discovered("nvidia_press_release_rss", "NVIDIA Rubin AI factory deployment", "HBM memory bandwidth")
    item.canonical_url = "https://nvidianews.nvidia.com/news/rubin-hbm"
    result = prefilter(item, nvidia(app_settings), {"products": ["HBM"], "monitoring_variables": []})
    assert result.candidate_status == CandidateStatus.CANDIDATE


def test_generic_ai_news_filtered_out(app_settings):
    item = discovered("nvidia_press_release_rss", "New AI software toolkit", "Healthcare robot software")
    item.canonical_url = "https://nvidianews.nvidia.com/news/software"
    result = prefilter(item, nvidia(app_settings), {"products": ["HBM"], "monitoring_variables": []})
    assert result.candidate_status == CandidateStatus.FILTERED_OUT


def test_mixed_relevance_and_exclusion_needs_review(app_settings):
    item = discovered(title="HBM board appointment", summary="memory capacity")
    assert prefilter(item, micron(app_settings), {"products": [], "monitoring_variables": []}).candidate_status == CandidateStatus.NEEDS_MANUAL_REVIEW


def test_dry_run_creates_no_document_and_fetches_no_article(session, app_settings):
    calls = []
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, content=MICRON_INDEX, headers={"content-type": "text/html"}, request=req)
    result = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], dry_run=True, since_hours=720), app_settings=app_settings, clients={"micron_ir_latest_news": client(handler)}, sleep_fn=lambda _: None)
    assert result.document_count == 0
    assert calls == ["/latest-news-english"]


def test_collection_creates_document_through_ingestion(session, app_settings):
    result = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], since_hours=720), app_settings=app_settings, clients={"micron_ir_latest_news": client(micron_handler())}, sleep_fn=lambda _: None)
    document = session.get(Document, json.loads(Path(result.artifact_path).read_text())["documents_created"][0])
    assert document.pipeline_state == PipelineState.COLLECTED
    assert document.fixture_payload["collection_run_id"] == result.run_id


def test_canonical_url_duplicate_reuses_document(session, app_settings):
    kwargs = dict(app_settings=app_settings, clients={"micron_ir_latest_news": client(micron_handler())}, sleep_fn=lambda _: None)
    first = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], since_hours=720), **kwargs)
    second = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], since_hours=720), **kwargs)
    assert first.document_count == 1
    assert second.duplicate_count == 1 and second.document_count == 0


def test_content_hash_duplicate_reuses_document(session, app_settings):
    result = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], max_items_per_source=2, since_hours=720), app_settings=app_settings, clients={"micron_ir_latest_news": client(micron_handler(index=MICRON_INDEX_TWO))}, sleep_fn=lambda _: None)
    assert result.document_count == 1
    assert result.duplicate_count == 1


def test_filtered_item_never_fetches_article(session, app_settings):
    paths = []
    def handler(req):
        paths.append(req.url.path)
        return httpx.Response(200, content=FILTERED_INDEX, headers={"content-type": "text/html"}, request=req)
    result = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], since_hours=720), app_settings=app_settings, clients={"micron_ir_latest_news": client(handler)}, sleep_fn=lambda _: None)
    assert result.filtered_count == 1 and result.fetched_count == 0
    assert paths == ["/latest-news-english"]


def test_collection_artifact_is_complete(session, app_settings):
    result = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], dry_run=True, since_hours=720), app_settings=app_settings, clients={"micron_ir_latest_news": client(micron_handler())}, sleep_fn=lambda _: None)
    payload = json.loads(Path(result.artifact_path).read_text())
    required = {"registry_version", "coverage_version", "per_source_results", "discovered_items", "prefilter_results", "fetch_results", "duplicates", "filtered_items", "failures", "documents_created", "documents_reused", "analyzed_document_ids", "request_count", "retry_count"}
    assert required <= payload.keys()


def test_source_ops_check_passes_valid_collection(session, app_settings):
    result = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], dry_run=True, since_hours=720), app_settings=app_settings, clients={"micron_ir_latest_news": client(micron_handler())}, sleep_fn=lambda _: None)
    assert json.loads(Path(result.artifact_path).read_text())["ops_check"]["passed"] is True


def test_source_ops_check_blocks_illegal_source(session, app_settings):
    result = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], dry_run=True, since_hours=720), app_settings=app_settings, clients={"micron_ir_latest_news": client(micron_handler())}, sleep_fn=lambda _: None)
    payload = json.loads(Path(result.artifact_path).read_text())
    payload["per_source_results"][0]["source_id"] = "evil"
    assert SourceCollectionOpsChecker(session).check(payload)["passed"] is False


def test_frozen_source_replay_is_stable(session, app_settings):
    result = collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], dry_run=True, since_hours=720), app_settings=app_settings, clients={"micron_ir_latest_news": client(micron_handler())}, sleep_fn=lambda _: None)
    replay, diff = replay_frozen_source(session, result.run_id, app_settings=app_settings)
    assert replay.replay_of == result.run_id
    assert diff["stable"] is True


def test_duplicate_does_not_call_analyzer(session, app_settings):
    calls = []
    kwargs = dict(app_settings=app_settings, clients={"micron_ir_latest_news": client(micron_handler())}, sleep_fn=lambda _: None, analyzer=lambda db, document_id: calls.append(document_id))
    collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], since_hours=720), **kwargs)
    calls.clear()
    collect_sources(session, CollectionRequest(source_ids=["micron_ir_latest_news"], since_hours=720, analyze=True), **kwargs)
    assert calls == []


def test_manual_ingestion_remains_functional(session):
    created = submit_manual_document(session, ManualDocumentCreate(title="Manual HBM source", source_name="Manual Source", source_tier="T1", published_at="2026-07-17T00:00:00Z", language="en", raw_text="Manual HBM source text remains fully supported after controlled collection changes."))
    assert session.get(Document, created.document_id).pipeline_state == PipelineState.COLLECTED


def test_source_collection_api(session, monkeypatch):
    expected = CollectionResponse(run_id="source-run", status="PASSED", sources_requested=["micron_ir_latest_news"], discovered_count=1, candidate_count=1, fetched_count=0, document_count=0, duplicate_count=0, filtered_count=0, error_count=0, artifact_path="/tmp/00-source-fetch.json")
    monkeypatch.setattr(routes, "collect_sources", lambda db, payload: expected)
    app.dependency_overrides[get_session] = lambda: iter([session])
    try:
        with TestClient(app) as api:
            response = api.post("/api/source-collections", json={"source_ids": ["micron_ir_latest_news"], "dry_run": True})
            assert response.status_code == 200
            assert response.json()["run_id"] == "source-run"
    finally:
        app.dependency_overrides.clear()
