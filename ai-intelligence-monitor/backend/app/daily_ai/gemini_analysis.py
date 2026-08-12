"""Gemini structured-output adapter for the isolated Phase 3C digest.

The API key is sent only in the ``x-goog-api-key`` request header.  The
adapter does not enable tools or Search Grounding, does not fall back to a
different model/provider, and still subjects output to the existing Pydantic
and exact-evidence validation.
"""

import json
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from app.daily_ai.global_analysis import (
    ANALYSIS_INPUT_MAX_CHARS,
    AnalysisOutputError,
    EvidenceMismatch,
    ProviderCallError,
)
from app.daily_ai.global_contracts import (
    GlobalAiAnalysisContract,
    closest_snippet,
    quote_matches,
)


OFFICIAL_GEMINI_HOST = "generativelanguage.googleapis.com"
MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_PROVIDER_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_RETRY_DELAY_SECONDS = 10.0

# The GenerateContent API supports a documented subset of JSON Schema.  Keys
# omitted here remain enforced by Pydantic after generation; for example the
# contract's ``const: false`` investment-advice guard is not weakened merely
# because Gemini cannot consume ``const`` in its response schema.
GEMINI_JSON_SCHEMA_KEYS = {
    "$id",
    "$defs",
    "$ref",
    "$anchor",
    "type",
    "format",
    "title",
    "description",
    "enum",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "anyOf",
    "oneOf",
    "properties",
    "additionalProperties",
    "required",
    "propertyOrdering",
}
ALLOWED_METADATA_FIELDS = {"title", "source_domain", "source_tier"}
SAFETY_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
}


def gemini_json_schema(schema: dict) -> dict:
    """Convert a Pydantic schema to Gemini's supported JSON Schema subset."""

    def convert(value, *, container_key: str | None = None):
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        if container_key in {"properties", "$defs"}:
            return {name: convert(child) for name, child in value.items()}
        result = {}
        for key, child in value.items():
            if key not in GEMINI_JSON_SCHEMA_KEYS:
                continue
            result[key] = convert(child, container_key=key)
        return result

    return convert(schema)


def _safe_usage(response: dict) -> dict:
    usage = response.get("usageMetadata")
    if not isinstance(usage, dict):
        return {}
    allowed = {
        "promptTokenCount",
        "candidatesTokenCount",
        "totalTokenCount",
        "thoughtsTokenCount",
    }
    return {key: usage[key] for key in allowed if key in usage}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _safe_provider_diagnostics(response: httpx.Response) -> dict:
    """Extract allowlisted quota metadata without provider messages or secrets."""

    result: dict = {}
    retry_after = _retry_after_seconds(response)
    if retry_after is not None:
        result["retry_after_seconds"] = round(retry_after, 3)
    try:
        envelope = response.json()
    except ValueError:
        return result
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if not isinstance(error, dict):
        return result
    status = error.get("status")
    if isinstance(status, str) and SAFE_PROVIDER_CODE.fullmatch(status):
        result["provider_status"] = status
    quota_ids: set[str] = set()
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("violations") or []:
            quota_id = violation.get("quotaId") if isinstance(violation, dict) else None
            if isinstance(quota_id, str) and SAFE_PROVIDER_CODE.fullmatch(quota_id):
                quota_ids.add(quota_id)
    if quota_ids:
        result["quota_ids"] = sorted(quota_ids)[:5]
    return result


class GeminiGlobalAiAnalysisAdapter:
    name = "GeminiGlobalAiAnalysisAdapter"
    version = "gemini-global-ai-v1"
    prompt_version = "daily-global-ai-analysis-v8"

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        base_url: str,
        model: str,
        prompt_path: Path,
        timeout_seconds: float = 60,
        max_retries: int = 2,
        max_provider_requests: int | None = None,
        sleep_fn=time.sleep,
        random_fn=random.random,
        client: httpx.Client | None = None,
    ):
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key:
            raise ValueError("GEMINI_API_KEY is required")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != OFFICIAL_GEMINI_HOST
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("GEMINI_BASE_URL must be an HTTPS Gemini API URL")
        if not MODEL_NAME.fullmatch(model):
            raise ValueError("GEMINI_ANALYSIS_MODEL is invalid")
        if not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if max_provider_requests is not None and max_provider_requests < 1:
            raise ValueError("max_provider_requests must be positive")

        self.model = model
        self.endpoint = (
            f"{base_url.rstrip('/')}/models/{quote(model, safe='-._')}:generateContent"
        )
        self.instructions = Path(prompt_path).read_text(encoding="utf-8").strip()
        self.response_schema = gemini_json_schema(
            GlobalAiAnalysisContract.model_json_schema()
        )
        self._headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": key,
            "x-goog-api-client": "ai-intelligence-monitor/phase3c",
        }
        self._owns_client = client is None
        self.max_retries = max_retries
        self.max_provider_requests = max_provider_requests
        self.request_count = 0
        self._sleep = sleep_fn
        self._random = random_fn
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            transport=httpx.HTTPTransport(retries=0),
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _payload(self, *, metadata: dict, raw_text: str, source_url: str) -> dict:
        sent_text = raw_text[:ANALYSIS_INPUT_MAX_CHARS]
        safe_metadata = {
            key: metadata[key]
            for key in ALLOWED_METADATA_FIELDS
            if key in metadata
        }
        safe_metadata["raw_text_truncated_for_model"] = len(raw_text) > len(sent_text)
        user_input = {
            "metadata": safe_metadata,
            "source_url": source_url,
            "raw_text": sent_text,
        }
        return {
            "system_instruction": {"parts": [{"text": self.instructions}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": json.dumps(user_input, ensure_ascii=False)}
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": self.response_schema,
                "maxOutputTokens": 6000,
            },
        }

    def analyze(
        self,
        *,
        metadata: dict,
        raw_text: str,
        source_url: str,
    ) -> GlobalAiAnalysisContract:
        payload = self._payload(
            metadata=metadata,
            raw_text=raw_text,
            source_url=source_url,
        )
        response = None
        request_attempts = 0
        for retry_number in range(self.max_retries + 1):
            if (
                self.max_provider_requests is not None
                and self.request_count >= self.max_provider_requests
            ):
                raise ProviderCallError(
                    "Gemini request budget exhausted",
                    diagnostics={
                        "reason_code": "GEMINI_REQUEST_BUDGET_EXHAUSTED",
                        "request_attempts": request_attempts,
                        "provider_requests_total": self.request_count,
                        "halt_provider_for_run": True,
                    },
                )
            self.request_count += 1
            request_attempts += 1
            try:
                response = self.client.post(
                    self.endpoint,
                    headers=self._headers,
                    json=payload,
                )
            except (httpx.TimeoutException, TimeoutError) as exc:
                raise ProviderCallError(
                    "Gemini analysis timed out",
                    diagnostics={
                        "reason_code": "GEMINI_TIMEOUT",
                        "request_attempts": request_attempts,
                    },
                ) from exc
            except httpx.RequestError as exc:
                raise ProviderCallError(
                    "Gemini analysis network error",
                    diagnostics={
                        "reason_code": "GEMINI_NETWORK_ERROR",
                        "error_type": type(exc).__name__,
                        "request_attempts": request_attempts,
                    },
                ) from exc

            retryable = response.status_code == 429 or response.status_code >= 500
            retry_after = _retry_after_seconds(response)
            retry_delay_too_long = (
                retry_after is not None and retry_after > MAX_RETRY_DELAY_SECONDS
            )
            budget_remaining = (
                self.max_provider_requests is None
                or self.request_count < self.max_provider_requests
            )
            if (
                response.status_code == 200
                or not retryable
                or retry_number >= self.max_retries
                or retry_delay_too_long
                or not budget_remaining
            ):
                break
            delay = retry_after
            if delay is None:
                delay = min(2**retry_number, MAX_RETRY_DELAY_SECONDS)
                delay += min(max(float(self._random()), 0.0), 1.0) * 0.25
            self._sleep(delay)

        assert response is not None

        if response.status_code != 200:
            if response.status_code == 429:
                reason_code = "GEMINI_RATE_LIMITED"
            elif response.status_code == 401:
                reason_code = "GEMINI_HTTP_401"
            elif response.status_code == 403:
                reason_code = "GEMINI_HTTP_403"
            elif response.status_code >= 500:
                reason_code = "GEMINI_SERVER_ERROR"
            else:
                reason_code = "GEMINI_HTTP_ERROR"
            budget_exhausted = (
                self.max_provider_requests is not None
                and self.request_count >= self.max_provider_requests
            )
            raise ProviderCallError(
                "Gemini analysis provider error",
                diagnostics={
                    "reason_code": reason_code,
                    "status_code": response.status_code,
                    "request_attempts": request_attempts,
                    "provider_requests_total": self.request_count,
                    # After bounded 429 retries, further calls in this run are
                    # stopped. This avoids turning a per-minute or per-day free
                    # tier limit into dozens of guaranteed failed requests.
                    "halt_provider_for_run": response.status_code == 429
                    or budget_exhausted,
                    **_safe_provider_diagnostics(response),
                },
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AnalysisOutputError(
                "Gemini returned an invalid response envelope",
                diagnostics={"reason_code": "INVALID_RESPONSE_JSON"},
            ) from exc

        prompt_feedback = data.get("promptFeedback") if isinstance(data, dict) else None
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            blocked = isinstance(prompt_feedback, dict) and bool(
                prompt_feedback.get("blockReason")
            )
            raise AnalysisOutputError(
                "Gemini returned no candidate",
                diagnostics={
                    "reason_code": "SAFETY_BLOCKED" if blocked else "NO_CANDIDATES",
                    "block_reason": (
                        prompt_feedback.get("blockReason") if blocked else None
                    ),
                    "usage": _safe_usage(data if isinstance(data, dict) else {}),
                },
            )

        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        finish_reason = str(candidate.get("finishReason") or "")
        diagnostics = {
            "finish_reason": finish_reason or None,
            "usage": _safe_usage(data),
        }
        if finish_reason == "MAX_TOKENS":
            raise AnalysisOutputError(
                "Gemini analysis output truncated",
                diagnostics={**diagnostics, "reason_code": "TRUNCATED_OUTPUT"},
            )
        if finish_reason in SAFETY_FINISH_REASONS:
            raise AnalysisOutputError(
                "Gemini analysis was safety blocked",
                diagnostics={**diagnostics, "reason_code": "SAFETY_BLOCKED"},
            )

        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        text_parts = [
            part["text"]
            for part in parts or []
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        text = "".join(text_parts).strip()
        diagnostics["content_length"] = len(text)
        if not text:
            raise AnalysisOutputError(
                "Gemini analysis returned empty content",
                diagnostics={**diagnostics, "reason_code": "EMPTY_CONTENT"},
            )
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise AnalysisOutputError(
                "Gemini analysis returned invalid JSON",
                diagnostics={**diagnostics, "reason_code": "INVALID_JSON"},
            ) from exc
        try:
            contract = GlobalAiAnalysisContract.model_validate(parsed)
        except ValidationError as exc:
            raise AnalysisOutputError(
                "Gemini analysis schema validation failed",
                diagnostics={
                    **diagnostics,
                    "reason_code": "SCHEMA_VALIDATION_FAILED",
                    "validation_errors": str(exc)[:500],
                },
            ) from exc

        failed = [
            entry
            for entry in contract.evidence
            if not quote_matches(entry.quote, raw_text)
            or str(entry.source_url).rstrip("/") != source_url.rstrip("/")
        ]
        if failed:
            raise EvidenceMismatch(
                "Gemini evidence quote mismatch",
                diagnostics=[
                    {
                        "model_quote": entry.quote,
                        "url_expected": source_url,
                        "url_given": str(entry.source_url),
                        **closest_snippet(entry.quote, raw_text),
                    }
                    for entry in failed
                ],
            )
        return contract
