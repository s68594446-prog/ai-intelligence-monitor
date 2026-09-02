"""Bounded official discovery for material U.S. Treasury financing events.

This adapter is event-driven: it does not publish a daily yields panel and it
does not turn listing-page text into evidence.  Matching releases still pass
safe fetch, v9 plus the macro constraints, exact-quote validation, importance
selection, and Ops before they can appear in the ordinary MACRO_POLICY section.
"""

import hashlib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from app.daily_ai.contracts import StableStatus
from app.web_discovery.contracts import ProviderSearchHit, ProviderSearchPage, SearchQuery


TREASURY_PRESS_RELEASES_URL = "https://home.treasury.gov/news/press-releases"
_RELEASE_PATH = re.compile(r"^/news/press-releases/[a-z0-9-]+/?$", re.I)
_MATERIAL_TERMS = (
    "marketable borrowing",
    "borrowing estimates",
    "financing estimates",
    "quarterly refunding",
    "treasury securities",
    "treasury market",
    "auction",
    "buyback",
    "debt limit",
    "debt ceiling",
    "extraordinary measures",
    "cash balance",
    "liquidity support",
)


class MacroDiscoveryError(RuntimeError):
    def __init__(self, code: StableStatus, *, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code.value)


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _TreasuryReleaseParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pending_datetime = ""
        self.active_href = ""
        self.active_text: list[str] = []
        self.items: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        values = dict(attrs)
        if tag == "time" and values.get("datetime"):
            self.pending_datetime = values["datetime"] or ""
        elif tag == "a" and _RELEASE_PATH.fullmatch(values.get("href") or ""):
            self.active_href = values["href"] or ""
            self.active_text = []

    def handle_data(self, data: str):
        if self.active_href:
            self.active_text.append(data)

    def handle_endtag(self, tag: str):
        if tag != "a" or not self.active_href:
            return
        title = " ".join("".join(self.active_text).split())
        if title and any(term in title.casefold() for term in _MATERIAL_TERMS):
            self.items.append(
                {
                    "title": title,
                    "href": self.active_href,
                    "published_at": _parse_datetime(self.pending_datetime),
                    "published_at_reported": self.pending_datetime[:50],
                }
            )
        self.active_href = ""
        self.active_text = []
        self.pending_datetime = ""


def parse_treasury_releases(body: bytes) -> list[dict]:
    parser = _TreasuryReleaseParser()
    try:
        parser.feed(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MacroDiscoveryError(
            StableStatus.INVALID_SCHEMA, detail="HTML_PARSE_ERROR"
        ) from exc

    by_url: dict[str, dict] = {}
    for item in parser.items:
        url = urljoin(TREASURY_PRESS_RELEASES_URL, item["href"])
        if urlsplit(url).hostname != "home.treasury.gov":
            continue
        existing = by_url.get(url)
        if existing is None or (
            existing["published_at"] is None and item["published_at"] is not None
        ):
            by_url[url] = {**item, "url": url}
    return list(by_url.values())


class TreasuryPressReleaseDiscoveryAdapter:
    name = "treasury_press_releases"
    version = "treasury-press-release-discovery-v1"
    max_response_bytes = 2_000_000

    def __init__(self, *, client=None, timeout_seconds: float = 20):
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "ai-intelligence-monitor-macro/1.0"},
        )
        self._owns_client = client is None

    def search(self, query: SearchQuery, *, max_items: int = 4) -> ProviderSearchPage:
        if not 1 <= max_items <= 10:
            raise ValueError("Treasury max_items must be between 1 and 10")
        try:
            response = self.client.get(TREASURY_PRESS_RELEASES_URL)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MacroDiscoveryError(
                StableStatus.UPSTREAM_ERROR, detail=type(exc).__name__
            ) from None
        if response.status_code == 429:
            raise MacroDiscoveryError(StableStatus.RATE_LIMITED)
        if response.status_code != 200:
            raise MacroDiscoveryError(
                StableStatus.UPSTREAM_ERROR, detail=f"HTTP_{response.status_code}"
            )
        body = response.content
        if len(body) > self.max_response_bytes:
            raise MacroDiscoveryError(
                StableStatus.INVALID_SCHEMA, detail="PAGE_TOO_LARGE"
            )
        if "html" not in response.headers.get("content-type", "").casefold():
            raise MacroDiscoveryError(
                StableStatus.INVALID_SCHEMA, detail="INVALID_CONTENT_TYPE"
            )

        items = parse_treasury_releases(body)[:max_items]
        now = datetime.now(timezone.utc)
        hits = [
            ProviderSearchHit(
                title=item["title"][:500],
                url=item["url"],
                rank=rank,
                discovered_at=now,
                published_at=item["published_at"],
                source_metadata={
                    "listing": TREASURY_PRESS_RELEASES_URL,
                    "published_at_reported": item["published_at_reported"],
                    "title_policy": "DISCOVERY_ONLY",
                    "snippet_policy": "DISCOVERY_ONLY",
                    "eligible_as_evidence": False,
                },
            )
            for rank, item in enumerate(items, 1)
        ]
        return ProviderSearchPage(
            provider=self.name,
            adapter_version=self.version,
            query_id=query.query_id,
            endpoint=TREASURY_PRESS_RELEASES_URL,
            request_count=1,
            retry_count=0,
            query_text_hash=hashlib.sha256(query.text.encode()).hexdigest(),
            raw_response_sha256=hashlib.sha256(body).hexdigest(),
            request_metadata={"item_count": len(hits), "page_bytes": len(body)},
            results=hits,
            raw_response={
                "item_count": len(hits),
                "items": [
                    {
                        "url": hit.url,
                        "published_at": hit.source_metadata["published_at_reported"],
                    }
                    for hit in hits
                ],
            },
        )

    def close(self):
        if self._owns_client:
            self.client.close()
