import json
import hashlib
import time
from datetime import datetime, timezone
from typing import Protocol

import httpx
from pydantic import SecretStr

from app.web_discovery.contracts import ProviderSearchHit, ProviderSearchPage, SearchQuery


BRAVE_WEB_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _redact_secret(value, secret: str):
    if isinstance(value, dict):
        return {key: _redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


class WebSearchAdapter(Protocol):
    name: str
    version: str

    def search(self, query: SearchQuery, *, count: int) -> ProviderSearchPage: ...


class WebSearchError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        request_count: int = 0,
        retry_count: int = 0,
    ):
        self.code = code
        self.retryable = retryable
        self.request_count = request_count
        self.retry_count = retry_count
        super().__init__(f"Web Search provider request failed ({code})")


class BraveWebSearchAdapter:
    name = "brave"
    version = "brave-web-search-v1"

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        base_url: str = BRAVE_WEB_ENDPOINT,
        timeout_seconds: float = 20,
        max_retries: int = 2,
        max_response_bytes: int = 2_000_000,
        client: httpx.Client | None = None,
        sleep_fn=time.sleep,
    ):
        if api_key is None:
            raise WebSearchError("MISSING_API_KEY")
        if base_url.rstrip("/") != BRAVE_WEB_ENDPOINT:
            raise WebSearchError("INVALID_PROVIDER_ENDPOINT")
        self._api_key = api_key
        self.endpoint = BRAVE_WEB_ENDPOINT
        self.max_retries = min(max_retries, 2)
        self.max_response_bytes = max_response_bytes
        self.sleep_fn = sleep_fn
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False, trust_env=False)

    def search(self, query: SearchQuery, *, count: int) -> ProviderSearchPage:
        retries = 0
        requests = 0
        while True:
            requests += 1
            try:
                with self.client.stream(
                    "GET", self.endpoint,
                    headers={"Accept": "application/json", "X-Subscription-Token": self._api_key.get_secret_value()},
                    params={"q": query.text, "count": count, "safesearch": "moderate", "text_decorations": "false"},
                ) as response:
                    status_code = response.status_code
                    response_bytes = bytearray()
                    if status_code == 200:
                        declared = response.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > self.max_response_bytes:
                            raise WebSearchError(
                                "RESPONSE_TOO_LARGE", request_count=requests, retry_count=retries
                            )
                        for chunk in response.iter_bytes():
                            if len(response_bytes) + len(chunk) > self.max_response_bytes:
                                raise WebSearchError(
                                    "RESPONSE_TOO_LARGE", request_count=requests, retry_count=retries
                                )
                            response_bytes.extend(chunk)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if retries >= self.max_retries:
                    raise WebSearchError(
                        "NETWORK_ERROR", retryable=True,
                        request_count=requests, retry_count=retries,
                    ) from exc
                retries += 1
                self.sleep_fn(0)
                continue
            if status_code in {401, 403}:
                raise WebSearchError(
                    f"HTTP_{status_code}", request_count=requests, retry_count=retries
                )
            if status_code == 429:
                raise WebSearchError(
                    "HTTP_429", retryable=True,
                    request_count=requests, retry_count=retries,
                )
            if 500 <= status_code <= 599:
                if retries >= self.max_retries:
                    raise WebSearchError(
                        "UPSTREAM_5XX", retryable=True,
                        request_count=requests, retry_count=retries,
                    )
                retries += 1
                self.sleep_fn(0)
                continue
            if status_code != 200:
                raise WebSearchError(
                    f"HTTP_{status_code}", request_count=requests, retry_count=retries
                )
            try:
                payload = json.loads(response_bytes)
            except (ValueError, UnicodeDecodeError) as exc:
                raise WebSearchError(
                    "INVALID_JSON", request_count=requests, retry_count=retries
                ) from exc
            payload = _redact_secret(payload, self._api_key.get_secret_value())
            raw_results = (payload.get("web") or {}).get("results") or []
            if not isinstance(raw_results, list):
                raise WebSearchError(
                    "INVALID_SCHEMA", request_count=requests, retry_count=retries
                )
            discovered_at = datetime.now(timezone.utc)
            hits = [
                ProviderSearchHit(
                    title=str(item.get("title") or "Untitled")[:500],
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("description") or ""),
                    rank=index,
                    discovered_at=discovered_at,
                    source_metadata={"snippet_policy": "DISCOVERY_ONLY"},
                )
                for index, item in enumerate(raw_results[:count], start=1)
                if item.get("url")
            ]
            safe_raw = {
                "web": {
                    "results": [
                        {"title": hit.title, "url": hit.url, "description": hit.snippet}
                        for hit in hits
                    ]
                }
            }
            return ProviderSearchPage(
                provider=self.name,
                adapter_version=self.version,
                query_id=query.query_id,
                endpoint=self.endpoint,
                request_count=requests,
                retry_count=retries,
                provider_request_id=(
                    response.headers.get("x-request-id") or response.headers.get("request-id")
                ),
                query_text_hash=hashlib.sha256(query.text.encode()).hexdigest(),
                raw_response_sha256=hashlib.sha256(response_bytes).hexdigest(),
                results=hits,
                raw_response=safe_raw,
            )

    def close(self):
        if self._owns_client:
            self.client.close()
