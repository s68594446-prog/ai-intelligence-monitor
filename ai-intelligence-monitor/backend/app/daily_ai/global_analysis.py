import json
from pathlib import Path

from openai import APIError, APITimeoutError, OpenAI
from pydantic import ValidationError

from app.daily_ai.global_contracts import GlobalAiAnalysisContract
from app.pipeline.errors import LLMAdapterTimeout, LLMOutputValidationError, LLMProviderError


class DeepSeekGlobalAiAnalysisAdapter:
    name = "DeepSeekGlobalAiAnalysisAdapter"
    version = "deepseek-global-ai-v1"
    prompt_version = "daily-global-ai-analysis-v1"

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
        payload = {"metadata": metadata, "source_url": source_url, "raw_text": raw_text}
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.instructions},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                max_tokens=3000,
            )
        except (APITimeoutError, TimeoutError) as exc:
            raise LLMAdapterTimeout("global AI analysis timed out") from exc
        except APIError as exc:
            raise LLMProviderError("global AI analysis provider error") from exc
        content = response.choices[0].message.content if response.choices else None
        try:
            contract = GlobalAiAnalysisContract.model_validate(json.loads(content or ""))
        except (ValueError, ValidationError) as exc:
            raise LLMOutputValidationError("global AI analysis schema validation failed") from exc
        if any(e.quote not in raw_text or str(e.source_url).rstrip("/") != source_url.rstrip("/") for e in contract.evidence):
            raise LLMOutputValidationError("global AI evidence quote mismatch")
        return contract
