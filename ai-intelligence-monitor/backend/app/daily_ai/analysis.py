import json

from openai import APIError, APITimeoutError, OpenAI
from pydantic import ValidationError

from app.daily_ai.contracts import AShareAnalysisContract
from app.pipeline.errors import LLMAdapterTimeout, LLMOutputValidationError, LLMProviderError


class DeepSeekAShareAiAnalysisAdapter:
    name = "DeepSeekAShareAiAnalysisAdapter"
    version = "deepseek-a-share-ai-v1"
    prompt_version = "daily-a-share-ai-analysis-v1"

    def __init__(self, *, api_key: str, base_url: str, model: str, timeout_seconds: float = 60, max_retries: int = 2, client=None):
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        self.model = model
        self.client = client or OpenAI(api_key=api_key, base_url=base_url.rstrip("/"), timeout=timeout_seconds, max_retries=max_retries)
        self.instructions = "Analyze one official A-share disclosure for AI relevance. Do not give investment advice, infer price causes, equate framework agreements with revenue, plans with delivered capacity, or index membership with a benefit. Every factual claim must quote exact source text. Return JSON matching: " + json.dumps(AShareAnalysisContract.model_json_schema(), ensure_ascii=False, sort_keys=True)

    def analyze(self, *, metadata: dict, raw_text: str, source_url: str) -> AShareAnalysisContract:
        payload = {"metadata": metadata, "source_url": source_url, "raw_text": raw_text}
        try:
            response = self.client.chat.completions.create(model=self.model, messages=[{"role": "system", "content": self.instructions}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], response_format={"type": "json_object"}, max_tokens=3000)
        except (APITimeoutError, TimeoutError) as exc:
            raise LLMAdapterTimeout("A-share AI analysis timed out") from exc
        except APIError as exc:
            raise LLMProviderError("A-share AI analysis provider error") from exc
        content = response.choices[0].message.content if response.choices else None
        try:
            contract = AShareAnalysisContract.model_validate(json.loads(content or ""))
        except (ValueError, ValidationError) as exc:
            raise LLMOutputValidationError("A-share AI analysis schema validation failed") from exc
        if any(e.quote not in raw_text or str(e.source_url).rstrip("/") != source_url.rstrip("/") for e in contract.evidence):
            raise LLMOutputValidationError("A-share AI evidence quote mismatch")
        return contract
