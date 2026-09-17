"""Gemini structured-output adapter for the isolated Phase 3C digest.

The API key is sent only in the ``x-goog-api-key`` request header.  The
adapter does not enable tools or Search Grounding. Only the explicitly enabled
daily-quota model transition is allowed; providers are never changed. Output
still passes the existing Pydantic and exact-evidence validation.
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
PER_MINUTE_QUOTA = re.compile(r"PerMinute", re.IGNORECASE)
DAILY_QUOTA = re.compile(r"PerDay|Daily", re.IGNORECASE)

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


class GeminiRequestGovernor:
    """One pacing/deadline budget shared by every Phase 3C Gemini adapter."""

    def __init__(
        self,
        *,
        minimum_interval_seconds: float,
        max_run_seconds: float,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
    ):
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._sleep = sleep_fn
        self._clock = monotonic_fn
        self._started_at = self._clock()
        self._deadline = self._started_at + max_run_seconds
        self._last_request_at: float | None = None
        self.minute_cooldown_used = False
        self.server_recovery_used = False

    def sleep(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if self._clock() + seconds > self._deadline:
            raise ProviderCallError(
                "Gemini run deadline exhausted",
                diagnostics={
                    "reason_code": "GEMINI_RUN_DEADLINE_EXCEEDED",
                    "halt_provider_for_run": True,
                },
            )
        self._sleep(seconds)

    def before_request(self) -> None:
        now = self._clock()
        if now > self._deadline:
            self.sleep(0)
        if self._last_request_at is not None:
            delay = max(
                0.0,
                self.minimum_interval_seconds - (now - self._last_request_at),
            )
            if delay:
                self.sleep(delay)
        self._last_request_at = self._clock()

    def minute_cooldown(self, seconds: float) -> bool:
        if self.minute_cooldown_used:
            return False
        self.minute_cooldown_used = True
        self.sleep(seconds)
        return True

    def server_recovery_cooldown(self, seconds: float) -> bool:
        """Allow one run-wide recovery pause after exhausted 5xx retries."""

        if self.server_recovery_used:
            return False
        self.server_recovery_used = True
        self.sleep(seconds)
        return True


class GeminiModelController:
    """Shared, explicit model state for all Phase 3C departments.

    A switch is allowed only for the daily per-model free-tier quota and only
    when the operator explicitly enabled and configured the fallback model.
    The transition is immutable audit data; no provider or paid-model fallback
    is inferred by the adapter.
    """

    def __init__(
        self,
        *,
        primary_model: str,
        fallback_model: str | None = None,
        fallback_enabled: bool = False,
    ):
        if not MODEL_NAME.fullmatch(primary_model):
            raise ValueError("GEMINI_ANALYSIS_MODEL is invalid")
        if fallback_model and not MODEL_NAME.fullmatch(fallback_model):
            raise ValueError("GEMINI_FALLBACK_MODEL is invalid")
        if fallback_enabled and not fallback_model:
            raise ValueError("enabled Gemini fallback requires GEMINI_FALLBACK_MODEL")
        if fallback_model == primary_model:
            raise ValueError("Gemini fallback model must differ from primary model")
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self.fallback_enabled = fallback_enabled
        self.active_model = primary_model
        self.models_used: list[str] = []
        self.transitions: list[dict[str, str]] = []

    def record_request(self) -> str:
        if self.active_model not in self.models_used:
            self.models_used.append(self.active_model)
        return self.active_model

    def activate_quota_fallback(self, *, quota_id: str) -> bool:
        if (
            not self.fallback_enabled
            or not self.fallback_model
            or self.active_model != self.primary_model
        ):
            return False
        previous = self.active_model
        self.active_model = self.fallback_model
        self.transitions.append(
            {
                "from_model": previous,
                "to_model": self.active_model,
                "reason_code": "GEMINI_DAILY_QUOTA_FALLBACK",
                "quota_id": quota_id,
            }
        )
        return True


class GeminiGlobalAiAnalysisAdapter:
    name = "GeminiGlobalAiAnalysisAdapter"
    version = "gemini-global-ai-v1"
    prompt_version = "daily-global-ai-analysis-v9"

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        base_url: str,
        model: str,
        prompt_path: Path,
        supplemental_prompt_path: Path | None = None,
        timeout_seconds: float = 60,
        max_retries: int = 2,
        max_provider_requests: int | None = None,
        governor: GeminiRequestGovernor | None = None,
        model_controller: GeminiModelController | None = None,
        rate_limit_cooldown_seconds: float = 60.0,
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
        expected_prompt_version = Path(prompt_path).stem.replace("_", "-")
        if expected_prompt_version != self.prompt_version:
            raise ValueError(
                f"prompt path/version mismatch: {expected_prompt_version} != {self.prompt_version}"
            )

        self.base_url = base_url.rstrip("/")
        self.model_controller = model_controller or GeminiModelController(
            primary_model=model
        )
        if self.model_controller.primary_model != model:
            raise ValueError("adapter model must match the shared controller primary model")
        self.model = self.model_controller.active_model
        self.endpoint = self._endpoint_for(self.model)
        self.instructions = Path(prompt_path).read_text(encoding="utf-8").strip()
        if supplemental_prompt_path is not None:
            supplemental = Path(supplemental_prompt_path).read_text(
                encoding="utf-8"
            ).strip()
            self.instructions = f"{self.instructions}\n\n{supplemental}"
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
        self.request_events: list[dict] = []
        self.models_used: list[str] = []
        self._sleep = sleep_fn
        self._random = random_fn
        self.governor = governor or GeminiRequestGovernor(
            minimum_interval_seconds=0,
            max_run_seconds=2400,
            sleep_fn=sleep_fn,
        )
        self.rate_limit_cooldown_seconds = rate_limit_cooldown_seconds
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            transport=httpx.HTTPTransport(retries=0),
            trust_env=False,
        )

    def _endpoint_for(self, model: str) -> str:
        return f"{self.base_url}/models/{quote(model, safe='-._')}:generateContent"

    def _sync_model(self) -> None:
        self.model = self.model_controller.active_model
        self.endpoint = self._endpoint_for(self.model)

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
            self._sync_model()
            self.governor.before_request()
            request_model = self.model_controller.record_request()
            if request_model not in self.models_used:
                self.models_used.append(request_model)
            self.request_count += 1
            request_attempts += 1
            try:
                response = self.client.post(
                    self.endpoint,
                    headers=self._headers,
                    json=payload,
                )
            except (httpx.TimeoutException, TimeoutError) as exc:
                will_retry = retry_number < self.max_retries and (
                    self.max_provider_requests is None
                    or self.request_count < self.max_provider_requests
                )
                self.request_events.append({
                    "source_url": source_url,
                    "model": request_model,
                    "request_number": self.request_count,
                    "reason_code": "GEMINI_TIMEOUT",
                    "retry_scheduled": will_retry,
                })
                if will_retry:
                    self.governor.sleep(min(2**retry_number, MAX_RETRY_DELAY_SECONDS))
                    continue
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

            diagnostics = _safe_provider_diagnostics(response)
            if response.status_code != 200:
                self.request_events.append({
                    "source_url": source_url,
                    "model": request_model,
                    "request_number": self.request_count,
                    "status_code": response.status_code,
                    **diagnostics,
                })
            quota_ids = diagnostics.get("quota_ids", [])
            daily_quota = any(DAILY_QUOTA.search(value) for value in quota_ids)
            minute_quota = any(PER_MINUTE_QUOTA.search(value) for value in quota_ids)
            retryable = response.status_code >= 500
            retry_after = _retry_after_seconds(response)
            budget_remaining = (
                self.max_provider_requests is None
                or self.request_count < self.max_provider_requests
            )
            if response.status_code == 429 and daily_quota and budget_remaining:
                quota_id = next(
                    (value for value in quota_ids if DAILY_QUOTA.search(value)),
                    "DAILY_QUOTA",
                )
                if self.model_controller.activate_quota_fallback(quota_id=quota_id):
                    self._sync_model()
                    # The model switch has its own explicit, one-time budget and
                    # does not consume the transient retry allowance. Re-entering
                    # is bounded because the controller can switch only once.
                    return self.analyze(
                        metadata=metadata,
                        raw_text=raw_text,
                        source_url=source_url,
                    )
            if (
                response.status_code == 429
                and not daily_quota
                and budget_remaining
            ):
                cooldown = min(
                    retry_after if retry_after is not None else self.rate_limit_cooldown_seconds,
                    self.rate_limit_cooldown_seconds,
                )
                if self.governor.minute_cooldown(cooldown):
                    self.request_events.append({
                        "source_url": source_url,
                        "model": request_model,
                        "request_number": self.request_count,
                        "reason_code": "GEMINI_MINUTE_QUOTA_RECOVERY_SCHEDULED",
                        "cooldown_seconds": round(cooldown, 3),
                    })
                    # Minute quota can arrive after earlier 5xx responses have
                    # consumed this candidate's transient retry allowance. The
                    # shared governor permits exactly one recovery pause for the
                    # whole run, so restarting the candidate is bounded by that
                    # flag, the request budget, and the run deadline.
                    return self.analyze(
                        metadata=metadata,
                        raw_text=raw_text,
                        source_url=source_url,
                    )
            if (
                response.status_code >= 500
                and retry_number >= self.max_retries
                and budget_remaining
                and self.governor.server_recovery_cooldown(
                    self.rate_limit_cooldown_seconds
                )
            ):
                self.request_events.append({
                    "source_url": source_url,
                    "model": request_model,
                    "request_number": self.request_count,
                    "reason_code": "GEMINI_SERVER_RECOVERY_SCHEDULED",
                    "cooldown_seconds": round(
                        self.rate_limit_cooldown_seconds, 3
                    ),
                })
                # A short Gemini outage can span all per-candidate retries. One
                # shared recovery pause gives the run a chance to resume without
                # creating an unbounded retry loop or weakening downstream Ops.
                return self.analyze(
                    metadata=metadata,
                    raw_text=raw_text,
                    source_url=source_url,
                )
            if (
                response.status_code == 200
                or not retryable
                or retry_number >= self.max_retries
                or not budget_remaining
            ):
                break
            delay = (
                min(retry_after, MAX_RETRY_DELAY_SECONDS)
                if retry_after is not None
                else None
            )
            if delay is None:
                delay = min(2**retry_number, MAX_RETRY_DELAY_SECONDS)
                delay += min(max(float(self._random()), 0.0), 1.0) * 0.25
            self.governor.sleep(delay)

        assert response is not None

        if response.status_code != 200:
            if response.status_code == 429:
                safe_diagnostics = _safe_provider_diagnostics(response)
                quota_ids = safe_diagnostics.get("quota_ids", [])
                daily_quota = any(DAILY_QUOTA.search(value) for value in quota_ids)
                reason_code = (
                    "GEMINI_DAILY_QUOTA_EXHAUSTED"
                    if daily_quota
                    else "GEMINI_RATE_LIMITED"
                )
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
                    # Authentication, daily quota, or the explicit request
                    # budget halt the run. A minute quota failure remains
                    # fail-soft after the one shared cooldown so later
                    # candidates can still be attempted within the hard budget.
                    "halt_provider_for_run": (
                        response.status_code in {401, 403}
                        or daily_quota
                        or budget_exhausted
                    ),
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
            self.request_events.append({
                "source_url": source_url,
                "model": request_model,
                "request_number": self.request_count,
                "reason_code": "CONTRACT_VALID_EVIDENCE_REJECTED",
            })
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
        self.request_events.append({
            "source_url": source_url,
            "model": request_model,
            "request_number": self.request_count,
            "reason_code": "VALIDATED_OUTPUT",
        })
        return contract
