import hashlib
import json
from datetime import datetime, timezone

import httpx
from pydantic import SecretStr

from app.daily_ai.contracts import StableStatus
from app.web_discovery.contracts import ProviderSearchHit, ProviderSearchPage, SearchQuery


JUHE_HEADLINES_ENDPOINT = "https://v.juhe.cn/toutiao/index"
JUHE_AI_BRIEF_ENDPOINT = "https://apis.juhe.cn/fapigw/aibrief/list"
ERROR_CODES = {
    10001: StableStatus.AUTHENTICATION_ERROR,
    10002: StableStatus.PERMISSION_DENIED,
    10003: StableStatus.KEY_EXPIRED,
    10009: StableStatus.AUTHENTICATION_ERROR,
    10011: StableStatus.RATE_LIMITED,
    10012: StableStatus.QUOTA_EXCEEDED,
    10013: StableStatus.QUOTA_EXCEEDED,
    10020: StableStatus.MAINTENANCE,
    10021: StableStatus.ENDPOINT_DISABLED,
}


class JuheError(RuntimeError):
    def __init__(self, code: StableStatus, *, business_code: int | None = None):
        self.code = code
        self.business_code = business_code
        super().__init__(code.value)


class _JuheDiscoveryAdapter:
    max_response_bytes = 1_000_000
    endpoint: str
    name: str
    version: str
    result_key: str

    def __init__(self, *, api_key: SecretStr | None, client=None, timeout_seconds: float = 20):
        if api_key is None:
            raise JuheError(StableStatus.MISSING_API_KEY)
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    def _request(self, params: dict) -> tuple[dict, bytes, dict]:
        params["key"] = self.api_key.get_secret_value()
        quota = {"usage_headers_present": False, "remaining_quota_status": "UNAVAILABLE"}
        try:
            with self.client.stream("GET", self.endpoint, params=params) as response:
                if response.status_code == 429:
                    raise JuheError(StableStatus.RATE_LIMITED)
                if response.status_code != 200:
                    raise JuheError(StableStatus.UPSTREAM_ERROR)
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > self.max_response_bytes:
                        raise JuheError(StableStatus.INVALID_SCHEMA)
                    body.extend(chunk)
                quota_headers = [key for key in response.headers if key.lower().startswith(("x-ratelimit-", "x-quota-"))]
                quota = {"usage_headers_present": bool(quota_headers), "remaining_quota_status": "AVAILABLE" if any("remaining" in key.lower() for key in quota_headers) else "UNAVAILABLE"}
        except JuheError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            raise JuheError(StableStatus.UPSTREAM_ERROR) from None
        finally:
            params.pop("key", None)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise JuheError(StableStatus.INVALID_SCHEMA) from None
        try:
            code = int(data.get("error_code"))
        except (AttributeError, TypeError, ValueError):
            raise JuheError(StableStatus.INVALID_SCHEMA) from None
        if code != 0:
            raise JuheError(ERROR_CODES.get(code, StableStatus.UPSTREAM_ERROR), business_code=code)
        return data, bytes(body), quota

    def search(self, query: SearchQuery, *, count: int = 5) -> ProviderSearchPage:
        if not 1 <= count <= 5:
            raise ValueError("Juhe discovery count must be between 1 and 5")
        params = self.parameters(count)
        try:
            data, body, quota = self._request(params)
        except JuheError as exc:
            code, business_code = exc.code, exc.business_code
            exc.__traceback__ = None
            raise JuheError(code, business_code=business_code) from None
        result = data.get("result")
        rows = result.get(self.result_key) if isinstance(result, dict) else None
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise JuheError(StableStatus.INVALID_SCHEMA)
        now = datetime.now(timezone.utc)
        hits = []
        for raw in rows[:count]:
            if not isinstance(raw, dict) or not raw.get("url"):
                continue
            summary = str(raw.get("summary") or "")[:2000]
            hits.append(ProviderSearchHit(
                title=str(raw.get("title") or "Untitled")[:500], url=str(raw["url"]),
                snippet=summary, rank=len(hits) + 1, discovered_at=now,
                source_metadata={
                    "provider_item_id": str(raw.get("id") or raw.get("uniquekey") or "")[:100],
                    "author_name": str(raw.get("author_name") or "")[:200],
                    "category": str(raw.get("type") or raw.get("category") or "")[:100],
                    "published_at_reported": str(raw.get("publish_date") or raw.get("date") or "")[:50],
                    "title_policy": "DISCOVERY_ONLY", "snippet_policy": "DISCOVERY_ONLY",
                    "eligible_as_evidence": False,
                },
            ))
        safe_raw = {"error_code": 0, "result_count": len(hits), "items": [{"id": h.source_metadata["provider_item_id"], "url": h.url, "published_at": h.source_metadata["published_at_reported"]} for h in hits]}
        return ProviderSearchPage(
            provider=self.name, adapter_version=self.version, query_id=query.query_id,
            endpoint=self.endpoint, request_count=1, retry_count=0,
            query_text_hash=hashlib.sha256(query.text.encode()).hexdigest(),
            raw_response_sha256=hashlib.sha256(body).hexdigest(), retrieval_status="SUCCESS",
            request_metadata={"page": 1, "requested_count": count, "business_status_code": 0, **quota},
            results=hits, raw_response=safe_raw,
        )

    def parameters(self, count: int) -> dict:
        raise NotImplementedError

    def close(self):
        if self._owns_client:
            self.client.close()


class JuheHeadlinesAdapter(_JuheDiscoveryAdapter):
    name = "juhe_headlines"
    version = "juhe-headlines-235-v1"
    endpoint = JUHE_HEADLINES_ENDPOINT
    result_key = "data"

    def parameters(self, count: int) -> dict:
        return {"type": "keji", "page": 1, "page_size": count, "is_filter": 1}


class JuheAiBriefAdapter(_JuheDiscoveryAdapter):
    name = "juhe_ai_brief"
    version = "juhe-ai-brief-850-v1"
    endpoint = JUHE_AI_BRIEF_ENDPOINT
    result_key = "list"

    def parameters(self, count: int) -> dict:
        return {"type": "tech", "page": 1, "page_size": count}
