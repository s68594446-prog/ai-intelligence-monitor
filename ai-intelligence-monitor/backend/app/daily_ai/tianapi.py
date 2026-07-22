import hashlib
import json

import httpx
from pydantic import SecretStr, ValidationError

from app.daily_ai.contracts import StableStatus, TianApiPage, TianNewsItem


TIANAPI_ENDPOINT = "https://apis.tianapi.com/ai/index"
BUSINESS_CODES = {
    130: StableStatus.RATE_LIMITED,
    140: StableStatus.PERMISSION_DENIED,
    150: StableStatus.USAGE_LIMIT_REACHED,
    160: StableStatus.API_NOT_ACTIVATED,
    190: StableStatus.AUTHENTICATION_ERROR,
    230: StableStatus.AUTHENTICATION_ERROR,
}


class TianApiError(RuntimeError):
    def __init__(self, code: StableStatus):
        self.code = code
        super().__init__(code.value)


class TianApiAiNewsAdapter:
    name = "tianapi_ai"
    version = "tianapi-ai-v1"

    def __init__(self, *, api_key: SecretStr | None, client=None, timeout_seconds: float = 20):
        if api_key is None:
            raise TianApiError(StableStatus.MISSING_API_KEY)
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    def discover(self, *, num: int = 3) -> TianApiPage:
        if not 1 <= num <= 5:
            raise ValueError("TianAPI num must be between 1 and 5")
        params = {"key": self.api_key.get_secret_value(), "page": 1, "num": num, "form": 1}
        try:
            with self.client.stream("GET", TIANAPI_ENDPOINT, params=params) as response:
                if response.status_code == 429:
                    raise TianApiError(StableStatus.RATE_LIMITED)
                if response.status_code != 200:
                    raise TianApiError(StableStatus.UPSTREAM_ERROR)
                body = response.read()
                if len(body) > 1_000_000:
                    raise TianApiError(StableStatus.INVALID_SCHEMA)
        except TianApiError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            raise TianApiError(StableStatus.UPSTREAM_ERROR) from None
        finally:
            params.pop("key", None)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise TianApiError(StableStatus.INVALID_SCHEMA) from None
        code = data.get("code") if isinstance(data, dict) else None
        if code != 200:
            raise TianApiError(BUSINESS_CODES.get(code, StableStatus.UPSTREAM_ERROR))
        result = data.get("result")
        news = result.get("newslist") if isinstance(result, dict) else None
        if not isinstance(news, list):
            raise TianApiError(StableStatus.INVALID_SCHEMA)
        items = []
        try:
            for raw in news[:num]:
                items.append(TianNewsItem(
                    provider_id=str(raw.get("id") or "")[:100], title=str(raw.get("title") or "")[:500],
                    url=raw.get("url"), description=str(raw.get("description") or "")[:2000],
                    published_at=str(raw.get("ctime") or "") or None, source=str(raw.get("source") or "")[:200],
                ))
        except (ValidationError, AttributeError, TypeError):
            raise TianApiError(StableStatus.INVALID_SCHEMA) from None
        return TianApiPage(
            status=StableStatus.SUCCESS, request_count=1, page=1, num=num,
            # TianAPI exposes result cardinality (`allnum`), not remaining account quota.
            result_count=len(items), remaining_quota_status="UNAVAILABLE",
            raw_response_sha256=hashlib.sha256(body).hexdigest(), items=items,
        )

    def close(self):
        if self._owns_client:
            self.client.close()
