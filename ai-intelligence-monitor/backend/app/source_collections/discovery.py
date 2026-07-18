import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

from app.source_collections.contracts import ApprovedSource, DiscoveredItem, DiscoveryMethod
from app.source_collections.security import CollectionHTTPError, SafeHttpClient, canonicalize_url, validate_url


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _IndexParser(HTMLParser):
    def __init__(self, base_url: str, patterns: list[str]):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.patterns = patterns
        self.current: dict | None = None
        self.items: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            url = canonicalize_url(urljoin(self.base_url, attributes["href"]))
            if any(re.search(pattern, url) for pattern in self.patterns):
                self.current = {"url": url, "title": attributes.get("title", ""), "summary": "", "published_at": None}
        if self.current is not None and tag == "time":
            self.current["published_at"] = _date(attributes.get("datetime"))

    def handle_data(self, data):
        if self.current is not None:
            self.current["title"] += " " + data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.current["title"] = _clean(self.current["title"])
            if self.current["title"]:
                self.items.append(self.current)
            self.current = None


class SourceDiscoveryAdapter:
    def discover(self, source: ApprovedSource, client: SafeHttpClient) -> list[DiscoveredItem]:
        raise NotImplementedError


class RSSSourceDiscovery(SourceDiscoveryAdapter):
    def discover(self, source: ApprovedSource, client: SafeHttpClient) -> list[DiscoveredItem]:
        response = client.get(source.discovery_url, accept="application/rss+xml, application/atom+xml, application/xml, text/xml")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise CollectionHTTPError("INVALID_FEED", "official RSS/Atom feed is invalid") from exc
        now = datetime.now(timezone.utc)
        items: list[DiscoveredItem] = []
        nodes = root.findall("./channel/item")
        if not nodes:
            nodes = root.findall("{http://www.w3.org/2005/Atom}entry")
        for node in nodes[: source.max_items_per_run]:
            atom = node.tag.endswith("entry")
            if atom:
                title = node.findtext("{http://www.w3.org/2005/Atom}title") or ""
                link_node = node.find("{http://www.w3.org/2005/Atom}link")
                link = link_node.attrib.get("href", "") if link_node is not None else ""
                summary = node.findtext("{http://www.w3.org/2005/Atom}summary") or ""
                published = node.findtext("{http://www.w3.org/2005/Atom}published") or node.findtext("{http://www.w3.org/2005/Atom}updated")
            else:
                title = node.findtext("title") or ""
                link = node.findtext("link") or ""
                summary = node.findtext("description") or ""
                published = node.findtext("pubDate")
            canonical = validate_url(link, source, article=True)
            items.append(DiscoveredItem(
                source_id=source.source_id,
                title=_clean(title),
                canonical_url=canonical,
                published_at=_date(published),
                summary=_clean(re.sub(r"<[^>]+>", " ", summary)),
                discovered_at=now,
                discovery_method=DiscoveryMethod.RSS,
            ))
        return items


class HTMLIndexSourceDiscovery(SourceDiscoveryAdapter):
    def discover(self, source: ApprovedSource, client: SafeHttpClient) -> list[DiscoveredItem]:
        response = client.get(source.discovery_url, accept="text/html, application/xhtml+xml")
        parser = _IndexParser(str(response.url), source.article_url_patterns)
        parser.feed(response.text)
        parser.close()
        now = datetime.now(timezone.utc)
        seen = set()
        items = []
        for item in parser.items:
            url = validate_url(item["url"], source, article=True)
            if url in seen:
                continue
            seen.add(url)
            items.append(DiscoveredItem(
                source_id=source.source_id,
                title=item["title"],
                canonical_url=url,
                published_at=item["published_at"],
                summary=item["summary"],
                discovered_at=now,
                discovery_method=DiscoveryMethod.HTML_INDEX,
            ))
            if len(items) >= source.max_items_per_run:
                break
        if not items:
            challenge_markers = (
                "triggerinterstitialchallenge",
                "bm-verify",
                "powered and protected by privacy",
            )
            lowered = response.text.lower()
            if any(marker in lowered for marker in challenge_markers):
                raise CollectionHTTPError("BLOCKED_PAGE", "official HTML index returned an interstitial challenge")
            raise CollectionHTTPError("PARSE_ERROR", "HTML index produced no approved article links")
        return items
