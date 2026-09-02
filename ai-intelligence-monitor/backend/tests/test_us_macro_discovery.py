from datetime import datetime, timezone

import httpx
import pytest

from app.daily_ai.contracts import StableStatus
from app.daily_ai.us_macro_discovery import (
    TREASURY_PRESS_RELEASES_URL,
    MacroDiscoveryError,
    TreasuryPressReleaseDiscoveryAdapter,
    parse_treasury_releases,
)
from app.web_discovery.contracts import SearchQuery


HTML = b"""<html><body>
<time datetime="2026-08-19T12:30:00Z">August 19</time>
<a href="/news/press-releases/sb0607">Treasury Announces Increased Sizes of Nominal Long-End Liquidity Support Buybacks</a>
<a href="/news/press-releases/sb0607">Treasury Announces Increased Sizes of Nominal Long-End Liquidity Support Buybacks</a>
<time datetime="2026-08-18T15:00:00Z">August 18</time>
<a href="/news/press-releases/sb0601">Treasury Announces Marketable Borrowing Estimates</a>
<time datetime="2026-08-18T10:00:00Z">August 18</time>
<a href="/news/press-releases/sb0599">Treasury Sanctions Unrelated Network</a>
<time datetime="2026-08-17T10:00:00Z">August 17</time>
<a href="/news/press-releases/sb0598">Treasury International Capital Data for June</a>
</body></html>"""


def _query() -> SearchQuery:
    return SearchQuery(
        query_id="official_treasury_press_releases",
        text="official treasury financing releases",
        variable="us_treasury_financing",
        purpose="official debt-management events",
        language="en",
        time_range="48h",
        plan_version="test-v1",
    )


def _client(response_fn):
    return httpx.Client(transport=httpx.MockTransport(response_fn))


def test_parser_keeps_material_events_and_deduplicates_urls():
    items = parse_treasury_releases(HTML)
    assert [item["url"] for item in items] == [
        "https://home.treasury.gov/news/press-releases/sb0607",
        "https://home.treasury.gov/news/press-releases/sb0601",
    ]
    assert items[0]["published_at"] == datetime(
        2026, 8, 19, 12, 30, tzinfo=timezone.utc
    )


def test_adapter_uses_fixed_endpoint_and_discovery_only_metadata():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200,
            content=HTML,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    adapter = TreasuryPressReleaseDiscoveryAdapter(client=_client(handler))
    page = adapter.search(_query())
    assert seen == [TREASURY_PRESS_RELEASES_URL]
    assert len(page.results) == 2
    assert page.results[0].source_metadata["eligible_as_evidence"] is False
    assert page.results[0].snippet == ""
    assert "Sanctions" not in " ".join(hit.title for hit in page.results)
    assert "International Capital" not in " ".join(hit.title for hit in page.results)


@pytest.mark.parametrize(
    ("status", "content_type", "expected"),
    [
        (429, "text/html", StableStatus.RATE_LIMITED),
        (500, "text/html", StableStatus.UPSTREAM_ERROR),
        (200, "application/json", StableStatus.INVALID_SCHEMA),
    ],
)
def test_adapter_classifies_failures_without_response_body(status, content_type, expected):
    def handler(request):
        return httpx.Response(
            status,
            content=b"secret upstream body",
            headers={"content-type": content_type},
            request=request,
        )

    adapter = TreasuryPressReleaseDiscoveryAdapter(client=_client(handler))
    with pytest.raises(MacroDiscoveryError) as caught:
        adapter.search(_query())
    assert caught.value.code == expected
    assert "secret" not in caught.value.detail
