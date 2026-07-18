import hashlib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from app.source_collections.contracts import ApprovedSource, DiscoveredItem, ExtractionStatus, FetchResult
from app.source_collections.security import CollectionHTTPError, SafeHttpClient


class _ArticleParser(HTMLParser):
    SKIP = {"script", "style", "svg", "iframe", "nav", "footer", "form", "noscript"}
    BLOCK = {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "br", "table", "tr"}

    def __init__(self, selectors: list[str]):
        super().__init__(convert_charrefs=True)
        self.selectors = selectors
        self.depth = 0
        self.capture_depth: int | None = None
        self.skip_depth = 0
        self.parts: list[str] = []
        self.all_parts: list[str] = []
        self.title = ""
        self.published_at: datetime | None = None
        self.language = ""

    def _matches(self, tag, attrs):
        classes = set(attrs.get("class", "").split())
        identifier = attrs.get("id", "")
        for selector in self.selectors:
            if selector == tag or (selector.startswith(".") and selector[1:] in classes) or (selector.startswith("#") and selector[1:] == identifier):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        self.depth += 1
        attributes = dict(attrs)
        if tag == "html":
            self.language = attributes.get("lang", "")
        if tag == "meta":
            key = attributes.get("property") or attributes.get("name")
            value = attributes.get("content", "")
            if key in {"og:title", "twitter:title"} and not self.title:
                self.title = value
            if key in {"article:published_time", "date", "datePublished"} and value:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    self.published_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
        if self.capture_depth is None and self._matches(tag, attributes):
            self.capture_depth = self.depth
        if tag in self.SKIP:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self.BLOCK:
            self.all_parts.append("\n")
            if self.capture_depth is not None:
                self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK:
            self.all_parts.append("\n")
            if self.capture_depth is not None:
                self.parts.append("\n")
        if self.capture_depth == self.depth:
            self.capture_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.all_parts.append(data)
        if self.capture_depth is not None:
            self.parts.append(data)

    def text(self):
        selected = self.parts if self.parts else self.all_parts
        lines = [re.sub(r"\s+", " ", item).strip() for item in "".join(selected).splitlines()]
        return "\n".join(item for item in lines if item)


class ArticleFetchAdapter:
    def fetch(self, item: DiscoveredItem, source: ApprovedSource, client: SafeHttpClient) -> FetchResult:
        raise NotImplementedError


class HTTPArticleFetcher(ArticleFetchAdapter):
    def __init__(self, *, max_bytes: int = 2_000_000, minimum_chars: int = 120):
        self.max_bytes = max_bytes
        self.minimum_chars = minimum_chars

    def fetch(self, item: DiscoveredItem, source: ApprovedSource, client: SafeHttpClient) -> FetchResult:
        now = datetime.now(timezone.utc)
        try:
            response = client.get(item.canonical_url, article=True, accept="text/html, application/xhtml+xml")
        except CollectionHTTPError as exc:
            status = ExtractionStatus.DISALLOWED_REDIRECT if exc.code == "DISALLOWED_REDIRECT" else ExtractionStatus.BLOCKED if exc.code in {"HTTP_401", "HTTP_403"} else ExtractionStatus.HTTP_ERROR
            return FetchResult(
                source_id=source.source_id, canonical_url=item.canonical_url, final_url=item.canonical_url,
                http_status=exc.status_code, fetched_at=now, title=item.title, published_at=item.published_at,
                language=source.language, extraction_status=status,
                response_metadata={"error_type": exc.code},
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        metadata = {
            "content_type": content_type,
            "response_bytes": len(response.content),
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
            "request_count": client.request_count,
            "retry_count": client.retry_count,
        }
        if len(response.content) > self.max_bytes or content_type not in {"text/html", "application/xhtml+xml"}:
            return FetchResult(
                source_id=source.source_id, canonical_url=item.canonical_url, final_url=str(response.url),
                http_status=response.status_code, fetched_at=now, title=item.title, published_at=item.published_at,
                language=source.language, extraction_status=ExtractionStatus.PARSE_ERROR, response_metadata=metadata,
            )
        parser = _ArticleParser(source.body_selectors)
        try:
            parser.feed(response.text)
            parser.close()
        except Exception:
            return FetchResult(
                source_id=source.source_id, canonical_url=item.canonical_url, final_url=str(response.url),
                http_status=response.status_code, fetched_at=now, title=item.title, published_at=item.published_at,
                language=source.language, extraction_status=ExtractionStatus.PARSE_ERROR, response_metadata=metadata,
            )
        raw_text = parser.text()
        if not raw_text:
            status = ExtractionStatus.METADATA_ONLY
        elif len(raw_text) < self.minimum_chars:
            status = ExtractionStatus.TOO_SHORT
        else:
            status = ExtractionStatus.SUCCESS
        return FetchResult(
            source_id=source.source_id,
            canonical_url=item.canonical_url,
            final_url=str(response.url),
            http_status=response.status_code,
            fetched_at=now,
            title=parser.title or item.title,
            published_at=parser.published_at or item.published_at,
            language=parser.language or source.language,
            raw_text=raw_text,
            content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest() if raw_text else None,
            extraction_status=status,
            response_metadata=metadata,
        )
