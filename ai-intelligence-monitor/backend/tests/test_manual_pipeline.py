from types import SimpleNamespace

import httpx
import pytest
from openai import RateLimitError
from pydantic import ValidationError
from sqlalchemy import select

from app.core.enums import (
    Adjudication,
    Direction,
    FactType,
    PipelineState,
    RedTeamAction,
    RunStatus,
    SignalLevel,
    ThesisDirection,
    TrustTier,
    ValueKind,
)
from app.coverage.service import load_coverage
from app.harness.service import build_harness
from app.manual.contracts import ManualDocumentCreate
from app.manual.replay import replay_manual_run
from app.manual.service import (
    analyze_manual_document,
    build_manual_harness,
    submit_manual_document,
)
from app.models import Document, Event, RunAudit, Signal
from app.pipeline.adapters import AnalysisAdapter
from app.pipeline.contracts import (
    AnalysisResult,
    EvidenceReference,
    ExtractedClaim,
    FactInput,
    LLMAnalysisContract,
    LLMRedTeamContract,
)
from app.pipeline.errors import (
    LLMAdapterTimeout,
    LLMConfigurationError,
    LLMOutputValidationError,
    LLMProviderError,
)
from app.pipeline.openai_adapters import OpenAIAnalysisAdapter, OpenAIRedTeamAdapter


T1_TEXT = (
    "Micron formally reduced its 2026 HBM production plan by 25 percent while maintaining "
    "its conventional DRAM capital discipline. The company said the change takes effect immediately."
)
T3_TEXT = (
    "An anonymous forum account claimed that a major customer cancelled HBM orders, but supplied "
    "no customer name, order volume, filing, or company confirmation."
)
OUT_OF_SCOPE_TEXT = (
    "A consumer photo application introduced new profile colors and avatar stickers. The release "
    "contains no memory, semiconductor, server, capacity, pricing, or capital expenditure information."
)


class FakeResponses:
    def __init__(self, parsed=None, error: Exception | None = None):
        self.parsed = parsed
        self.error = error
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(output_parsed=self.parsed)


class FakeClient:
    def __init__(self, parsed=None, error: Exception | None = None):
        self.responses = FakeResponses(parsed=parsed, error=error)


def manual_payload(
    *,
    raw_text: str = T1_TEXT,
    tier: str = "T1",
    title: str = "Micron HBM production update",
) -> ManualDocumentCreate:
    return ManualDocumentCreate(
        title=title,
        url="https://example.com/manual-source",
        source_name="Manual Source",
        source_tier=tier,
        published_at="2026-07-17T00:00:00Z",
        language="en",
        raw_text=raw_text,
        optional_notes="User supplied full text; URL is metadata only.",
    )


def analysis_contract(
    raw_text: str,
    *,
    level: SignalLevel = SignalLevel.S3,
    coverage_match: bool = True,
    affected_variable: str = "HBM capacity",
    thesis_direction: ThesisDirection = ThesisDirection.STRENGTHEN,
    value_kind: ValueKind = ValueKind.TEXT,
    value: str | None = None,
    fact_type: FactType = FactType.COMPANY_GUIDANCE,
) -> LLMAnalysisContract:
    quote = raw_text.split(". ")[0] + "." if ". " in raw_text else raw_text
    return LLMAnalysisContract(
        coverage_match=coverage_match,
        affected_variables=[affected_variable] if coverage_match else [],
        extracted_claims=[
            ExtractedClaim(
                statement=quote,
                fact_type=fact_type,
                value_kind=value_kind,
                value=value,
                evidence_references=[
                    EvidenceReference(reference_id="manual-1", quote=quote)
                ],
            )
        ],
        novelty=90 if coverage_match else 5,
        marginal_change="A source-backed change in the monitored variable." if coverage_match else "None.",
        thesis_direction=thesis_direction if coverage_match else ThesisDirection.NEUTRAL,
        affected_assumptions=["HBM continues to consume advanced DRAM capacity."] if coverage_match else [],
        materiality=92 if level == SignalLevel.S3 else 60 if level == SignalLevel.S2 else 0,
        proposed_signal_level=level,
        unknowns=["Follow-through remains unknown."],
        next_validation_points=["Check the next formal company update."],
        reasoning_summary="The disclosed change maps directly to the frozen DRAM/HBM thesis." if coverage_match else "No DRAM/HBM mapping.",
    )


def red_team_contract(level: SignalLevel) -> LLMRedTeamContract:
    return LLMRedTeamContract(
        action=RedTeamAction.NO_CHANGE,
        challenged_assumptions=["HBM continues to consume advanced DRAM capacity."],
        alternative_explanations=["The change may be temporary timing rather than structural supply discipline."],
        confirmation_bias_risk="A supportive thesis may overweight one disclosure.",
        priced_in_assessment="The source is public, but cross-industry implications may not be fully assessed.",
        non_conclusions=["It does not prove industry-wide supply will contract."],
        validation_points=["Observe the next company production update."],
        downside_case="The plan reverses and effective supply is unchanged.",
        recommended_signal_level=level,
        confidence=85,
    )


def adapters(app_settings, analysis_output, red_output=None, analysis_error=None):
    coverage = load_coverage(app_settings.coverage_path)
    thesis = {
        "statement": coverage["thesis"],
        "assumptions": coverage["supporting_assumptions"],
        "disconfirming_conditions": coverage["disconfirming_conditions"],
        "reverse_triggers": coverage["reverse_triggers"],
        "version": coverage["version"],
    }
    analysis_client = FakeClient(parsed=analysis_output, error=analysis_error)
    red_client = FakeClient(parsed=red_output)
    analysis = OpenAIAnalysisAdapter(
        api_key="test-key",
        model="test-analysis-model",
        timeout_seconds=1,
        max_retries=0,
        prompt_path=app_settings.analysis_prompt_path,
        coverage_snapshot=coverage,
        thesis_snapshot=thesis,
        client=analysis_client,
    )
    red_team = OpenAIRedTeamAdapter(
        api_key="test-key",
        model="test-red-team-model",
        timeout_seconds=1,
        max_retries=0,
        prompt_path=app_settings.red_team_prompt_path,
        coverage_snapshot=coverage,
        thesis_snapshot=thesis,
        client=red_client,
    )
    return analysis, red_team, analysis_client, red_client


def run_manual(session, app_settings, payload, contract, red_contract=None):
    created = submit_manual_document(session, payload)
    analysis, red_team, analysis_client, red_client = adapters(
        app_settings,
        contract,
        red_contract,
    )
    result = analyze_manual_document(
        session,
        created.document_id,
        app_settings,
        analysis_adapter=analysis,
        red_team_adapter=red_team,
    )
    return created, result, analysis_client, red_client


def test_manual_document_submission_preserves_raw_text_and_metadata(session):
    created = submit_manual_document(session, manual_payload())
    document = session.get(Document, created.document_id)
    assert created.current_status == PipelineState.COLLECTED
    assert document.raw_text == T1_TEXT
    assert document.source.trust_tier == TrustTier.T1
    assert document.url == "https://example.com/manual-source"
    assert document.optional_notes.startswith("User supplied")


def test_invalid_source_tier_is_rejected():
    with pytest.raises(ValidationError):
        manual_payload(tier="T4")


def test_missing_or_short_raw_text_is_rejected():
    with pytest.raises(ValidationError):
        manual_payload(raw_text="too short")


def test_duplicate_manual_document_returns_existing_document(session):
    first = submit_manual_document(session, manual_payload())
    second = submit_manual_document(session, manual_payload())
    assert second.duplicate is True
    assert second.document_id == first.document_id
    assert session.scalars(select(Document).where(Document.ingestion_id.is_not(None))).all().__len__() == 1


def test_out_of_scope_document_is_not_published(session, app_settings):
    contract = analysis_contract(
        OUT_OF_SCOPE_TEXT,
        level=SignalLevel.S0,
        coverage_match=False,
        fact_type=FactType.OBSERVED_FACT,
    )
    _, result, _, red_client = run_manual(
        session,
        app_settings,
        manual_payload(raw_text=OUT_OF_SCOPE_TEXT, title="Consumer application update"),
        contract,
    )
    event = session.scalar(select(Event).where(Event.run_id == result.run_id))
    assert event.pipeline_state == PipelineState.OUT_OF_SCOPE
    assert result.signal_id is None
    assert result.ops_check_status is True
    assert red_client.responses.calls == 0
    brief = next(path for path in result.artifact_paths if path.endswith("05-brief.html"))
    rendered = open(brief, encoding="utf-8").read()
    assert "OUT_OF_SCOPE: Consumer application update" in rendered
    assert "Manual Source" in rendered


def test_valid_t1_dram_hbm_s3_passes_red_team_and_ops(session, app_settings):
    contract = analysis_contract(
        T1_TEXT,
        level=SignalLevel.S3,
        value_kind=ValueKind.NUMBER,
        value="-25%",
    )
    _, result, _, red_client = run_manual(
        session,
        app_settings,
        manual_payload(),
        contract,
        red_team_contract(SignalLevel.S3),
    )
    signal = session.get(Signal, result.signal_id)
    run = session.scalar(select(RunAudit).where(RunAudit.run_id == result.run_id))
    assert signal.signal_level == SignalLevel.S3
    assert signal.adjudication == Adjudication.SEND_ALERT
    assert signal.pipeline_state == PipelineState.OPS_CHECKED
    assert red_client.responses.calls == 1
    assert run.status == RunStatus.PASSED
    assert run.llm_schema_validated is True
    assert run.ops_check["checks"] == 12


def test_t3_only_rumor_is_blocked_from_s3_and_alert(session, app_settings):
    contract = analysis_contract(
        T3_TEXT,
        level=SignalLevel.S3,
        affected_variable="HBM orders",
        thesis_direction=ThesisDirection.WEAKEN,
        fact_type=FactType.RUMOR,
    )
    _, result, _, red_client = run_manual(
        session,
        app_settings,
        manual_payload(raw_text=T3_TEXT, tier="T3", title="Anonymous HBM cancellation rumor"),
        contract,
        red_team_contract(SignalLevel.S3),
    )
    signal = session.get(Signal, result.signal_id)
    assert signal.signal_level == SignalLevel.S1
    assert signal.adjudication == Adjudication.REQUEST_HUMAN_REVIEW
    assert signal.pipeline_state == PipelineState.UNVERIFIED
    assert red_client.responses.calls == 1


def test_llm_schema_validation_failure_is_rejected(app_settings):
    analysis, _, _, _ = adapters(app_settings, {"coverage_match": True})
    from app.manual.collector import snapshot_to_document_input
    document = snapshot_to_document_input({
        "reference_id": "manual-1", "source_id": 1, "source_name": "Source",
        "source_type": "COMPANY", "source_tier": "T1", "title": "Title",
        "url": "", "published_at": "2026-07-17T00:00:00Z", "language": "en",
        "raw_text": T1_TEXT, "content_hash": "a" * 64, "verification_status": "UNVERIFIED",
        "optional_notes": None,
    })
    with pytest.raises(LLMOutputValidationError, match="schema validation"):
        analysis.analyze(document)


def test_analysis_adapter_timeout_is_clear(app_settings):
    analysis, _, _, _ = adapters(
        app_settings,
        None,
        analysis_error=TimeoutError("simulated timeout"),
    )
    from app.manual.collector import snapshot_to_document_input
    document = snapshot_to_document_input({
        "reference_id": "manual-1", "source_id": 1, "source_name": "Source",
        "source_type": "COMPANY", "source_tier": "T1", "title": "Title",
        "url": "", "published_at": "2026-07-17T00:00:00Z", "language": "en",
        "raw_text": T1_TEXT, "content_hash": "a" * 64, "verification_status": "UNVERIFIED",
        "optional_notes": None,
    })
    with pytest.raises(LLMAdapterTimeout, match="timed out"):
        analysis.analyze(document)


def test_openai_provider_error_is_sanitized(app_settings):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(429, request=request)
    provider_error = RateLimitError(
        "quota details must not leak",
        response=response,
        body={"code": "insufficient_quota"},
    )
    analysis, _, _, _ = adapters(
        app_settings,
        None,
        analysis_error=provider_error,
    )
    from app.manual.collector import snapshot_to_document_input
    document = snapshot_to_document_input({
        "reference_id": "manual-1", "source_id": 1, "source_name": "Source",
        "source_type": "COMPANY", "source_tier": "T1", "title": "Title",
        "url": "", "published_at": "2026-07-17T00:00:00Z", "language": "en",
        "raw_text": T1_TEXT, "content_hash": "a" * 64, "verification_status": "UNVERIFIED",
        "optional_notes": None,
    })
    with pytest.raises(LLMProviderError, match="OpenAI analysis request failed") as caught:
        analysis.analyze(document)
    assert "quota details must not leak" not in str(caught.value)


def test_red_team_is_required_and_called_for_s2(session, app_settings):
    contract = analysis_contract(T1_TEXT, level=SignalLevel.S2)
    _, result, _, red_client = run_manual(
        session,
        app_settings,
        manual_payload(),
        contract,
        red_team_contract(SignalLevel.S2),
    )
    signal = session.get(Signal, result.signal_id)
    assert red_client.responses.calls == 1
    assert signal.red_team_payload["research_contract"]["recommended_signal_level"] == "S2"


def test_missing_evidence_reference_is_blocked(app_settings):
    bad = analysis_contract(T1_TEXT)
    bad.extracted_claims[0].evidence_references = []
    analysis, _, _, _ = adapters(app_settings, bad)
    from app.manual.collector import snapshot_to_document_input
    document = snapshot_to_document_input({
        "reference_id": "manual-1", "source_id": 1, "source_name": "Source",
        "source_type": "COMPANY", "source_tier": "T1", "title": "Title",
        "url": "", "published_at": "2026-07-17T00:00:00Z", "language": "en",
        "raw_text": T1_TEXT, "content_hash": "a" * 64, "verification_status": "UNVERIFIED",
        "optional_notes": None,
    })
    with pytest.raises(LLMOutputValidationError, match="no evidence reference"):
        analysis.analyze(document)


class UnsourcedNumberAnalysisAdapter(AnalysisAdapter):
    name = "UnsourcedNumberAnalysisAdapter"
    version = "unsafe-test-v1"
    prompt_version = "analysis-v1"
    model = "test-model"

    def __init__(self, contract):
        self.contract = contract

    def analyze(self, document):
        return AnalysisResult(
            adapter_name=self.name,
            adapter_version=self.version,
            affected_variable="inventory",
            direction=Direction.CHALLENGES,
            novelty=80,
            materiality=70,
            source_quality=100,
            signal_level=SignalLevel.S2,
            facts=[FactInput(
                statement="Inventory rose 40 percent.",
                fact_type=FactType.COMPANY_GUIDANCE,
                value_kind=ValueKind.NUMBER,
                value="40%",
                source_refs=[],
            )],
            marginal_change="Inventory may be rising.",
            thesis_impact="Potential challenge.",
            unknowns=[],
            next_validation="Next filing.",
            adjudication=Adjudication.INCLUDE_WEEKLY,
            research_contract=self.contract,
            prompt_version=self.prompt_version,
            model_name=self.model,
        )


def test_missing_number_source_fails_manual_ops(session, app_settings):
    created = submit_manual_document(session, manual_payload())
    contract = analysis_contract(T1_TEXT, level=SignalLevel.S2)
    _, red_team, _, _ = adapters(app_settings, contract, red_team_contract(SignalLevel.S2))
    result = analyze_manual_document(
        session,
        created.document_id,
        app_settings,
        analysis_adapter=UnsourcedNumberAnalysisAdapter(contract),
        red_team_adapter=red_team,
    )
    run = session.scalar(select(RunAudit).where(RunAudit.run_id == result.run_id))
    assert run.status == RunStatus.FAILED_OPS_CHECK
    assert any("number has no source" in error for error in run.ops_check["errors"])


def test_frozen_output_replay_is_stable(session, app_settings):
    contract = analysis_contract(T1_TEXT, level=SignalLevel.S3)
    _, result, _, _ = run_manual(
        session,
        app_settings,
        manual_payload(),
        contract,
        red_team_contract(SignalLevel.S3),
    )
    replay, diff = replay_manual_run(
        session,
        result.run_id,
        mode="frozen-output",
        app_settings=app_settings,
    )
    assert replay.replay_of == result.run_id
    assert replay.replay_mode == "FROZEN_OUTPUT"
    assert diff["stable"] is True


def test_live_model_replay_reports_structured_diff(session, app_settings):
    original_contract = analysis_contract(T1_TEXT, level=SignalLevel.S3)
    _, result, _, _ = run_manual(
        session,
        app_settings,
        manual_payload(),
        original_contract,
        red_team_contract(SignalLevel.S3),
    )
    changed = analysis_contract(T1_TEXT, level=SignalLevel.S2)
    analysis, red_team, _, _ = adapters(
        app_settings,
        changed,
        red_team_contract(SignalLevel.S2),
    )
    replay, diff = replay_manual_run(
        session,
        result.run_id,
        mode="live-model",
        app_settings=app_settings,
        analysis_adapter=analysis,
        red_team_adapter=red_team,
    )
    assert replay.replay_mode == "LIVE_MODEL"
    assert diff["mode"] == "live-model"
    assert diff["stable"] is False
    assert "signal_levels" in diff["comparison"]


def test_missing_api_key_returns_configuration_error(session, app_settings):
    created = submit_manual_document(session, manual_payload())
    assert app_settings.openai_api_key is None
    with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
        build_manual_harness(session, created.document_id, app_settings)


def test_existing_fake_adapter_path_remains_functional(harness):
    run = harness.run("verified_s3")
    assert run.status == RunStatus.PASSED
    assert run.run_kind == "fixture"


def test_manual_api_submission_analysis_and_reads(session, app_settings, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.core.database import get_session
    from app.main import app

    contract = analysis_contract(T1_TEXT, level=SignalLevel.S3)
    analysis, red_team, _, _ = adapters(
        app_settings,
        contract,
        red_team_contract(SignalLevel.S3),
    )
    monkeypatch.setattr(
        routes,
        "analyze_manual_document",
        lambda db, document_id: analyze_manual_document(
            db,
            document_id,
            app_settings,
            analysis_adapter=analysis,
            red_team_adapter=red_team,
        ),
    )

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            submitted = client.post(
                "/api/manual-documents",
                json=manual_payload().model_dump(mode="json"),
            )
            assert submitted.status_code == 200
            document_id = submitted.json()["document_id"]
            analyzed = client.post(f"/api/manual-documents/{document_id}/analyze")
            assert analyzed.status_code == 200
            run_id = analyzed.json()["run_id"]
            assert analyzed.json()["ops_check_status"] is True
            assert client.get(f"/api/runs/{run_id}").status_code == 200
            signals = client.get("/api/signals")
            assert signals.status_code == 200
            assert any(item["run_id"] == run_id for item in signals.json())
    finally:
        app.dependency_overrides.clear()
