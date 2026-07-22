import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import SecretStr

from app.web_discovery.contracts import ProviderSearchHit, ProviderSearchPage, SearchQuery
from app.web_discovery.search import WebSearchError, _redact_secret


TAVILY_ENDPOINT = "https://api.tavily.com/search"
EXA_ENDPOINT = "https://api.exa.ai/search"
YOU_ENDPOINT = "https://ydc-index.io/v1/search"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
SEARXNG_ENDPOINT = "http://127.0.0.1:8080/search"
THENEWSAPI_ENDPOINT = "https://api.thenewsapi.com/v1/news/all"


def _date(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _JSONSearchAdapter:
    max_response_bytes = 2_000_000

    def __init__(self, *, client=None, timeout_seconds=20, max_retries=2, sleep_fn=time.sleep):
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self.max_retries = min(max_retries, 2)
        self.sleep_fn = sleep_fn

    def _request(self, method: str, endpoint: str, *, headers=None, params=None, payload=None, secret=""):
        requests = retries = 0
        while True:
            requests += 1
            try:
                with self.client.stream(method, endpoint, headers=headers, params=params, json=payload) as response:
                    status = response.status_code
                    request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                    if status in {301, 302, 303, 307, 308}:
                        raise WebSearchError(
                            "PROVIDER_REDIRECT", request_count=requests, retry_count=retries
                        )
                    if status in {401, 403}:
                        raise WebSearchError(
                            f"HTTP_{status}", request_count=requests, retry_count=retries
                        )
                    if status in {429, 432, 433}:
                        raise WebSearchError(
                            f"HTTP_{status}", retryable=True,
                            request_count=requests, retry_count=retries,
                        )
                    if 500 <= status <= 599:
                        if retries < self.max_retries:
                            retries += 1
                            self.sleep_fn(0)
                            continue
                        raise WebSearchError(
                            "UPSTREAM_5XX", retryable=True,
                            request_count=requests, retry_count=retries,
                        )
                    if status != 200:
                        raise WebSearchError(
                            f"HTTP_{status}", request_count=requests, retry_count=retries
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_response_bytes:
                        raise WebSearchError(
                            "RESPONSE_TOO_LARGE", request_count=requests, retry_count=retries
                        )
                    body = bytearray()
                    chunks = [response.content] if response.is_stream_consumed else response.iter_bytes()
                    for chunk in chunks:
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise WebSearchError(
                                "RESPONSE_TOO_LARGE", request_count=requests, retry_count=retries
                            )
                        body.extend(chunk)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if retries < self.max_retries:
                    retries += 1
                    self.sleep_fn(0)
                    continue
                raise WebSearchError(
                    "NETWORK_ERROR", retryable=True,
                    request_count=requests, retry_count=retries,
                ) from exc
            try:
                data = json.loads(body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise WebSearchError(
                    "INVALID_JSON", request_count=requests, retry_count=retries
                ) from exc
            return _redact_secret(data, secret), hashlib.sha256(body).hexdigest(), request_id, requests, retries

    def close(self):
        if self._owns_client:
            self.client.close()

    def _page(self, query, endpoint, hits, raw, sha, request_id, requests, retries):
        return ProviderSearchPage(
            provider=self.name, adapter_version=self.version, provider_request_id=request_id,
            query_id=query.query_id, query_text_hash=hashlib.sha256(query.text.encode()).hexdigest(),
            endpoint=endpoint, request_count=requests, retry_count=retries, results=hits,
            raw_response=raw, raw_response_sha256=sha,
        )


class TavilyWebSearchAdapter(_JSONSearchAdapter):
    name = "tavily"
    version = "tavily-search-v1"

    def __init__(self, *, api_key: SecretStr | None, **kwargs):
        if api_key is None:
            raise WebSearchError("MISSING_API_KEY")
        self.api_key = api_key
        super().__init__(**kwargs)

    def search(self, query: SearchQuery, *, count: int):
        secret = self.api_key.get_secret_value()
        payload = {
            "query": query.text, "search_depth": "basic", "max_results": count,
            "topic": "general", "auto_parameters": False, "include_answer": False,
            "include_raw_content": False, "include_images": False,
        }
        data, sha, request_id, requests, retries = self._request(
            "POST", TAVILY_ENDPOINT, headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            payload=payload, secret=secret,
        )
        raw_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise WebSearchError("INVALID_SCHEMA")
        now = datetime.now(timezone.utc)
        hits = [ProviderSearchHit(
            title=str(item.get("title") or "Untitled")[:500], url=str(item.get("url") or ""),
            snippet=str(item.get("content") or "")[:2000], rank=index, discovered_at=now,
            provider_score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
            source_metadata={"snippet_policy": "DISCOVERY_ONLY"},
        ) for index, item in enumerate(raw_results[:count], 1) if item.get("url")]
        safe_raw = {"results": [{"title": hit.title, "url": hit.url, "content": hit.snippet, "score": hit.provider_score} for hit in hits], "request_id": data.get("request_id")}
        return self._page(query, TAVILY_ENDPOINT, hits, safe_raw, sha, data.get("request_id") or request_id, requests, retries)


class ExaWebSearchAdapter(_JSONSearchAdapter):
    name = "exa"
    version = "exa-search-v1"

    def __init__(self, *, api_key: SecretStr | None, **kwargs):
        if api_key is None:
            raise WebSearchError("MISSING_API_KEY")
        self.api_key = api_key
        super().__init__(**kwargs)

    def search(self, query: SearchQuery, *, count: int):
        secret = self.api_key.get_secret_value()
        data, sha, request_id, requests, retries = self._request(
            "POST", EXA_ENDPOINT, headers={"x-api-key": secret, "Content-Type": "application/json"},
            payload={"query": query.text, "numResults": count, "moderation": True}, secret=secret,
        )
        raw_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise WebSearchError("INVALID_SCHEMA")
        now = datetime.now(timezone.utc)
        hits = [ProviderSearchHit(
            title=str(item.get("title") or "Untitled")[:500], url=str(item.get("url") or ""), snippet="",
            rank=index, published_at=_date(item.get("publishedDate")), discovered_at=now,
            provider_score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
            source_metadata={"author": item.get("author"), "snippet_policy": "DISCOVERY_ONLY", "contents_requested": False},
        ) for index, item in enumerate(raw_results[:count], 1) if item.get("url")]
        safe_raw = {
            "results": [
                {
                    "title": hit.title,
                    "url": hit.url,
                    "publishedDate": hit.published_at.isoformat() if hit.published_at else None,
                    "score": hit.provider_score,
                }
                for hit in hits
            ],
            "requestId": data.get("requestId"),
        }
        return self._page(query, EXA_ENDPOINT, hits, safe_raw, sha, data.get("requestId") or request_id, requests, retries)


class YouWebSearchAdapter(_JSONSearchAdapter):
    name = "you"
    version = "you-simple-search-v1"

    def __init__(self, *, api_key: SecretStr | None, **kwargs):
        if api_key is None:
            raise WebSearchError("MISSING_API_KEY")
        self.api_key = api_key
        super().__init__(**kwargs)

    def search(self, query: SearchQuery, *, count: int):
        secret = self.api_key.get_secret_value()
        data, sha, request_id, requests, retries = self._request(
            "GET", YOU_ENDPOINT, headers={"X-API-Key": secret}, params={"query": query.text, "count": count}, secret=secret,
        )
        raw_results = ((data.get("results") or {}).get("web")) if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise WebSearchError("INVALID_SCHEMA")
        now = datetime.now(timezone.utc)
        hits = [ProviderSearchHit(
            title=str(item.get("title") or "Untitled")[:500], url=str(item.get("url") or ""),
            snippet=str(item.get("description") or "")[:2000], rank=index, discovered_at=now,
            source_metadata={"snippet_policy": "DISCOVERY_ONLY"},
        ) for index, item in enumerate(raw_results[:count], 1) if item.get("url")]
        safe_raw = {"results": {"web": [{"title": hit.title, "url": hit.url, "description": hit.snippet} for hit in hits]}}
        return self._page(query, YOU_ENDPOINT, hits, safe_raw, sha, request_id, requests, retries)


def _unwrap_google_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.hostname and parsed.hostname.endswith("google.com"):
        for key in ("url", "q", "target"):
            candidate = (parse_qs(parsed.query).get(key) or [None])[0]
            if candidate and candidate.startswith("https://"):
                return candidate
    return value


class GeminiGoogleSearchAdapter(_JSONSearchAdapter):
    name = "gemini_google"
    version = "gemini-google-search-v1"

    def __init__(self, *, api_key: SecretStr | None, model: str, **kwargs):
        if api_key is None:
            raise WebSearchError("MISSING_API_KEY")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,80}", model):
            raise WebSearchError("INVALID_MODEL")
        self.api_key = api_key
        self.model = model
        super().__init__(**kwargs)

    @property
    def endpoint(self):
        return f"{GEMINI_BASE}/{self.model}:generateContent"

    def search(self, query: SearchQuery, *, count: int):
        secret = self.api_key.get_secret_value()
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "Find public web sources for this exact query. Return citations only: " + query.text}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 256},
        }
        data, sha, request_id, requests, retries = self._request(
            "POST", self.endpoint, headers={"x-goog-api-key": secret, "Content-Type": "application/json"}, payload=payload, secret=secret,
        )
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise WebSearchError("INVALID_SCHEMA")
        metadata = candidates[0].get("groundingMetadata") or {}
        chunks = metadata.get("groundingChunks") or []
        now = datetime.now(timezone.utc)
        hits = []
        for chunk in chunks:
            web = chunk.get("web") if isinstance(chunk, dict) else None
            if not isinstance(web, dict) or not web.get("uri"):
                continue
            hits.append(ProviderSearchHit(
                title=str(web.get("title") or "Untitled")[:500], url=_unwrap_google_url(str(web["uri"])), snippet="",
                rank=len(hits) + 1, discovered_at=now,
                source_metadata={"snippet_policy": "DISCOVERY_ONLY", "generated_answer_discarded": True, "search_queries": metadata.get("webSearchQueries") or []},
            ))
            if len(hits) >= count:
                break
        safe_raw = {"groundingMetadata": {"webSearchQueries": metadata.get("webSearchQueries") or [], "groundingChunks": [{"web": {"uri": hit.url, "title": hit.title}} for hit in hits]}}
        return self._page(query, self.endpoint, hits, safe_raw, sha, request_id, requests, retries)


class SearXNGWebSearchAdapter(_JSONSearchAdapter):
    name = "searxng"

    def __init__(self, *, image_digest: str = "UNVERIFIED", **kwargs):
        self.image_digest = image_digest
        # Fits the audited database column even with a full sha256 digest.
        self.version = f"searxng@{image_digest}"
        super().__init__(**kwargs)

    def search(self, query: SearchQuery, *, count: int):
        data, sha, request_id, requests, retries = self._request(
            "GET", SEARXNG_ENDPOINT,
            params={"q": query.text, "format": "json", "language": query.language, "safesearch": 1, "pageno": 1},
        )
        raw_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw_results, list):
            raise WebSearchError("INVALID_SCHEMA")
        now = datetime.now(timezone.utc)
        hits = [ProviderSearchHit(
            title=str(item.get("title") or "Untitled")[:500], url=str(item.get("url") or ""),
            snippet=str(item.get("content") or "")[:2000], rank=index, published_at=_date(item.get("publishedDate")), discovered_at=now,
            provider_score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
            source_metadata={
                "engines": item.get("engines") or [],
                "snippet_policy": "DISCOVERY_ONLY",
                "runtime_image_digest": self.image_digest,
            },
        ) for index, item in enumerate(raw_results[:count], 1) if item.get("url")]
        safe_raw = {"results": [{"title": hit.title, "url": hit.url, "content": hit.snippet, "score": hit.provider_score, "engines": hit.source_metadata["engines"]} for hit in hits]}
        return self._page(query, SEARXNG_ENDPOINT, hits, safe_raw, sha, request_id, requests, retries)


class TheNewsAPIAdapter(_JSONSearchAdapter):
    """Separately budgeted news discovery; never participates in Web fallback."""

    name = "thenewsapi"
    version = "thenewsapi-all-news-v1"

    def __init__(self, *, api_key: SecretStr | None, clock=None, **kwargs):
        if api_key is None:
            raise WebSearchError("MISSING_API_KEY")
        self.api_key = api_key
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        super().__init__(**kwargs)

    def search(self, query: SearchQuery, *, count: int):
        if count < 1 or count > 3:
            raise WebSearchError("NEWS_RESULT_LIMIT_INVALID")
        now = self.clock().astimezone(timezone.utc).replace(microsecond=0)
        lookback_days = int(query.time_range.removesuffix("d"))
        if not 1 <= lookback_days <= 30:
            raise WebSearchError("NEWS_DATE_RANGE_INVALID")
        published_after = now - timedelta(days=lookback_days)
        params = {
            "api_token": self.api_key.get_secret_value(),
            "language": query.language,
            # TheNewsAPI documents UTC values without a timezone suffix.
            "published_after": published_after.strftime("%Y-%m-%dT%H:%M:%S"),
            "published_before": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "sort": "published_at",
            "search": query.text,
            "limit": count,
            "page": 1,
        }
        try:
            data, sha, request_id, requests, retries = self._request_news(params)
        except WebSearchError as exc:
            code = exc.code
            retryable = exc.retryable
            request_count = exc.request_count
            retry_count = exc.retry_count
            params.pop("api_token", None)
            exc.__traceback__ = None
            raise WebSearchError(
                code, retryable=retryable,
                request_count=request_count, retry_count=retry_count,
            ) from None
        finally:
            params.pop("api_token", None)
        articles = data.get("data") if isinstance(data, dict) else None
        meta = data.get("meta") if isinstance(data, dict) else None
        if not isinstance(articles, list) or not isinstance(meta, dict):
            raise WebSearchError("INVALID_SCHEMA", request_count=requests, retry_count=retries)
        if meta.get("page") not in {None, 1}:
            raise WebSearchError("UNEXPECTED_PAGINATION", request_count=requests, retry_count=retries)
        discovered_at = datetime.now(timezone.utc)
        hits = []
        for article in articles[:count]:
            if not isinstance(article, dict) or not article.get("url"):
                continue
            description = str(article.get("description") or article.get("snippet") or "")[:2000]
            hits.append(ProviderSearchHit(
                title=str(article.get("title") or "Untitled")[:500],
                url=str(article["url"]),
                snippet=description,
                rank=len(hits) + 1,
                published_at=_date(article.get("published_at")),
                discovered_at=discovered_at,
                provider_score=(
                    article.get("relevance_score")
                    if isinstance(article.get("relevance_score"), (int, float))
                    else None
                ),
                source_metadata={
                    "article_uuid": str(article.get("uuid") or "")[:100],
                    "source_domain_reported": str(article.get("source") or "")[:253],
                    "language": str(article.get("language") or "")[:20],
                    "categories": [str(item)[:100] for item in (article.get("categories") or [])[:20]],
                    "keywords": str(article.get("keywords") or "")[:500],
                    "snippet_policy": "DISCOVERY_ONLY",
                    "keywords_policy": "DISCOVERY_ONLY",
                    "full_text_available": False,
                },
            ))
        safe_raw = {
            "meta": {
                "returned": len(hits), "limit": count, "page": 1,
            },
            "data": [
                {
                    "uuid": hit.source_metadata["article_uuid"],
                    "title": hit.title,
                    "url": hit.url,
                    "description": hit.snippet,
                    "published_at": hit.published_at.isoformat() if hit.published_at else None,
                    "source": hit.source_metadata["source_domain_reported"],
                    "language": hit.source_metadata["language"],
                    "categories": hit.source_metadata["categories"],
                }
                for hit in hits
            ],
        }
        request_metadata = {
            "request_fingerprint": hashlib.sha256(json.dumps({
                key: value for key, value in params.items() if key != "api_token"
            }, sort_keys=True).encode()).hexdigest(),
            "page": 1,
            "limit": count,
            "language": query.language,
            "published_after": params["published_after"],
            "published_before": params["published_before"],
            "sort": "published_at",
            "search_query_hash": hashlib.sha256(query.text.encode()).hexdigest(),
            **self._usage_metadata,
        }
        return self._page(
            query, THENEWSAPI_ENDPOINT, hits, safe_raw, sha, request_id, requests, retries
        ).model_copy(update={"request_metadata": request_metadata})

    def _request_news(self, params: dict):
        requests = retries = 0
        self._usage_metadata = {
            "usage_headers_present": False,
            "remaining_quota_parsed": False,
        }
        while True:
            requests += 1
            try:
                with self.client.stream("GET", THENEWSAPI_ENDPOINT, params=params) as response:
                    status = response.status_code
                    request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                    self._usage_metadata = _news_usage_metadata(response.headers)
                    if status in {301, 302, 303, 307, 308}:
                        raise WebSearchError("PROVIDER_REDIRECT", request_count=requests, retry_count=retries)
                    status_map = {
                        400: "INVALID_PARAMETERS", 401: "HTTP_401",
                        402: "USAGE_LIMIT_REACHED", 403: "HTTP_403",
                        404: "INVALID_PROVIDER_ENDPOINT", 429: "RATE_LIMIT_REACHED",
                    }
                    if status in status_map:
                        raise WebSearchError(status_map[status], request_count=requests, retry_count=retries)
                    if 500 <= status <= 599:
                        if retries < self.max_retries:
                            retries += 1
                            self.sleep_fn(0)
                            continue
                        raise WebSearchError(
                            "UPSTREAM_5XX", retryable=True,
                            request_count=requests, retry_count=retries,
                        )
                    if status != 200:
                        raise WebSearchError(
                            f"HTTP_{status}", request_count=requests, retry_count=retries
                        )
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_response_bytes:
                        raise WebSearchError(
                            "RESPONSE_TOO_LARGE", request_count=requests, retry_count=retries
                        )
                    body = bytearray()
                    chunks = [response.content] if response.is_stream_consumed else response.iter_bytes()
                    for chunk in chunks:
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise WebSearchError(
                                "RESPONSE_TOO_LARGE", request_count=requests, retry_count=retries
                            )
                        body.extend(chunk)
            except WebSearchError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError):
                if retries < self.max_retries:
                    retries += 1
                    self.sleep_fn(0)
                    continue
                # No exception chaining: httpx request URLs contain api_token.
                raise WebSearchError(
                    "NETWORK_ERROR", retryable=True,
                    request_count=requests, retry_count=retries,
                ) from None
            try:
                data = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                raise WebSearchError(
                    "INVALID_JSON", request_count=requests, retry_count=retries
                ) from None
            return (
                _redact_secret(data, self.api_key.get_secret_value()),
                hashlib.sha256(body).hexdigest(), request_id, requests, retries,
            )


def _news_usage_metadata(headers) -> dict:
    remaining = headers.get("x-usagelimit-remaining") or headers.get("x-ratelimit-remaining")
    present = any(
        key.lower().startswith(("x-usagelimit-", "x-ratelimit-"))
        for key in headers.keys()
    )
    parsed = None
    if remaining is not None:
        try:
            parsed = max(int(remaining), 0)
        except ValueError:
            parsed = None
    return {
        "usage_headers_present": present,
        "remaining_quota_parsed": parsed is not None,
        "remaining_quota": parsed,
    }
