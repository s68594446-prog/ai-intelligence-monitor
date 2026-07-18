import json
from types import SimpleNamespace

import httpx
import pytest
from openai import RateLimitError
from pydantic import ValidationError

from app.core.enums import FactType, RedTeamAction, SignalLevel, ThesisDirection
from app.core.settings import PROJECT_DIR, Settings
from app.coverage.service import load_coverage
from app.manual.collector import snapshot_to_document_input
from app.manual.contracts import ManualDocumentCreate
from app.manual.service import build_manual_harness, submit_manual_document
from app.pipeline.contracts import (
    EvidenceReference,
    ExtractedClaim,
    LLMAnalysisContract,
    LLMRedTeamContract,
)
from app.pipeline.deepseek_adapters import DeepSeekAnalysisAdapter, DeepSeekRedTeamAdapter
from app.pipeline.errors import LLMConfigurationError, LLMOutputValidationError, LLMProviderError
from app.pipeline.provider import build_live_adapters


RAW_TEXT = "Micron said HBM capacity will increase after customer qualification is complete."


class FakeCompletions:
    def __init__(
        self,
        content: str,
        finish_reason: str = "stop",
        error: Exception | None = None,
    ):
        self.content = content
        self.finish_reason = finish_reason
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self.finish_reason,
                    message=SimpleNamespace(content=self.content),
                )
            ]
        )


class FakeClient:
    def __init__(
        self,
        content: str,
        finish_reason: str = "stop",
        error: Exception | None = None,
    ):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(
                content,
                finish_reason=finish_reason,
                error=error,
            )
        )


def snapshots():
    coverage = load_coverage(PROJECT_DIR / "config" / "coverage" / "dram_hbm.yaml")
    thesis = {
        "statement": coverage["thesis"],
        "assumptions": coverage["supporting_assumptions"],
        "disconfirming_conditions": coverage["disconfirming_conditions"],
        "reverse_triggers": coverage["reverse_triggers"],
        "version": coverage["version"],
    }
    return coverage, thesis


def document():
    return snapshot_to_document_input(
        {
            "reference_id": "manual-1",
            "source_id": 1,
            "source_name": "Micron",
            "source_type": "COMPANY",
            "source_tier": "T1",
            "title": "HBM update",
            "url": "https://example.com/source",
            "published_at": "2026-07-17T00:00:00Z",
            "language": "en",
            "raw_text": RAW_TEXT,
            "content_hash": "a" * 64,
            "verification_status": "UNVERIFIED",
            "optional_notes": None,
        }
    )


def analysis_contract():
    return LLMAnalysisContract(
        coverage_match=True,
        affected_variables=["HBM capacity"],
        extracted_claims=[
            ExtractedClaim(
                statement=RAW_TEXT,
                fact_type=FactType.COMPANY_GUIDANCE,
                evidence_references=[
                    EvidenceReference(reference_id="manual-1", quote=RAW_TEXT)
                ],
            )
        ],
        novelty=70,
        marginal_change="The company described a conditional capacity increase.",
        thesis_direction=ThesisDirection.STRENGTHEN,
        affected_assumptions=["HBM continues to consume advanced DRAM capacity."],
        materiality=60,
        proposed_signal_level=SignalLevel.S2,
        unknowns=["Qualification timing is unknown."],
        next_validation_points=["Check the next formal update."],
        reasoning_summary="The source maps to HBM capacity but remains conditional.",
    )


def red_team_contract():
    return LLMRedTeamContract(
        action=RedTeamAction.NO_CHANGE,
        challenged_assumptions=["Qualification will complete on schedule."],
        alternative_explanations=["The statement may describe planned rather than effective supply."],
        confirmation_bias_risk="A supportive thesis may overweight company guidance.",
        priced_in_assessment="The statement is public.",
        non_conclusions=["It does not prove industry-wide supply growth."],
        validation_points=["Observe qualification and shipment disclosures."],
        downside_case="Qualification slips and effective supply does not increase.",
        recommended_signal_level=SignalLevel.S2,
        confidence=80,
    )


def deepseek_analysis(client):
    coverage, thesis = snapshots()
    return DeepSeekAnalysisAdapter(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        timeout_seconds=1,
        max_retries=0,
        prompt_path=PROJECT_DIR / "backend" / "app" / "prompts" / "analysis_v1.txt",
        coverage_snapshot=coverage,
        thesis_snapshot=thesis,
        client=client,
    )


def test_deepseek_analysis_uses_chat_completions_json_and_existing_contract():
    client = FakeClient(analysis_contract().model_dump_json())
    result = deepseek_analysis(client).analyze(document())
    call = client.chat.completions.calls[0]
    assert call["model"] == "deepseek-v4-pro"
    assert call["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in call["messages"][0]["content"]
    assert result.adapter_name == "DeepSeekAnalysisAdapter"
    assert result.signal_level == SignalLevel.S2
    assert result.schema_validated is True


def test_deepseek_analysis_does_not_send_local_optional_notes():
    client = FakeClient(analysis_contract().model_dump_json())
    item = document().model_copy(update={"optional_notes": "LOCAL_AUDIT_ONLY_MARKER"})
    deepseek_analysis(client).analyze(item)
    payload = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "LOCAL_AUDIT_ONLY_MARKER" not in payload
    assert "OPTIONAL_NOTES" not in payload


def test_deepseek_analysis_rejects_invalid_json():
    adapter = deepseek_analysis(FakeClient("not-json"))
    with pytest.raises(LLMOutputValidationError, match="invalid JSON"):
        adapter.analyze(document())


def test_deepseek_analysis_keeps_evidence_validation():
    contract = analysis_contract()
    contract.extracted_claims[0].evidence_references[0].quote = "not in source"
    adapter = deepseek_analysis(FakeClient(contract.model_dump_json()))
    with pytest.raises(LLMOutputValidationError, match="not present"):
        adapter.analyze(document())


def test_deepseek_empty_or_truncated_output_is_rejected():
    with pytest.raises(LLMOutputValidationError, match="empty JSON"):
        deepseek_analysis(FakeClient(" ")).analyze(document())
    with pytest.raises(LLMOutputValidationError, match="truncated"):
        deepseek_analysis(FakeClient("{}", finish_reason="length")).analyze(document())


def test_deepseek_provider_error_is_sanitized():
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(429, request=request)
    provider_error = RateLimitError(
        "sensitive provider details",
        response=response,
        body={"code": "insufficient_balance"},
    )
    adapter = deepseek_analysis(FakeClient("", error=provider_error))
    with pytest.raises(LLMProviderError, match="DeepSeek analysis request failed") as caught:
        adapter.analyze(document())
    assert "sensitive provider details" not in str(caught.value)


def test_deepseek_red_team_uses_same_pydantic_contract():
    coverage, thesis = snapshots()
    client = FakeClient(red_team_contract().model_dump_json())
    adapter = DeepSeekRedTeamAdapter(
        api_key="test-key",
        base_url="https://api.deepseek.com/",
        model="deepseek-v4-pro",
        timeout_seconds=1,
        max_retries=0,
        prompt_path=PROJECT_DIR / "backend" / "app" / "prompts" / "red_team_v1.txt",
        coverage_snapshot=coverage,
        thesis_snapshot=thesis,
        client=client,
    )
    analysis = deepseek_analysis(FakeClient(analysis_contract().model_dump_json())).analyze(document())
    result = adapter.review(document(), analysis)
    assert result.adapter_name == "DeepSeekRedTeamAdapter"
    assert result.resulting_level == SignalLevel.S2
    assert client.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_provider_factory_preserves_openai_default_and_selects_deepseek(monkeypatch):
    coverage, thesis = snapshots()
    default = Settings(_env_file=None, llm_provider="openai")
    assert default.llm_provider == "openai"

    clients = []

    def fake_openai(**kwargs):
        clients.append(kwargs)
        return FakeClient(analysis_contract().model_dump_json())

    monkeypatch.setattr("app.pipeline.deepseek_adapters.OpenAI", fake_openai)
    configured = Settings(
        _env_file=None,
        llm_provider="deepseek",
        deepseek_api_key="test-key",
        deepseek_base_url="https://api.deepseek.com/",
        deepseek_model="deepseek-v4-pro",
        deepseek_red_team_model="deepseek-v4-flash",
    )
    analysis, red_team = build_live_adapters(configured, coverage, thesis)
    assert isinstance(analysis, DeepSeekAnalysisAdapter)
    assert isinstance(red_team, DeepSeekRedTeamAdapter)
    assert analysis.model == "deepseek-v4-pro"
    assert red_team.model == "deepseek-v4-flash"
    assert all(item["base_url"] == "https://api.deepseek.com" for item in clients)


def test_manual_harness_uses_configured_deepseek_provider(
    session,
    app_settings,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.pipeline.deepseek_adapters.OpenAI",
        lambda **kwargs: FakeClient(analysis_contract().model_dump_json()),
    )
    app_settings.llm_provider = "deepseek"
    app_settings.deepseek_api_key = "test-key"
    app_settings.deepseek_model = "deepseek-v4-pro"
    app_settings.deepseek_red_team_model = "deepseek-v4-flash"
    created = submit_manual_document(
        session,
        ManualDocumentCreate(
            title="HBM update",
            url="https://example.com/source",
            source_name="Micron",
            source_tier="T1",
            published_at="2026-07-17T00:00:00Z",
            language="en",
            raw_text=RAW_TEXT,
        ),
    )
    harness, _ = build_manual_harness(session, created.document_id, app_settings)
    assert isinstance(harness.analysis, DeepSeekAnalysisAdapter)
    assert isinstance(harness.red_team, DeepSeekRedTeamAdapter)
    assert harness.model_name == "deepseek-v4-pro"
    assert harness.red_team_model_name == "deepseek-v4-flash"


def test_deepseek_missing_key_and_invalid_provider_are_rejected():
    coverage, thesis = snapshots()
    configured = Settings(_env_file=None, llm_provider="deepseek", deepseek_api_key=None)
    with pytest.raises(LLMConfigurationError, match="DEEPSEEK_API_KEY"):
        build_live_adapters(configured, coverage, thesis)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="unsupported")
