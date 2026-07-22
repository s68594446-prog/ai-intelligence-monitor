import hashlib
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.web_discovery.contracts import FetchStatus, NormalizedSearchResult, WebFetchResult
from app.web_discovery.security import Resolver, WebSecurityError, canonicalize_url, system_resolver, validate_public_url


class _TextExtractor(HTMLParser):
    _blocked = {"script", "style", "noscript", "svg", "canvas", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.title_depth = 0
        self.text: list[str] = []
        self.title: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag in self._blocked:
            self.depth += 1
        if tag == "title":
            self.title_depth += 1
        if tag == "meta":
            data = {key.casefold(): value for key, value in attrs if value}
            key = (data.get("property") or data.get("name") or "").casefold()
            if key and data.get("content"):
                self.meta[key] = data["content"]

    def handle_endtag(self, tag):
        if tag in self._blocked and self.depth:
            self.depth -= 1
        if tag == "title" and self.title_depth:
            self.title_depth -= 1

    def handle_data(self, data):
        if self.depth:
            return
        if self.title_depth:
            self.title.append(data)
        self.text.append(data)


class WebPageFetcher:
    version = "safe-web-fetch-v1"
    _allowed_types = {"text/html", "application/xhtml+xml", "text/plain"}
    _block_markers = (
        "captcha",
        "verify you are human",
        "cloudflare ray id",
        "access denied",
        "sign in to continue",
        "subscribe to continue",
        "paywall",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        resolver: Resolver = system_resolver,
        max_bytes: int = 2_000_000,
        max_redirects: int = 5,
        max_retries: int = 2,
        minimum_interval_seconds: float = 1,
        sleep_fn=time.sleep,
    ):
        self.client = client
        self.resolver = resolver
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_retries = min(max_retries, 2)
        self.minimum_interval_seconds = minimum_interval_seconds
        self.sleep_fn = sleep_fn
        self._last_request: dict[str, float] = {}

    def _blocked(self, result, status, code, **kwargs):
        return WebFetchResult(
            candidate_id=result.candidate_id,
            canonical_url=result.canonical_url,
            final_url=kwargs.pop("final_url", result.canonical_url),
            status=status,
            title=result.title,
            error_code=code,
            **kwargs,
        )

    def _throttle(self, host: str):
        now = time.monotonic()
        previous = self._last_request.get(host)
        if previous is not None:
            remaining = self.minimum_interval_seconds - (now - previous)
            if remaining > 0:
                self.sleep_fn(remaining)
        self._last_request[host] = time.monotonic()

    def fetch(self, result: NormalizedSearchResult, *, allowed_urls: set[str]) -> WebFetchResult:
        allowed = {canonicalize_url(url) for url in allowed_urls}
        if canonicalize_url(result.canonical_url) not in allowed:
            return self._blocked(result, FetchStatus.BLOCKED, "URL_NOT_FROM_SEARCH")
        url = result.canonical_url
        redirects = requests = retries = 0
        seen: set[str] = set()
        initial_host = urlsplit(url).hostname or ""
        while True:
            try:
                url, before_addresses = validate_public_url(url, self.resolver)
            except WebSecurityError as exc:
                return self._blocked(result, FetchStatus.BLOCKED, exc.code, final_url=url, request_count=requests, retry_count=retries, redirect_count=redirects)
            if url in seen:
                return self._blocked(result, FetchStatus.BLOCKED, "REDIRECT_LOOP", final_url=url, request_count=requests, retry_count=retries, redirect_count=redirects)
            seen.add(url)
            host = urlsplit(url).hostname or ""
            self._throttle(host)
            requests += 1
            try:
                parts = urlsplit(url)
                pinned_address = sorted(before_addresses)[0]
                pinned_netloc = f"[{pinned_address}]" if ":" in pinned_address else pinned_address
                pinned_url = urlunsplit((parts.scheme, pinned_netloc, parts.path, parts.query, ""))
                self.client.cookies.clear()
                request = self.client.build_request(
                    "GET", pinned_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,text/plain",
                        "Accept-Encoding": "identity",
                        "Host": host,
                    },
                    extensions={"sni_hostname": host},
                )
                response = self.client.send(request, stream=True, follow_redirects=False)
                self.client.cookies.clear()
            except (httpx.TimeoutException, httpx.NetworkError):
                if retries < self.max_retries:
                    retries += 1
                    seen.discard(url)
                    continue
                return self._blocked(result, FetchStatus.HTTP_ERROR, "NETWORK_ERROR", final_url=url, request_count=requests, retry_count=retries, redirect_count=redirects)
            try:
                _, after_addresses = validate_public_url(url, self.resolver)
            except WebSecurityError as exc:
                response.close()
                return self._blocked(result, FetchStatus.BLOCKED, exc.code, final_url=url, request_count=requests, retry_count=retries, redirect_count=redirects)
            if set(before_addresses) != set(after_addresses):
                response.close()
                return self._blocked(result, FetchStatus.BLOCKED, "DNS_REBINDING", final_url=url, request_count=requests, retry_count=retries, redirect_count=redirects)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                response.close()
                if not location:
                    return self._blocked(result, FetchStatus.BLOCKED, "REDIRECT_WITHOUT_LOCATION", final_url=url, request_count=requests, retry_count=retries, redirect_count=redirects)
                redirects += 1
                if redirects > self.max_redirects:
                    return self._blocked(result, FetchStatus.BLOCKED, "TOO_MANY_REDIRECTS", final_url=url, request_count=requests, retry_count=retries, redirect_count=redirects)
                url = urljoin(url, location)
                continue
            if response.status_code in {401, 403, 429}:
                response.close()
                return self._blocked(result, FetchStatus.BLOCKED, f"HTTP_{response.status_code}", final_url=url, http_status=response.status_code, request_count=requests, retry_count=retries, redirect_count=redirects)
            if 500 <= response.status_code <= 599 and retries < self.max_retries:
                response.close()
                retries += 1
                seen.discard(url)
                continue
            if response.status_code != 200:
                response.close()
                return self._blocked(result, FetchStatus.HTTP_ERROR, f"HTTP_{response.status_code}", final_url=url, http_status=response.status_code, request_count=requests, retry_count=retries, redirect_count=redirects)
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            if content_type not in self._allowed_types:
                response.close()
                return self._blocked(result, FetchStatus.INVALID_CONTENT, "CONTENT_TYPE_NOT_ALLOWED", final_url=url, http_status=200, content_type=content_type, request_count=requests, retry_count=retries, redirect_count=redirects)
            content_encoding = response.headers.get("content-encoding", "").strip().casefold()
            if content_encoding not in {"", "identity"}:
                response.close()
                return self._blocked(result, FetchStatus.TOO_LARGE, "COMPRESSED_RESPONSE_FORBIDDEN", final_url=url, http_status=200, content_type=content_type, request_count=requests, retry_count=retries, redirect_count=redirects)
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                response.close()
                return self._blocked(result, FetchStatus.TOO_LARGE, "RESPONSE_TOO_LARGE", final_url=url, http_status=200, content_type=content_type, response_bytes=0, request_count=requests, retry_count=retries, redirect_count=redirects)
            content_buffer = bytearray()
            try:
                chunks = [response.content] if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    if len(content_buffer) + len(chunk) > self.max_bytes:
                        response.close()
                        return self._blocked(result, FetchStatus.TOO_LARGE, "RESPONSE_TOO_LARGE", final_url=url, http_status=200, content_type=content_type, response_bytes=len(content_buffer), request_count=requests, retry_count=retries, redirect_count=redirects)
                    content_buffer.extend(chunk)
            finally:
                response.close()
            content = bytes(content_buffer)
            try:
                charset_match = re.search(r"charset=([^;\s]+)", response.headers.get("content-type", ""), re.IGNORECASE)
                decoded = content.decode(charset_match.group(1).strip('"\'') if charset_match else "utf-8", errors="strict")
            except (LookupError, UnicodeError):
                return self._blocked(result, FetchStatus.INVALID_CONTENT, "DECODE_ERROR", final_url=url, http_status=200, content_type=content_type, response_bytes=len(content), request_count=requests, retry_count=retries, redirect_count=redirects)
            lowered = decoded.casefold()[:100_000]
            if any(marker in lowered for marker in self._block_markers):
                return self._blocked(result, FetchStatus.BLOCKED, "ACCESS_CHALLENGE", final_url=url, http_status=200, content_type=content_type, response_bytes=len(content), request_count=requests, retry_count=retries, redirect_count=redirects)
            title = result.title
            published_at = None
            if content_type == "text/plain":
                raw_text = decoded
            else:
                parser = _TextExtractor()
                try:
                    parser.feed(decoded)
                except Exception:
                    return self._blocked(result, FetchStatus.EXTRACTION_FAILED, "HTML_PARSE_ERROR", final_url=url, http_status=200, content_type=content_type, response_bytes=len(content), request_count=requests, retry_count=retries, redirect_count=redirects)
                raw_text = "\n".join(line for chunk in parser.text if (line := " ".join(chunk.split())))
                title = " ".join(parser.title).strip() or parser.meta.get("og:title") or result.title
                date_value = parser.meta.get("article:published_time") or parser.meta.get("date")
                if date_value:
                    try:
                        published_at = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
                    except ValueError:
                        try:
                            published_at = parsedate_to_datetime(date_value)
                        except (TypeError, ValueError):
                            published_at = None
            raw_text = re.sub(r"[ \t]+", " ", raw_text).strip()
            if len(raw_text) < 120:
                return self._blocked(result, FetchStatus.EXTRACTION_FAILED, "TEXT_TOO_SHORT", final_url=url, http_status=200, content_type=content_type, response_bytes=len(content), request_count=requests, retry_count=retries, redirect_count=redirects)
            body_hash = hashlib.sha256(content).hexdigest()
            content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            return WebFetchResult(
                candidate_id=result.candidate_id,
                canonical_url=result.canonical_url,
                final_url=url,
                status=FetchStatus.SUCCESS,
                http_status=200,
                content_type=content_type,
                response_bytes=len(content),
                response_sha256=body_hash,
                title=title[:500],
                language=response.headers.get("content-language", "")[:20],
                published_at=published_at,
                raw_text=raw_text,
                content_hash=content_hash,
                request_count=requests,
                retry_count=retries,
                redirect_count=redirects,
                security_checks={
                    "https": True,
                    "public_dns_before": True,
                    "public_dns_after": True,
                    "no_dns_rebinding": True,
                    "dns_pinned_connection": True,
                    "approved_origin": canonicalize_url(result.canonical_url) in allowed,
                    "redirects_validated": True,
                    "initial_host_recorded": bool(initial_host),
                },
            )
