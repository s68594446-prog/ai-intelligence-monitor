import json
import re
from pathlib import Path

from openai import APIError, APITimeoutError, OpenAI
from pydantic import ValidationError

from app.daily_ai.global_contracts import GlobalAiAnalysisContract, closest_snippet, quote_matches
from app.pipeline.errors import LLMAdapterTimeout, LLMOutputValidationError, LLMProviderError


# The model only sees the first slice of very long pages to stay inside the
# context window; evidence quotes are still validated against the FULL
# raw_text (a superset), so the verbatim discipline is unaffected.
ANALYSIS_INPUT_MAX_CHARS = 50_000


class EvidenceMismatch(LLMOutputValidationError):
    """Evidence quote failed verbatim validation; carries per-quote diagnostics."""

    def __init__(self, message: str, *, diagnostics: list[dict]):
        super().__init__(message)
        self.diagnostics = diagnostics


class AnalysisOutputError(LLMOutputValidationError):
    """Model output unusable (empty/truncated/non-JSON/schema); carries response diagnostics."""

    def __init__(self, message: str, *, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = diagnostics


class ProviderCallError(LLMProviderError):
    """Provider API call failed; carries HTTP-level diagnostics without credentials."""

    def __init__(self, message: str, *, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = diagnostics


def _strip_code_fence(content: str) -> str:
    """Unwrap a ```json ... ``` markdown fence; the JSON inside is still strictly validated."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


class DeepSeekGlobalAiAnalysisAdapter:
    name = "DeepSeekGlobalAiAnalysisAdapter"
    version = "deepseek-global-ai-v2"
    prompt_version = "daily-global-ai-analysis-v2"

    def __init__(self, *, api_key: str, base_url: str, model: str, prompt_path: Path, timeout_seconds: float = 60, max_retries: int = 2, client=None):
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        self.model = model
        self.client = client or OpenAI(api_key=api_key, base_url=base_url.rstrip("/"), timeout=timeout_seconds, max_retries=max_retries)
        self.instructions = (
            Path(prompt_path).read_text(encoding="utf-8").strip()
            + "\n\nReturn exactly one JSON object matching this JSON Schema:\n"
            + json.dumps(GlobalAiAnalysisContract.model_json_schema(), ensure_ascii=False, sort_keys=True)
        )

    def analyze(self, *, metadata: dict, raw_text: str, source_url: str) -> GlobalAiAnalysisContract:
        sent_text = raw_text[:ANALYSIS_INPUT_MAX_CHARS]
        payload = {
            "metadata": {**metadata, "raw_text_truncated_for_model": len(raw_text) > len(sent_text)},
            "source_url": source_url,
            "raw_text": sent_text,
        }
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.instructions},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                max_tokens=6000,
            )
        except (APITimeoutError, TimeoutError) as exc:
            raise LLMAdapterTimeout("global AI analysis timed out") from exc
        except APIError as exc:
            raise ProviderCallError(
                "global AI analysis provider error",
                diagnostics={
                    "reason_code": "PROVIDER_API_ERROR",
                    "error_type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                    "body_head": str(getattr(exc, "body", None) or getattr(exc, "message", exc))[:500],
                },
            ) from exc
        choice = response.choices[0] if getattr(response, "choices", None) else None
        message = getattr(choice, "message", None) if choice is not None else None
        content = getattr(message, "content", None) if message is not None else None
        diagnostics = {
            "finish_reason": getattr(choice, "finish_reason", None),
            "usage": str(getattr(response, "usage", None)),
            "content_length": len(content or ""),
            "content_head": (content or "")[:500],
        }
        if diagnostics["finish_reason"] == "length":
            raise AnalysisOutputError("global AI analysis output truncated", diagnostics={**diagnostics, "reason_code": "TRUNCATED_OUTPUT"})
        if not content or not content.strip():
            raise AnalysisOutputError("global AI analysis returned empty content", diagnostics={**diagnostics, "reason_code": "EMPTY_CONTENT"})
        try:
            parsed = json.loads(_strip_code_fence(content))
        except ValueError as exc:
            raise AnalysisOutputError("global AI analysis returned invalid JSON", diagnostics={**diagnostics, "reason_code": "INVALID_JSON"}) from exc
        try:
            contract = GlobalAiAnalysisContract.model_validate(parsed)
        except ValidationError as exc:
            raise AnalysisOutputError(
                "global AI analysis schema validation failed",
                diagnostics={**diagnostics, "reason_code": "SCHEMA_VALIDATION_FAILED", "validation_errors": str(exc)[:500]},
            ) from exc
        failed = [
            e for e in contract.evidence
            if not quote_matches(e.quote, raw_text) or str(e.source_url).rstrip("/") != source_url.rstrip("/")
        ]
        if failed:
            raise EvidenceMismatch(
                "global AI evidence quote mismatch",
                diagnostics=[
                    {
                        "model_quote": e.quote,
                        "url_expected": source_url,
                        "url_given": str(e.source_url),
                        **closest_snippet(e.quote, raw_text),
                    }
                    for e in failed
                ],
            )
        return contract
