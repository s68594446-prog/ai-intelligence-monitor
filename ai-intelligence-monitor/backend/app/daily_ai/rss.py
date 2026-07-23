"""RSS discovery adapter for the daily AI digest.

RSS titles and summaries are DISCOVERY_ONLY, exactly like Juhe and TianAPI:
they contribute original URLs and provenance but never become Evidence and
never raise Source Tier. Final digest content still requires a safe fetch of
the original page and exact evidence quotes.
"""
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from app.daily_ai.contracts import StableStatus
from app.web_discovery.contracts import ProviderSearchHit, ProviderSearchPage, SearchQuery


class RssError(RuntimeError):
    def __init__(self, code: StableStatus, *, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code.value)


def _text(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _parse_items(body: bytes) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RssError(StableStatus.INVALID_SCHEMA, detail="XML_PARSE_ERROR") from exc
    items = []
    for node in root.iter("item"):  # RSS 2.0
        link = _text(node.find("link"))
        if link:
            items.append({"title": _text(node.find("title")), "url": link,
                          "summary": _text(node.find("description")), "published": _text(node.find("pubDate"))})
    atom = "{http://www.w3.org/2005/Atom}"
    for node in root.iter(f"{atom}entry"):  # Atom
        link_node = node.find(f"{atom}link")
        link = (link_node.get("href") or "").strip() if link_node is not None else ""
        if link:
            items.append({"title": _text(node.find(f"{atom}title")), "url": link,
                          "summary": _text(node.find(f"{atom}summary")), "published": _text(node.find(f"{atom}updated"))})
    if not items:
        raise RssError(StableStatus.INVALID_SCHEMA, detail="NO_FEED_ITEMS")
    return items


class RssDiscoveryAdapter:
    name = "rss_discovery"
    version = "rss-discovery-v1"
    max_response_bytes = 2_000_000

    def __init__(self, *, client=None, timeout_seconds: float = 20):
        self.client = client or httpx.Client(
            timeout=timeout_seconds, follow_redirects=True, max_redirects=3, trust_env=False,
            headers={"User-Agent": "ai-intelligence-monitor-rss/1.0"},
        )
        self._owns_client = client is None

    def fetch_feed(self, *, feed_name: str, feed_url: str, max_items: int, query: SearchQuery) -> ProviderSearchPage:
        if not 1 <= max_items <= 20:
            raise ValueError("RSS max_items must be between 1 and 20")
        try:
            response = self.client.get(feed_url)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RssError(StableStatus.UPSTREAM_ERROR, detail=type(exc).__name__) from None
        if response.status_code == 429:
            raise RssError(StableStatus.RATE_LIMITED)
        if response.status_code != 200:
            raise RssError(StableStatus.UPSTREAM_ERROR, detail=f"HTTP_{response.status_code}")
        body = response.content
        if len(body) > self.max_response_bytes:
            raise RssError(StableStatus.INVALID_SCHEMA, detail="FEED_TOO_LARGE")
        items = _parse_items(body)[:max_items]
        now = datetime.now(timezone.utc)
        hits = [
            ProviderSearchHit(
                title=item["title"][:500] or "Untitled", url=item["url"],
                snippet=item["summary"][:2000], rank=rank, discovered_at=now,
                source_metadata={
                    "feed_name": feed_name, "published_at_reported": item["published"][:50],
                    "title_policy": "DISCOVERY_ONLY", "snippet_policy": "DISCOVERY_ONLY",
                    "eligible_as_evidence": False,
                },
            )
            for rank, item in enumerate(items, 1)
        ]
        return ProviderSearchPage(
            provider=f"rss_{feed_name}", adapter_version=self.version, query_id=query.query_id,
            endpoint=feed_url, request_count=1, retry_count=0,
            query_text_hash=hashlib.sha256(query.text.encode()).hexdigest(),
            raw_response_sha256=hashlib.sha256(body).hexdigest(), retrieval_status="SUCCESS",
            request_metadata={"item_count": len(hits), "feed_bytes": len(body)},
            results=hits,
            raw_response={"item_count": len(hits), "items": [{"url": hit.url, "published_at": hit.source_metadata["published_at_reported"]} for hit in hits]},
        )

    def close(self):
        if self._owns_client:
            self.client.close()
