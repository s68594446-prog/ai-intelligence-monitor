import ipaddress
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from app.source_collections.contracts import ApprovedSource


class CollectionError(RuntimeError):
    code = "COLLECTION_ERROR"


class CollectionSecurityError(CollectionError):
    code = "CONFIGURATION_ERROR"


class CollectionHTTPError(CollectionError):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"source", "campaign"}
        )
    )
    return urlunsplit((parsed.scheme.lower(), host + port, path, query, ""))


def validate_url(url: str, source: ApprovedSource, *, article: bool = False) -> str:
    canonical = canonicalize_url(url)
    parsed = urlsplit(canonical)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise CollectionSecurityError("only credential-free HTTPS URLs are allowed")
    if parsed.port not in {None, 443}:
        raise CollectionSecurityError("only the default HTTPS port is allowed")
    if host == "localhost" or host.endswith(".localhost"):
        raise CollectionSecurityError("localhost is prohibited")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise CollectionSecurityError("private or non-public IP addresses are prohibited")
    allowed = {item.lower().rstrip(".") for item in source.allowed_domains}
    if host not in allowed:
        raise CollectionSecurityError(f"domain is outside the approved allowlist: {host}")
    if article and not any(re.search(pattern, canonical) for pattern in source.article_url_patterns):
        raise CollectionSecurityError("article URL does not match the approved patterns")
    return canonical


class SafeHttpClient:
    def __init__(
        self,
        source: ApprovedSource,
        *,
        user_agent: str,
        client: httpx.Client | None = None,
        sleep_fn=time.sleep,
        max_retries: int = 2,
    ):
        self.source = source
        self.user_agent = user_agent
        self.client = client or httpx.Client()
        self._owns_client = client is None
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries
        self.request_count = 0
        self.retry_count = 0
        self._last_request_at: float | None = None

    def close(self):
        if self._owns_client:
            self.client.close()

    def get(self, url: str, *, article: bool = False, accept: str = "text/html") -> httpx.Response:
        current = validate_url(url, self.source, article=article)
        redirects = 0
        retries = 0
        while True:
            if self._last_request_at is not None:
                elapsed = time.monotonic() - self._last_request_at
                delay = self.source.minimum_interval_seconds - elapsed
                if delay > 0:
                    self.sleep_fn(delay)
            try:
                self.request_count += 1
                response = self.client.get(
                    current,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": accept,
                        # Some approved newsrooms advertise a compressed body
                        # that stalls before EOF. Identity encoding keeps the
                        # response auditable and does not alter access control.
                        "Accept-Encoding": "identity",
                    },
                    timeout=self.source.request_timeout_seconds,
                    follow_redirects=False,
                )
                self._last_request_at = time.monotonic()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if retries < self.max_retries:
                    retries += 1
                    self.retry_count += 1
                    continue
                raise CollectionHTTPError("NETWORK_TIMEOUT", "official source request timed out") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                redirects += 1
                if redirects > 5:
                    raise CollectionHTTPError("DISALLOWED_REDIRECT", "redirect limit exceeded")
                location = response.headers.get("location")
                if not location:
                    raise CollectionHTTPError("DISALLOWED_REDIRECT", "redirect omitted Location")
                try:
                    current = validate_url(urljoin(current, location), self.source, article=article)
                except CollectionSecurityError as exc:
                    raise CollectionHTTPError("DISALLOWED_REDIRECT", str(exc)) from exc
                continue
            if response.status_code in {401, 403, 404}:
                raise CollectionHTTPError(f"HTTP_{response.status_code}", f"official source returned HTTP {response.status_code}", response.status_code)
            if response.status_code == 429:
                raise CollectionHTTPError("RATE_LIMITED", "official source rate limited the request", 429)
            if 500 <= response.status_code < 600:
                if retries < self.max_retries:
                    retries += 1
                    self.retry_count += 1
                    continue
                raise CollectionHTTPError("HTTP_5XX", f"official source returned HTTP {response.status_code}", response.status_code)
            if response.status_code < 200 or response.status_code >= 300:
                raise CollectionHTTPError("HTTP_ERROR", f"official source returned HTTP {response.status_code}", response.status_code)
            return response
