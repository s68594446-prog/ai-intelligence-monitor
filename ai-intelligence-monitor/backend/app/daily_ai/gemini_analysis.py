"""Gemini structured-output adapter for the isolated Phase 3C digest.

The API key is sent only in the ``x-goog-api-key`` request header.  The
adapter does not enable tools or Search Grounding, does not fall back to a
different model/provider, and still subjects output to the existing Pydantic
and exact-evidence validation.
"""

import json
import re
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
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            transport=httpx.HTTPTransport(retries=max_retries),
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
        try:
            response = self.client.post(
                self.endpoint,
                headers=self._headers,
                json=self._payload(
                    metadata=metadata,
                    raw_text=raw_text,
                    source_url=source_url,
                ),
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderCallError(
                "Gemini analysis timed out",
                diagnostics={"reason_code": "GEMINI_TIMEOUT"},
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderCallError(
                "Gemini analysis network error",
                diagnostics={
                    "reason_code": "GEMINI_NETWORK_ERROR",
                    "error_type": type(exc).__name__,
                },
            ) from exc

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
            raise ProviderCallError(
                "Gemini analysis provider error",
                diagnostics={
                    "reason_code": reason_code,
                    "status_code": response.status_code,
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
