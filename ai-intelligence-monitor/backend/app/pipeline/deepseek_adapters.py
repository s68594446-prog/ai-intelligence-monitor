import json
from pathlib import Path
from typing import Any

from openai import APIError, APITimeoutError, OpenAI
from pydantic import ValidationError

from app.pipeline.adapters import AnalysisAdapter, RedTeamAdapter
from app.pipeline.contracts import (
    AnalysisResult,
    DocumentInput,
    FactInput,
    LLMAnalysisContract,
    LLMRedTeamContract,
    RedTeamResult,
)
from app.pipeline.errors import (
    LLMAdapterTimeout,
    LLMConfigurationError,
    LLMOutputValidationError,
    LLMProviderError,
)
from app.pipeline.openai_adapters import (
    _adjudication_for,
    _direction,
    _source_quality,
    _validate_evidence,
)
from app.prompts import ANALYSIS_PROMPT_VERSION, RED_TEAM_PROMPT_VERSION, render_prompt


def _json_instructions(instructions: str, schema: type[LLMAnalysisContract] | type[LLMRedTeamContract]) -> str:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, sort_keys=True)
    return (
        f"{instructions}\n\n"
        "Return exactly one JSON object with no markdown or surrounding commentary. "
        "The JSON object must satisfy this JSON Schema:\n"
        f"{schema_json}"
    )


def _response_content(response: Any, operation: str) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        raise LLMOutputValidationError(f"DeepSeek {operation} returned no choices")
    choice = choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise LLMOutputValidationError(f"DeepSeek {operation} JSON output was truncated")
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if not isinstance(content, str) or not content.strip():
        raise LLMOutputValidationError(f"DeepSeek {operation} returned empty JSON content")
    return content


def _parse_contract(content: str, schema: type[LLMAnalysisContract] | type[LLMRedTeamContract], operation: str):
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMOutputValidationError(f"DeepSeek {operation} returned invalid JSON") from exc
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise LLMOutputValidationError(
            f"DeepSeek {operation} failed schema validation"
        ) from exc


class DeepSeekAnalysisAdapter(AnalysisAdapter):
    name = "DeepSeekAnalysisAdapter"
    version = "deepseek-chat-completions-analysis-v1"
    prompt_version = ANALYSIS_PROMPT_VERSION

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        prompt_path: Path,
        coverage_snapshot: dict,
        thesis_snapshot: dict,
        client: Any | None = None,
    ):
        if not api_key:
            raise LLMConfigurationError(
                "DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek; no fake fallback was used"
            )
        self.model = model
        self.instructions = _json_instructions(
            render_prompt(prompt_path, coverage_snapshot, thesis_snapshot),
            LLMAnalysisContract,
        )
        self.client = client or OpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def analyze(self, document: DocumentInput) -> AnalysisResult:
        input_payload = (
            f"DOCUMENT_REFERENCE_ID: {document.fixture_id}\n"
            f"TITLE: {document.title}\n"
            f"SOURCE_NAME: {document.source.name}\n"
            f"SOURCE_TIER: {document.source.trust_tier.value}\n"
            f"PUBLISHED_AT: {document.published_at.isoformat()}\n"
            f"URL: {document.url or 'N/A'}\n"
            f"RAW_TEXT:\n{document.raw_text}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.instructions},
                    {"role": "user", "content": input_payload},
                ],
                response_format={"type": "json_object"},
                max_tokens=8192,
            )
        except (APITimeoutError, TimeoutError) as exc:
            raise LLMAdapterTimeout(f"DeepSeek analysis timed out for model {self.model}") from exc
        except APIError as exc:
            code = getattr(exc, "code", None) or exc.__class__.__name__
            raise LLMProviderError(
                f"DeepSeek analysis request failed for model {self.model} ({code})"
            ) from exc
        contract = _parse_contract(
            _response_content(response, "analysis"),
            LLMAnalysisContract,
            "analysis",
        )
        _validate_evidence(contract, document)
        facts = [
            FactInput(
                statement=claim.statement,
                fact_type=claim.fact_type,
                value_kind=claim.value_kind,
                value=claim.value,
                source_refs=[document.fixture_id],
                missing_marker=claim.missing_marker,
            )
            for claim in contract.extracted_claims
        ]
        return AnalysisResult(
            adapter_name=self.name,
            adapter_version=self.version,
            affected_variable=contract.affected_variables[0] if contract.affected_variables else "none",
            direction=_direction(contract.thesis_direction),
            novelty=contract.novelty,
            materiality=contract.materiality,
            source_quality=_source_quality(document.source.trust_tier),
            signal_level=contract.proposed_signal_level,
            facts=facts,
            marginal_change=contract.marginal_change,
            thesis_impact=contract.reasoning_summary,
            unknowns=contract.unknowns,
            next_validation="; ".join(contract.next_validation_points) or "N/A",
            adjudication=_adjudication_for(
                contract.proposed_signal_level,
                contract.coverage_match,
            ),
            out_of_scope=not contract.coverage_match,
            research_contract=contract,
            prompt_version=self.prompt_version,
            model_name=self.model,
            schema_validated=True,
        )


class DeepSeekRedTeamAdapter(RedTeamAdapter):
    name = "DeepSeekRedTeamAdapter"
    version = "deepseek-chat-completions-red-team-v1"
    prompt_version = RED_TEAM_PROMPT_VERSION

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        prompt_path: Path,
        coverage_snapshot: dict,
        thesis_snapshot: dict,
        client: Any | None = None,
    ):
        if not api_key:
            raise LLMConfigurationError(
                "DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek; no fake fallback was used"
            )
        self.model = model
        self.instructions = _json_instructions(
            render_prompt(prompt_path, coverage_snapshot, thesis_snapshot),
            LLMRedTeamContract,
        )
        self.client = client or OpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def review(
        self,
        document: DocumentInput,
        analysis: AnalysisResult | None = None,
    ) -> RedTeamResult:
        if analysis is None:
            raise LLMOutputValidationError("Red Team requires a validated analysis result")
        input_payload = (
            f"DOCUMENT_REFERENCE_ID: {document.fixture_id}\n"
            f"RAW_TEXT:\n{document.raw_text}\n\n"
            f"VALIDATED_ANALYSIS:\n{analysis.model_dump_json()}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.instructions},
                    {"role": "user", "content": input_payload},
                ],
                response_format={"type": "json_object"},
                max_tokens=8192,
            )
        except (APITimeoutError, TimeoutError) as exc:
            raise LLMAdapterTimeout(f"DeepSeek Red Team timed out for model {self.model}") from exc
        except APIError as exc:
            code = getattr(exc, "code", None) or exc.__class__.__name__
            raise LLMProviderError(
                f"DeepSeek Red Team request failed for model {self.model} ({code})"
            ) from exc
        contract = _parse_contract(
            _response_content(response, "Red Team"),
            LLMRedTeamContract,
            "Red Team",
        )
        return RedTeamResult(
            adapter_name=self.name,
            adapter_version=self.version,
            action=contract.action,
            resulting_level=contract.recommended_signal_level,
            assumption_at_risk="; ".join(contract.challenged_assumptions) or "UNKNOWN",
            alternative_explanation="; ".join(contract.alternative_explanations) or "UNKNOWN",
            confirmation_bias=contract.confirmation_bias_risk,
            market_knowledge=contract.priced_in_assessment,
            cannot_prove="; ".join(contract.non_conclusions) or "UNKNOWN",
            next_validation="; ".join(contract.validation_points) or "UNKNOWN",
            worst_position_risk=contract.downside_case,
            research_contract=contract,
            prompt_version=self.prompt_version,
            model_name=self.model,
            schema_validated=True,
        )
