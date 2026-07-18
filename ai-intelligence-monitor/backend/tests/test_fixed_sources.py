import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes
from app.core.database import get_session
from app.core.enums import FetchStatus, PipelineState
from app.fixed_sources.collector import FixedSourceParseError, FixedSourceSecurityError
from app.fixed_sources.contracts import FixedFetchResponse
from app.fixed_sources.service import fetch_fixed_source, list_fixed_sources
from app.main import app
from app.manual.collector import ManualCollector
from app.models import Document, SourceFetchAudit


NVIDIA_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>NVIDIA</title>
<item>
  <title>NVIDIA Rubin AI Factory Expands HBM Memory Demand</title>
  <link>https://nvidianews.nvidia.com/news/rubin-memory-update</link>
  <categories><category>Press Releases</category></categories>
  <content>&amp;lt;![CDATA[&amp;lt;p&amp;gt;NVIDIA said Rubin systems require expanded HBM memory capacity for AI factory deployments.&amp;lt;/p&amp;gt;]]&amp;gt;</content>
  <description>Rubin systems require expanded HBM memory capacity.</description>
  <pubDate>Thu, 16 Jul 2026 08:00:00 GMT</pubDate>
</item>
<item>
  <title>Gaming update</title>
  <link>https://blogs.nvidia.com/blog/gaming</link>
  <category>Blogs</category>
  <description>Unrelated gaming news.</description>
  <pubDate>Thu, 16 Jul 2026 07:00:00 GMT</pubDate>
</item>
</channel></rss>"""


MICRON_INDEX = b"""<html><body>
<div class="search-result">
  <div class="result-title"><h4><a href="/news-releases/news-release-details/hbm-update">Micron HBM Update</a></h4></div>
  <p class="date">Jul 16, 2026</p>
  <div class="result-snippet">HBM memory capacity and customer qualification update.</div>
</div>
<div class="search-result">
  <div class="result-title"><h4><a href="/news-releases/news-release-details/board-update">Board Update</a></h4></div>
  <p class="date">Jul 15, 2026</p>
  <div class="result-snippet">Director appointment.</div>
</div>
</body></html>"""


MICRON_ARTICLE = b"""<html><body><div id="ndq-article"><article>
<div class="field field--name-field-nir-news-date"><div class="field__item">July 16, 2026 at 5:00 PM EDT</div></div>
<div class="field field--name-field-nir-news-title"><div class="field__item">Micron HBM Update</div></div>
<div class="node__content"><p>Micron said HBM memory capacity will increase after customer qualification.</p>
<script>secret_noise()</script><p>Actual shipment volume remains undisclosed.</p></div>
</article></div></body></html>"""


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_registry_is_small_fixed_t1_allowlist(app_settings):
    sources = list_fixed_sources(app_settings)
    assert [source.key for source in sources] == [
        "micron_ir",
        "nvidia_newsroom",
        "samsung_global_newsroom",
    ]
    assert all(source.trust_tier.value == "T1" for source in sources)
    assert all(source.active for source in sources)


def test_rss_collection_normalizes_persists_and_deduplicates(session, app_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://nvidianews.nvidia.com/releases.xml"
        assert request.headers["accept"].startswith("application/rss+xml")
        return httpx.Response(
            200,
            content=NVIDIA_RSS,
            headers={"content-type": "application/rss+xml", "etag": '"nvidia-v1"'},
            request=request,
        )

    first = fetch_fixed_source(
        session,
        "nvidia_newsroom",
        limit=2,
        app_settings=app_settings,
        client=_client(handler),
    )
    assert first.status == FetchStatus.PASSED
    assert first.documents_created == 1
    assert first.duplicates_found == 0
    document = session.get(Document, first.document_ids[0])
    assert document.pipeline_state == PipelineState.COLLECTED
    assert "expanded HBM memory capacity" in document.raw_text
    assert "<![CDATA[" not in document.raw_text
    assert document.fixture_payload["source_key"] == "nvidia_newsroom"
    batch = ManualCollector(session, document.id).collect("fixed-source-test")
    assert batch.documents[0].raw_text == document.raw_text

    second = fetch_fixed_source(
        session,
        "nvidia_newsroom",
        limit=2,
        app_settings=app_settings,
        client=_client(handler),
    )
    assert second.documents_created == 0
    assert second.duplicates_found == 1
    assert second.duplicate_document_ids == [document.id]
    with open(second.artifact_path, encoding="utf-8") as artifact:
        assert json.load(artifact)["duplicates_found"] == 1


def test_conditional_fetch_records_not_modified(session, app_settings):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert request.headers["if-none-match"] == '"nvidia-v1"'
            return httpx.Response(304, headers={"etag": '"nvidia-v1"'}, request=request)
        return httpx.Response(
            200,
            content=NVIDIA_RSS,
            headers={"content-type": "application/rss+xml", "etag": '"nvidia-v1"'},
            request=request,
        )

    client = _client(handler)
    fetch_fixed_source(session, "nvidia_newsroom", limit=1, app_settings=app_settings, client=client)
    result = fetch_fixed_source(
        session, "nvidia_newsroom", limit=1, app_settings=app_settings, client=client
    )
    assert result.status == FetchStatus.NOT_MODIFIED
    assert result.candidates_found == 0
    assert result.documents_created == 0


def test_micron_html_collection_uses_fixed_article_contract(session, app_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/latest-news-english":
            assert request.headers["accept"].startswith("text/html")
            body = MICRON_INDEX
        elif request.url.path == "/news-releases/news-release-details/hbm-update":
            body = MICRON_ARTICLE
        else:
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/html; charset=UTF-8"},
            request=request,
        )

    result = fetch_fixed_source(
        session,
        "micron_ir",
        limit=3,
        app_settings=app_settings,
        client=_client(handler),
    )
    assert result.documents_created == 1
    document = session.get(Document, result.document_ids[0])
    assert document.title == "Micron HBM Update"
    assert document.published_at.isoformat().startswith("2026-07-16T21:00:00")
    assert "Actual shipment volume remains undisclosed." in document.raw_text
    assert "secret_noise" not in document.raw_text


def test_redirect_outside_allowlist_fails_and_is_audited(session, app_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/data"}, request=request)

    with pytest.raises(FixedSourceSecurityError, match="outside the fixed-source allowlist"):
        fetch_fixed_source(
            session,
            "nvidia_newsroom",
            limit=1,
            app_settings=app_settings,
            client=_client(handler),
        )
    audit = session.scalar(select(SourceFetchAudit))
    assert audit.status == FetchStatus.FAILED
    assert "FixedSourceSecurityError" in audit.errors[0]
    assert audit.artifact_path


def test_micron_antibot_page_fails_closed_with_response_audit(session, app_settings):
    body = b'<html><head><meta http-equiv="refresh" content="5; URL=?bm-verify=token"></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/html"},
            request=request,
        )

    with pytest.raises(FixedSourceParseError, match="anti-bot verification page"):
        fetch_fixed_source(
            session,
            "micron_ir",
            limit=1,
            app_settings=app_settings,
            client=_client(handler),
        )
    audit = session.scalar(select(SourceFetchAudit))
    assert audit.status == FetchStatus.FAILED
    assert audit.http_status == 200
    assert audit.response_bytes == len(body)
    assert audit.response_sha256


def test_fixed_source_api_lists_and_triggers_without_arbitrary_url(
    session,
    app_settings,
    monkeypatch,
):
    expected = FixedFetchResponse(
        fetch_id="fetch-test",
        source_key="nvidia_newsroom",
        status=FetchStatus.PASSED,
        candidates_found=1,
        documents_created=1,
        duplicates_found=0,
        document_ids=[42],
        duplicate_document_ids=[],
        artifact_path="/tmp/fetch-test.json",
        errors=[],
    )
    monkeypatch.setattr(routes, "list_fixed_sources", lambda: list_fixed_sources(app_settings))
    monkeypatch.setattr(routes, "fetch_fixed_source", lambda db, key, limit=None: expected)
    for number in range(3):
        session.add(
            SourceFetchAudit(
                fetch_id=f"fetch-api-{number}",
                source_key="nvidia_newsroom",
                source_name="NVIDIA Newsroom Press Releases",
                status=FetchStatus.PASSED,
                request_url="https://nvidianews.nvidia.com/releases.xml",
            )
        )
    session.commit()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            sources = client.get("/api/fixed-sources")
            assert sources.status_code == 200
            assert len(sources.json()) == 3
            fetched = client.post("/api/fixed-sources/nvidia_newsroom/fetch?limit=1")
            assert fetched.status_code == 200
            assert fetched.json()["document_ids"] == [42]
            assert client.post("/api/fixed-sources/nvidia_newsroom/fetch?limit=11").status_code == 422
            audits = client.get("/api/fetch-audits?limit=2")
            assert audits.status_code == 200
            assert len(audits.json()) == 2
            assert client.get("/api/fetch-audits?limit=0").status_code == 422
    finally:
        app.dependency_overrides.clear()
