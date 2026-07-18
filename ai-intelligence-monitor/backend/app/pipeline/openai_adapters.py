import re
from pathlib import Path
from typing import Any

from openai import APIError, APITimeoutError, OpenAI
from pydantic import ValidationError

from app.core.enums import (
    Adjudication,
    Direction,
    MissingMarker,
    SignalLevel,
    ThesisDirection,
    TrustTier,
    ValueKind,
)
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
from app.prompts import ANALYSIS_PROMPT_VERSION, RED_TEAM_PROMPT_VERSION, render_prompt


def _adjudication_for(level: SignalLevel, coverage_match: bool) -> Adjudication:
    if not coverage_match or level == SignalLevel.S0:
        return Adjudication.DROP
    if level == SignalLevel.S1:
        return Adjudication.ARCHIVE
    if level == SignalLevel.S2:
        return Adjudication.INCLUDE_WEEKLY
    return Adjudication.SEND_ALERT


def _direction(value: ThesisDirection) -> Direction:
    if value == ThesisDirection.STRENGTHEN:
        return Direction.SUPPORTS
    if value == ThesisDirection.WEAKEN:
        return Direction.CHALLENGES
    return Direction.NEUTRAL


def _source_quality(tier: TrustTier) -> int:
    return {TrustTier.T1: 100, TrustTier.T2: 70, TrustTier.T3: 20}[tier]


def _validate_evidence(contract: LLMAnalysisContract, document: DocumentInput) -> None:
    for claim in contract.extracted_claims:
        if not claim.evidence_references:
            raise LLMOutputValidationError(f"claim has no evidence reference: {claim.statement}")
        for reference in claim.evidence_references:
            if reference.reference_id != document.fixture_id:
                raise LLMOutputValidationError(
                    f"evidence reference {reference.reference_id} does not match {document.fixture_id}"
                )
            if reference.quote not in document.raw_text:
                raise LLMOutputValidationError("evidence quote is not present in the manual raw_text")
        contains_number = bool(
            re.search(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?%?", claim.statement)
            or (
                claim.value
                and re.search(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?%?", claim.value)
            )
        )
        if (claim.value_kind == ValueKind.NUMBER or contains_number) and not claim.evidence_references:
            raise LLMOutputValidationError("numeric claim has no evidence reference")


class OpenAIAnalysisAdapter(AnalysisAdapter):
    name = "OpenAIAnalysisAdapter"
    version = "openai-responses-analysis-v1"
    prompt_version = ANALYSIS_PROMPT_VERSION

    def __init__(
        self,
        *,
        api_key: str | None,
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
                "OPENAI_API_KEY is required for live manual analysis; no fake fallback was used"
            )
        self.model = model
        self.instructions = render_prompt(prompt_path, coverage_snapshot, thesis_snapshot)
        self.client = client or OpenAI(
            api_key=api_key,
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
            f"OPTIONAL_NOTES: {document.optional_notes or 'N/A'}\n"
            f"RAW_TEXT:\n{document.raw_text}"
        )
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=self.instructions,
                input=input_payload,
                text_format=LLMAnalysisContract,
            )
        except (APITimeoutError, TimeoutError) as exc:
            raise LLMAdapterTimeout(f"OpenAI analysis timed out for model {self.model}") from exc
        except APIError as exc:
            code = getattr(exc, "code", None) or exc.__class__.__name__
            raise LLMProviderError(
                f"OpenAI analysis request failed for model {self.model} ({code})"
            ) from exc
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise LLMOutputValidationError("OpenAI analysis returned no parsed structured output")
        try:
            contract = LLMAnalysisContract.model_validate(parsed)
        except ValidationError as exc:
            raise LLMOutputValidationError("OpenAI analysis failed schema validation") from exc
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


class OpenAIRedTeamAdapter(RedTeamAdapter):
    name = "OpenAIRedTeamAdapter"
    version = "openai-responses-red-team-v1"
    prompt_version = RED_TEAM_PROMPT_VERSION

    def __init__(
        self,
        *,
        api_key: str | None,
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
                "OPENAI_API_KEY is required for live Red Team analysis; no fake fallback was used"
            )
        self.model = model
        self.instructions = render_prompt(prompt_path, coverage_snapshot, thesis_snapshot)
        self.client = client or OpenAI(
            api_key=api_key,
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
            response = self.client.responses.parse(
                model=self.model,
                instructions=self.instructions,
                input=input_payload,
                text_format=LLMRedTeamContract,
            )
        except (APITimeoutError, TimeoutError) as exc:
            raise LLMAdapterTimeout(f"OpenAI Red Team timed out for model {self.model}") from exc
        except APIError as exc:
            code = getattr(exc, "code", None) or exc.__class__.__name__
            raise LLMProviderError(
                f"OpenAI Red Team request failed for model {self.model} ({code})"
            ) from exc
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise LLMOutputValidationError("OpenAI Red Team returned no parsed structured output")
        try:
            contract = LLMRedTeamContract.model_validate(parsed)
        except ValidationError as exc:
            raise LLMOutputValidationError("OpenAI Red Team failed schema validation") from exc
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
