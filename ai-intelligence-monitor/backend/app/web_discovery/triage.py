import json
from pathlib import Path
from typing import Any, Protocol

from openai import APIError, APITimeoutError, OpenAI
from pydantic import ValidationError

from app.pipeline.errors import LLMAdapterTimeout, LLMConfigurationError, LLMOutputValidationError, LLMProviderError
from app.web_discovery.contracts import DiscoveryTriageContract, NormalizedSearchResult, TriageResult


class DiscoveryTriageAdapter(Protocol):
    name: str
    version: str
    prompt_version: str

    def triage(
        self,
        candidate: NormalizedSearchResult,
        *,
        coverage_snapshot: dict,
        thesis_snapshot: dict,
        reverse_triggers: list[dict],
    ) -> TriageResult: ...


class DeepSeekDiscoveryTriageAdapter:
    name = "DeepSeekDiscoveryTriageAdapter"
    version = "deepseek-discovery-triage-v1"
    prompt_version = "discovery-triage-v1"
    _authorized_keys = ["candidate", "coverage_snapshot", "reverse_triggers", "thesis_snapshot"]

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        prompt_path: Path,
        client: Any | None = None,
    ):
        if not api_key:
            raise LLMConfigurationError("DEEPSEEK_API_KEY is required for discovery triage; no fallback was used")
        self.model = model
        prompt = Path(prompt_path).read_text(encoding="utf-8")
        schema = json.dumps(DiscoveryTriageContract.model_json_schema(), ensure_ascii=False, sort_keys=True)
        self.instructions = f"{prompt}\n\nReturn one JSON object matching this schema:\n{schema}"
        self.client = client or OpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self.last_payload: dict | None = None

    def triage(self, candidate, *, coverage_snapshot, thesis_snapshot, reverse_triggers):
        payload = {
            "coverage_snapshot": coverage_snapshot,
            "thesis_snapshot": thesis_snapshot,
            "reverse_triggers": reverse_triggers,
            "candidate": {
                "candidate_id": candidate.candidate_id,
                "title": candidate.title,
                "url": candidate.canonical_url,
                "snippet": candidate.snippet,
                "source_tier": candidate.source_tier.value,
                "source_domain": candidate.source_domain,
                "query_ids": candidate.query_ids,
            },
        }
        self.last_payload = payload
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.instructions},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
                ],
                response_format={"type": "json_object"},
                max_tokens=2048,
            )
        except (APITimeoutError, TimeoutError) as exc:
            raise LLMAdapterTimeout(f"DeepSeek discovery triage timed out for model {self.model}") from exc
        except APIError as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            raise LLMProviderError(f"DeepSeek discovery triage failed for model {self.model} ({code})") from exc
        choices = getattr(response, "choices", None)
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        if not isinstance(content, str) or not content.strip():
            raise LLMOutputValidationError("DeepSeek discovery triage returned empty JSON content")
        try:
            contract = DiscoveryTriageContract.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMOutputValidationError("DeepSeek discovery triage failed JSON or schema validation") from exc
        if contract.candidate_id != candidate.candidate_id:
            raise LLMOutputValidationError("DeepSeek discovery triage candidate id mismatch")
        return TriageResult(
            candidate_id=candidate.candidate_id,
            contract=contract,
            adapter_name=self.name,
            adapter_version=self.version,
            prompt_version=self.prompt_version,
            model_name=self.model,
            authorized_payload_keys=self._authorized_keys,
        )
