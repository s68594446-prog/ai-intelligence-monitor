import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.core.enums import FetchStatus
from app.fixed_sources.contracts import CollectorResult, FetchCandidate, FixedSourceConfig


class FixedSourceError(RuntimeError):
    """Base error for a controlled official-source fetch."""


class FixedSourceSecurityError(FixedSourceError):
    """Raised when a URL leaves the configured HTTPS allowlist."""


class FixedSourceParseError(FixedSourceError):
    """Raised when a registered source no longer matches its expected contract."""


class _TextParser(HTMLParser):
    BLOCK_TAGS = {
        "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ol", "p", "section", "table", "td", "th", "tr", "ul",
    }
    SKIP_TAGS = {"iframe", "noscript", "script", "style", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return _normalize_text("".join(self.parts))


class _MicronIndexParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.result_depth: int | None = None
        self.link_depth: int | None = None
        self.date_depth: int | None = None
        self.snippet_depth: int | None = None
        self.current: dict[str, str] | None = None
        self.items: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.depth += 1
        attributes = dict(attrs)
        classes = set(attributes.get("class", "").split())
        if tag == "div" and "search-result" in classes and self.result_depth is None:
            self.result_depth = self.depth
            self.current = {"title": "", "url": "", "date": "", "snippet": ""}
            return
        if self.current is None:
            return
        if tag == "a" and self.link_depth is None and attributes.get("href", "").startswith(
            "/news-releases/news-release-details/"
        ):
            self.link_depth = self.depth
            self.current["url"] = attributes["href"]
        elif tag == "p" and "date" in classes:
            self.date_depth = self.depth
        elif tag == "div" and "result-snippet" in classes:
            self.snippet_depth = self.depth

    def handle_endtag(self, tag: str) -> None:
        if self.link_depth == self.depth:
            self.link_depth = None
        if self.date_depth == self.depth:
            self.date_depth = None
        if self.snippet_depth == self.depth:
            self.snippet_depth = None
        if self.result_depth == self.depth:
            if self.current and self.current["url"] and self.current["title"].strip():
                self.items.append({key: _normalize_text(value) for key, value in self.current.items()})
            self.current = None
            self.result_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        if self.link_depth is not None:
            self.current["title"] += data
        elif self.date_depth is not None:
            self.current["date"] += data
        elif self.snippet_depth is not None:
            self.current["snippet"] += data


class _MicronArticleParser(HTMLParser):
    BLOCK_TAGS = _TextParser.BLOCK_TAGS
    SKIP_TAGS = _TextParser.SKIP_TAGS

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.title_depth: int | None = None
        self.date_depth: int | None = None
        self.body_depth: int | None = None
        self.skip_depth = 0
        self.title_parts: list[str] = []
        self.date_parts: list[str] = []
        self.body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.depth += 1
        attributes = dict(attrs)
        classes = set(attributes.get("class", "").split())
        if "field--name-field-nir-news-title" in classes:
            self.title_depth = self.depth
        if "field--name-field-nir-news-date" in classes:
            self.date_depth = self.depth
        if "node__content" in classes:
            self.body_depth = self.depth
        if self.body_depth is not None:
            if tag in self.SKIP_TAGS:
                self.skip_depth += 1
            elif not self.skip_depth and tag in self.BLOCK_TAGS:
                self.body_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.body_depth is not None:
            if tag in self.SKIP_TAGS and self.skip_depth:
                self.skip_depth -= 1
            elif not self.skip_depth and tag in self.BLOCK_TAGS:
                self.body_parts.append("\n")
        if self.title_depth == self.depth:
            self.title_depth = None
        if self.date_depth == self.depth:
            self.date_depth = None
        if self.body_depth == self.depth:
            self.body_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.title_depth is not None:
            self.title_parts.append(data)
        if self.date_depth is not None:
            self.date_parts.append(data)
        if self.body_depth is not None and not self.skip_depth:
            self.body_parts.append(data)

    @property
    def title(self) -> str:
        return _normalize_text("".join(self.title_parts))

    @property
    def published_text(self) -> str:
        return _normalize_text("".join(self.date_parts))

    @property
    def body(self) -> str:
        return _normalize_text("".join(self.body_parts))


def _normalize_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(line for line in lines if line)


def _html_to_text(value: str) -> str:
    decoded = value
    for _ in range(3):
        updated = html.unescape(decoded)
        if updated == decoded:
            break
        decoded = updated
    decoded = re.sub(r"^\s*<!\[CDATA\[", "", decoded)
    decoded = re.sub(r"\]\]>\s*$", "", decoded)
    parser = _TextParser()
    parser.feed(decoded)
    parser.close()
    return parser.text()


def _parse_rss_date(value: str) -> datetime:
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_micron_date(value: str, timezone_name: str) -> datetime:
    cleaned = re.sub(r"\s+(?:EDT|EST|CDT|CST|MDT|MST|PDT|PST)$", "", value.strip())
    parsed = datetime.strptime(cleaned, "%B %d, %Y at %I:%M %p")
    return parsed.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)


class ControlledSourceCollector:
    version = "official-source-collector-v1"

    def __init__(
        self,
        source: FixedSourceConfig,
        *,
        timeout_seconds: float,
        max_bytes: int,
        user_agent: str,
        client: httpx.Client | None = None,
    ):
        self.source = source
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.client = client or httpx.Client()
        self._owns_client = client is None
        self.last_response: dict[str, str | int | None] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def collect(
        self,
        limit: int,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> CollectorResult:
        headers = {
            "Accept": (
                "application/rss+xml, application/xml, text/xml;q=0.9"
                if self.source.mode == "rss_full"
                else "text/html, application/xhtml+xml;q=0.9"
            )
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        listing = self._get(str(self.source.index_url), headers=headers)
        if listing.status_code == 304:
            return CollectorResult(
                status=FetchStatus.NOT_MODIFIED,
                request_url=str(listing.url),
                http_status=304,
                etag=listing.headers.get("etag") or etag,
                last_modified=listing.headers.get("last-modified") or last_modified,
            )
        if self.source.mode == "rss_full":
            candidates, item_results = self._collect_rss(listing, limit)
        else:
            candidates, item_results = self._collect_micron(listing, limit)
        return CollectorResult(
            status=FetchStatus.PASSED,
            request_url=str(listing.url),
            http_status=listing.status_code,
            etag=listing.headers.get("etag"),
            last_modified=listing.headers.get("last-modified"),
            response_sha256=hashlib.sha256(listing.content).hexdigest(),
            response_bytes=len(listing.content),
            candidates=candidates,
            item_results=item_results,
        )

    def _get(self, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        current = url
        request_headers = {"User-Agent": self.user_agent, **(headers or {})}
        for _ in range(4):
            self._validate_url(current)
            response = self.client.get(
                current,
                headers=request_headers,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise FixedSourceSecurityError("redirect response omitted Location")
                current = urljoin(current, location)
                continue
            if response.status_code == 304:
                self._remember_response(response)
                return response
            if response.status_code < 200 or response.status_code >= 300:
                raise FixedSourceError(f"official source returned HTTP {response.status_code}")
            if len(response.content) > self.max_bytes:
                raise FixedSourceError("official source response exceeded the configured byte limit")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in {
                "application/rss+xml", "application/xml", "text/xml", "text/html", "application/xhtml+xml",
            }:
                raise FixedSourceError(f"unsupported official source content type: {content_type or 'missing'}")
            self._remember_response(response)
            return response
        raise FixedSourceSecurityError("official source exceeded the redirect limit")

    def _remember_response(self, response: httpx.Response) -> None:
        self.last_response = {
            "request_url": str(response.url),
            "http_status": response.status_code,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "response_sha256": hashlib.sha256(response.content).hexdigest() if response.content else None,
            "response_bytes": len(response.content),
        }

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise FixedSourceSecurityError("fixed sources require credential-free HTTPS URLs")
        if parsed.port not in {None, 443}:
            raise FixedSourceSecurityError("fixed sources may use only the default HTTPS port")
        if host not in self.source.allowed_hosts:
            raise FixedSourceSecurityError("URL host is outside the fixed-source allowlist")
        if not any(parsed.path.startswith(prefix) for prefix in self.source.allowed_path_prefixes):
            raise FixedSourceSecurityError("URL path is outside the fixed-source allowlist")

    def _matches_terms(self, *values: str) -> bool:
        searchable = " ".join(values).casefold()
        return any(term.casefold() in searchable for term in self.source.discovery_terms)

    def _collect_rss(self, response: httpx.Response, limit: int) -> tuple[list[FetchCandidate], list[dict]]:
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise FixedSourceParseError("official RSS is not valid XML") from exc
        candidates: list[FetchCandidate] = []
        item_results: list[dict] = []
        content_tag = "{http://purl.org/rss/1.0/modules/content/}encoded"
        for item in root.findall("./channel/item")[: self.source.max_candidates]:
            title = _normalize_text(item.findtext("title") or "")
            link = _normalize_text(item.findtext("link") or "")
            categories = [_normalize_text(node.text or "") for node in item.findall(".//category")]
            required = {value.casefold() for value in self.source.required_categories}
            if required and not required.intersection(value.casefold() for value in categories):
                item_results.append({"title": title, "url": link, "status": "SKIPPED_CATEGORY"})
                continue
            try:
                self._validate_url(link)
            except FixedSourceSecurityError:
                item_results.append({"title": title, "url": link, "status": "SKIPPED_URL"})
                continue
            description = item.findtext("description") or ""
            body_html = item.findtext(content_tag) or item.findtext("content") or description
            raw_text = _html_to_text(body_html)
            if not self._matches_terms(title, description, raw_text):
                item_results.append({"title": title, "url": link, "status": "SKIPPED_TERM"})
                continue
            if len(raw_text) < 40:
                item_results.append({"title": title, "url": link, "status": "SKIPPED_EMPTY"})
                continue
            try:
                published_at = _parse_rss_date(item.findtext("pubDate") or "")
            except (TypeError, ValueError, OverflowError):
                item_results.append({"title": title, "url": link, "status": "SKIPPED_DATE"})
                continue
            body_bytes = body_html.encode("utf-8")
            candidates.append(
                FetchCandidate(
                    title=title,
                    url=link,
                    published_at=published_at,
                    language=self.source.language,
                    raw_text=raw_text,
                    response_sha256=hashlib.sha256(body_bytes).hexdigest(),
                    response_bytes=len(body_bytes),
                    http_status=response.status_code,
                )
            )
            item_results.append({"title": title, "url": link, "status": "COLLECTED_FROM_RSS"})
            if len(candidates) >= limit:
                break
        return candidates, item_results

    def _collect_micron(self, response: httpx.Response, limit: int) -> tuple[list[FetchCandidate], list[dict]]:
        parser = _MicronIndexParser()
        parser.feed(response.text)
        parser.close()
        if not parser.items:
            if "bm-verify" in response.text or "akamai" in response.text.casefold():
                raise FixedSourceParseError(
                    "Micron IR returned an anti-bot verification page; browser automation is prohibited"
                )
            raise FixedSourceParseError("Micron IR listing contract produced no article candidates")
        candidates: list[FetchCandidate] = []
        item_results: list[dict] = []
        for item in parser.items[: self.source.max_candidates]:
            article_url = urljoin(str(self.source.index_url), item["url"])
            if not self._matches_terms(item["title"], item["snippet"]):
                item_results.append({"title": item["title"], "url": article_url, "status": "SKIPPED_TERM"})
                continue
            article = self._get(article_url)
            article_parser = _MicronArticleParser()
            article_parser.feed(article.text)
            article_parser.close()
            if not article_parser.title or len(article_parser.body) < 40 or not article_parser.published_text:
                item_results.append({"title": item["title"], "url": article_url, "status": "SKIPPED_PARSE"})
                continue
            try:
                published_at = _parse_micron_date(article_parser.published_text, self.source.timezone)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise FixedSourceParseError("Micron article publication date contract changed") from exc
            candidates.append(
                FetchCandidate(
                    title=article_parser.title,
                    url=article_url,
                    published_at=published_at,
                    language=self.source.language,
                    raw_text=article_parser.body,
                    response_sha256=hashlib.sha256(article.content).hexdigest(),
                    response_bytes=len(article.content),
                    http_status=article.status_code,
                )
            )
            item_results.append(
                {
                    "title": article_parser.title,
                    "url": article_url,
                    "status": "COLLECTED_FROM_ARTICLE",
                    "http_status": article.status_code,
                    "response_sha256": hashlib.sha256(article.content).hexdigest(),
                    "response_bytes": len(article.content),
                }
            )
            if len(candidates) >= limit:
                break
        return candidates, item_results
