from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import smtplib
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    Adjudication,
    DeliveryStatus,
    FactType,
    PipelineState,
    RedTeamAction,
    RunStatus,
    SignalLevel,
    ThesisDirection,
    ValueKind,
)
from app.core.database import build_engine
from app.core.settings import Settings
from app.coverage.service import load_coverage
from app.delivery.contracts import (
    EmailDeliveryOutcome,
    EmailDeliveryRequest,
    EmailDeliveryResult,
    EmailProviderResult,
)
from app.delivery import service as delivery_service
from app.delivery.coordinator import coordinate_post_ops_email_delivery
from app.delivery.email_adapter import (
    EmailProviderError,
    SMTPEmailDeliveryAdapter,
    resolve_smtp_configuration,
)
from app.delivery.service import EMAIL_CHANNEL, MAX_BRIEF_BYTES, deliver_run_email
from app.harness.service import replay_run
from app.manual.contracts import ManualDocumentCreate
from app.manual.service import analyze_manual_document, submit_manual_document
from app.models import DeliveryFeedback, Event, RunAudit, Signal
from app.pipeline.contracts import (
    EvidenceReference,
    ExtractedClaim,
    LLMAnalysisContract,
    LLMRedTeamContract,
)
from app.pipeline.openai_adapters import OpenAIAnalysisAdapter, OpenAIRedTeamAdapter
from app.scripts.deliver_email import result_exit_code


class FakeEmailAdapter:
    def __init__(self, error: Exception | None = None, message_id: str | None = None):
        self.error = error
        self.message_id = message_id
        self.calls: list[EmailDeliveryRequest] = []

    def send(self, request: EmailDeliveryRequest) -> EmailProviderResult:
        self.calls.append(request)
        if self.error:
            raise self.error
        return EmailProviderResult(provider_message_id=self.message_id or request.message_id)


class FakeStructuredResponses:
    def __init__(self, parsed):
        self.parsed = parsed

    def parse(self, **_kwargs):
        return SimpleNamespace(output_parsed=self.parsed)


class FakeStructuredClient:
    def __init__(self, parsed):
        self.responses = FakeStructuredResponses(parsed)


MANUAL_ALERT_TEXT = (
    "Micron formally reduced its 2026 HBM production plan by 25 percent while maintaining "
    "its conventional DRAM capital discipline. The change takes effect immediately."
)


def live_test_adapters(app_settings, reference_id: str):
    quote = (
        "Micron formally reduced its 2026 HBM production plan by 25 percent while maintaining "
        "its conventional DRAM capital discipline."
    )
    analysis_contract = LLMAnalysisContract(
        coverage_match=True,
        affected_variables=["cloud provider CapEx"],
        extracted_claims=[
            ExtractedClaim(
                statement=quote,
                fact_type=FactType.COMPANY_GUIDANCE,
                value_kind=ValueKind.NUMBER,
                value="-25%",
                evidence_references=[EvidenceReference(reference_id=reference_id, quote=quote)],
            )
        ],
        novelty=95,
        marginal_change="A formal reduction in the monitored demand sentinel.",
        thesis_direction=ThesisDirection.WEAKEN,
        affected_assumptions=["Cloud provider CapEx continues to grow."],
        materiality=95,
        proposed_signal_level=SignalLevel.S3,
        unknowns=["Whether other customers will follow remains unknown."],
        next_validation_points=["Check the next formal company update."],
        reasoning_summary="The formal change directly affects a monitored demand variable.",
    )
    red_team_contract = LLMRedTeamContract(
        action=RedTeamAction.NO_CHANGE,
        challenged_assumptions=["Cloud provider CapEx continues to grow."],
        alternative_explanations=["The change may reflect timing rather than structural demand."],
        confirmation_bias_risk="One disclosure may be overweighted.",
        priced_in_assessment="The disclosure is public but its read-through may be incomplete.",
        non_conclusions=["It does not prove aggregate HBM demand is contracting."],
        validation_points=["Observe the next production update."],
        downside_case="The plan reverses and effective supply is unchanged.",
        recommended_signal_level=SignalLevel.S3,
        confidence=85,
    )
    coverage = load_coverage(app_settings.coverage_path)
    thesis = {
        "statement": coverage["thesis"],
        "assumptions": coverage["supporting_assumptions"],
        "disconfirming_conditions": coverage["disconfirming_conditions"],
        "reverse_triggers": coverage["reverse_triggers"],
        "version": coverage["version"],
    }
    analysis = OpenAIAnalysisAdapter(
        api_key="offline-test-key",
        model="gpt-5.6-sol",
        timeout_seconds=1,
        max_retries=0,
        prompt_path=app_settings.analysis_prompt_path,
        coverage_snapshot=coverage,
        thesis_snapshot=thesis,
        client=FakeStructuredClient(analysis_contract),
    )
    red_team = OpenAIRedTeamAdapter(
        api_key="offline-test-key",
        model="gpt-5.6-sol",
        timeout_seconds=1,
        max_retries=0,
        prompt_path=app_settings.red_team_prompt_path,
        coverage_snapshot=coverage,
        thesis_snapshot=thesis,
        client=FakeStructuredClient(red_team_contract),
    )
    return analysis, red_team


def enabled_settings(app_settings, **updates):
    values = {
        "email_delivery_enabled": True,
        "email_auto_send": False,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "mailer@example.com",
        "smtp_password": SecretStr("smtp-test-password"),
        "smtp_from_address": "alerts@example.com",
        "smtp_to_addresses": "second@example.com,first@example.com",
        "smtp_security": "STARTTLS",
        "smtp_timeout_seconds": 5.0,
        "smtp_message_id_domain": "example.com",
    }
    values.update(updates)
    return app_settings.model_copy(update=values)


def valid_email_run(session, app_settings):
    created = submit_manual_document(
        session,
        ManualDocumentCreate(
            title="Micron HBM production update",
            url="https://example.com/manual-source",
            source_name="Manual T1 Source",
            source_tier="T1",
            published_at="2026-07-18T00:00:00Z",
            language="en",
            raw_text=MANUAL_ALERT_TEXT,
        ),
    )
    analysis, red_team = live_test_adapters(app_settings, f"manual-{created.document_id}")
    response = analyze_manual_document(
        session,
        created.document_id,
        app_settings,
        analysis_adapter=analysis,
        red_team_adapter=red_team,
    )
    run = session.scalar(select(RunAudit).where(RunAudit.run_id == response.run_id))
    signal = session.scalar(select(Signal).where(Signal.run_id == run.run_id))
    brief = Path(run.artifact_paths[-1])
    return run, signal, brief


def email_attempt(session, run_id):
    return session.scalar(
        select(DeliveryFeedback).where(
            DeliveryFeedback.run_id == run_id,
            DeliveryFeedback.delivery_channel == EMAIL_CHANNEL,
        )
    )


def test_email_delivery_is_disabled_by_default(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    adapter = FakeEmailAdapter()
    result = deliver_run_email(session, run.run_id, adapter, app_settings=app_settings)
    assert result.status == EmailDeliveryOutcome.BLOCKED
    assert "EMAIL_DELIVERY_DISABLED" in result.reasons
    assert adapter.calls == []


def test_incomplete_smtp_configuration_fails_closed(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    configured = app_settings.model_copy(update={"email_delivery_enabled": True})
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=configured)
    assert result.status == EmailDeliveryOutcome.BLOCKED
    assert "SMTP_PASSWORD_MISSING" in result.reasons


def test_ops_check_failure_blocks_email(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    run.ops_check = {"passed": False, "errors": ["test"]}
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "OPS_CHECK_NOT_PASSED" in result.reasons


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.FAILED, RunStatus.FAILED_OPS_CHECK])
def test_nonpassed_run_blocks_email(harness, session, app_settings, status):
    run, _, _ = valid_email_run(session, app_settings)
    run.status = status
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "RUN_NOT_PASSED" in result.reasons


def test_run_without_send_alert_is_blocked(harness, session, app_settings):
    run, signal, _ = valid_email_run(session, app_settings)
    run.alerts_selected = 0
    session.delete(signal)
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "NO_SEND_ALERT_SIGNAL" in result.reasons


@pytest.mark.parametrize("level", [SignalLevel.S0, SignalLevel.S1])
def test_s0_and_s1_are_blocked(harness, session, app_settings, level):
    run, signal, _ = valid_email_run(session, app_settings)
    signal.signal_level = level
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "LOW_SIGNAL_LEVEL_BLOCKED" in result.reasons


@pytest.mark.parametrize(
    "adjudication",
    [
        Adjudication.DROP,
        Adjudication.ARCHIVE,
        Adjudication.INCLUDE_WEEKLY,
        Adjudication.REQUEST_HUMAN_REVIEW,
    ],
)
def test_nonalert_adjudications_are_blocked(harness, session, app_settings, adjudication):
    run, signal, _ = valid_email_run(session, app_settings)
    signal.adjudication = adjudication
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "NON_ALERT_ADJUDICATION_PRESENT" in result.reasons


def test_replay_run_is_blocked(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    run.replay_of = "original-run"
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "REPLAY_RUN_BLOCKED" in result.reasons


def test_fixture_run_is_blocked(harness, session, app_settings):
    run = harness.run("verified_s3")
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "RUN_KIND_NOT_DELIVERABLE" in result.reasons


def test_brief_missing_is_blocked(harness, session, app_settings):
    run, _, brief = valid_email_run(session, app_settings)
    brief.unlink()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "BRIEF_MISSING" in result.reasons


def test_brief_symlink_escape_is_blocked(harness, session, app_settings):
    run, _, brief = valid_email_run(session, app_settings)
    outside = app_settings.runs_dir.parent / "outside-brief.html"
    outside.write_text("<!-- ops-check-status:PASSED -->", encoding="utf-8")
    brief.unlink()
    brief.symlink_to(outside)
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "BRIEF_PATH_INVALID" in result.reasons


def test_brief_symlink_inside_run_is_blocked(harness, session, app_settings):
    run, _, brief = valid_email_run(session, app_settings)
    same_directory_target = brief.with_name("copied-brief.html")
    same_directory_target.write_bytes(brief.read_bytes())
    brief.unlink()
    brief.symlink_to(same_directory_target.name)
    result = deliver_run_email(
        session,
        run.run_id,
        FakeEmailAdapter(),
        app_settings=enabled_settings(app_settings),
    )
    assert "BRIEF_PATH_INVALID" in result.reasons


def test_oversized_brief_is_blocked_before_adapter(harness, session, app_settings):
    run, _, brief = valid_email_run(session, app_settings)
    brief.write_bytes(b"<!-- ops-check-status:PASSED -->" + b"x" * MAX_BRIEF_BYTES)
    adapter = FakeEmailAdapter()
    result = deliver_run_email(
        session,
        run.run_id,
        adapter,
        app_settings=enabled_settings(app_settings),
    )
    assert "BRIEF_TOO_LARGE" in result.reasons
    assert adapter.calls == []


def test_brief_ops_marker_must_be_passed(harness, session, app_settings):
    run, _, brief = valid_email_run(session, app_settings)
    brief.write_text(brief.read_text(encoding="utf-8").replace("PASSED", "FAILED"), encoding="utf-8")
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "BRIEF_OPS_STATUS_NOT_PASSED" in result.reasons


def test_valid_send_alert_sends_exactly_once(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    adapter = FakeEmailAdapter()
    result = deliver_run_email(session, run.run_id, adapter, app_settings=enabled_settings(app_settings))
    assert result.status == EmailDeliveryOutcome.SENT
    assert len(adapter.calls) == 1
    attempt = email_attempt(session, run.run_id)
    assert attempt.delivery_status == DeliveryStatus.EMAIL_SENT
    assert attempt.attempted_at is not None and attempt.sent_at is not None


def test_html_brief_and_plain_fallback_are_preserved(harness, session, app_settings):
    run, _, brief = valid_email_run(session, app_settings)
    original = brief.read_text(encoding="utf-8")
    adapter = FakeEmailAdapter()
    deliver_run_email(session, run.run_id, adapter, app_settings=enabled_settings(app_settings))
    request = adapter.calls[0]
    assert request.html_body == original
    assert "AI Intelligence Monitor alert" in request.text_body
    assert run.run_id in request.text_body


def test_repeat_call_returns_already_sent_without_adapter_call(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    configured = enabled_settings(app_settings)
    first = FakeEmailAdapter()
    deliver_run_email(session, run.run_id, first, app_settings=configured)
    second = FakeEmailAdapter()
    result = deliver_run_email(session, run.run_id, second, app_settings=configured)
    assert result.status == EmailDeliveryOutcome.ALREADY_SENT
    assert second.calls == []


def test_idempotency_key_has_database_unique_constraint(harness, session, app_settings):
    run, _, brief = valid_email_run(session, app_settings)
    result = deliver_run_email(
        session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings)
    )
    duplicate = DeliveryFeedback(
        signal_id=None,
        run_id="different-run",
        delivery_status=DeliveryStatus.EMAIL_PENDING,
        rendered_path=str(brief),
        delivery_channel=EMAIL_CHANNEL,
        idempotency_key=result.idempotency_key,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_run_channel_has_database_unique_constraint(harness, session, app_settings):
    run, _, brief = valid_email_run(session, app_settings)
    first = DeliveryFeedback(
        signal_id=None,
        run_id=run.run_id,
        delivery_status=DeliveryStatus.EMAIL_PENDING,
        rendered_path=str(brief),
        delivery_channel=EMAIL_CHANNEL,
        idempotency_key="a" * 64,
    )
    second = DeliveryFeedback(
        signal_id=None,
        run_id=run.run_id,
        delivery_status=DeliveryStatus.EMAIL_PENDING,
        rendered_path=str(brief),
        delivery_channel=EMAIL_CHANNEL,
        idempotency_key="b" * 64,
    )
    session.add_all([first, second])
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_adapter_failure_is_sanitized_and_recorded(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    secret = "smtp-test-password"
    adapter = FakeEmailAdapter(EmailProviderError(f"provider exposed {secret}"))
    result = deliver_run_email(session, run.run_id, adapter, app_settings=enabled_settings(app_settings))
    attempt = email_attempt(session, run.run_id)
    assert result.status == EmailDeliveryOutcome.FAILED
    assert attempt.error_code == "EMAIL_PROVIDER_ERROR"
    assert secret not in (attempt.feedback or "")
    assert run.status == RunStatus.PASSED


def test_unknown_delivery_state_fails_closed(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    configured = enabled_settings(app_settings)
    adapter = FakeEmailAdapter(
        EmailProviderError("SMTP_DELIVERY_UNKNOWN", outcome_unknown=True)
    )
    first = deliver_run_email(session, run.run_id, adapter, app_settings=configured)
    second_adapter = FakeEmailAdapter()
    second = deliver_run_email(session, run.run_id, second_adapter, app_settings=configured)
    assert first.status == EmailDeliveryOutcome.UNKNOWN
    assert second.status == EmailDeliveryOutcome.UNKNOWN
    assert second_adapter.calls == []


def test_known_failed_attempt_is_not_automatically_retried(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    configured = enabled_settings(app_settings)
    deliver_run_email(
        session,
        run.run_id,
        FakeEmailAdapter(EmailProviderError("SMTP_PROVIDER_ERROR")),
        app_settings=configured,
    )
    adapter = FakeEmailAdapter()
    result = deliver_run_email(session, run.run_id, adapter, app_settings=configured)
    assert result.status == EmailDeliveryOutcome.FAILED
    assert adapter.calls == []


@pytest.mark.parametrize(
    "updates",
    [
        {"smtp_to_addresses": "safe@example.com\nBcc:evil@example.com"},
        {"smtp_from_address": "alerts@example.com\r\nCc:evil@example.com"},
    ],
)
def test_recipient_and_header_injection_are_rejected(harness, session, app_settings, updates):
    run, _, _ = valid_email_run(session, app_settings)
    adapter = FakeEmailAdapter()
    result = deliver_run_email(
        session,
        run.run_id,
        adapter,
        app_settings=enabled_settings(app_settings, **updates),
    )
    assert result.status == EmailDeliveryOutcome.BLOCKED
    assert adapter.calls == []


def test_password_is_not_persisted_or_added_to_message(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    password = "smtp-test-password"
    adapter = FakeEmailAdapter()
    deliver_run_email(session, run.run_id, adapter, app_settings=enabled_settings(app_settings))
    attempt = email_attempt(session, run.run_id)
    stored = " ".join(
        str(value or "")
        for value in (
            attempt.feedback,
            attempt.error_code,
            attempt.provider_message_id,
            attempt.recipient_fingerprint,
        )
    )
    assert password not in stored
    assert password not in adapter.calls[0].model_dump_json()


def test_auto_send_disabled_does_not_call_adapter(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    adapter = FakeEmailAdapter()
    result = coordinate_post_ops_email_delivery(
        session,
        run.run_id,
        app_settings=enabled_settings(app_settings, email_auto_send=False),
        adapter=adapter,
    )
    assert result is None
    assert adapter.calls == []


def test_auto_send_enabled_calls_adapter_once(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    adapter = FakeEmailAdapter()
    result = coordinate_post_ops_email_delivery(
        session,
        run.run_id,
        app_settings=enabled_settings(app_settings, email_auto_send=True),
        adapter=adapter,
    )
    assert result.status == EmailDeliveryOutcome.SENT
    assert len(adapter.calls) == 1


def test_email_failure_does_not_change_research_run(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    result = coordinate_post_ops_email_delivery(
        session,
        run.run_id,
        app_settings=enabled_settings(app_settings, email_auto_send=True),
        adapter=FakeEmailAdapter(EmailProviderError("SMTP_PROVIDER_ERROR")),
    )
    session.refresh(run)
    assert result.status == EmailDeliveryOutcome.FAILED
    assert run.status == RunStatus.PASSED
    assert run.ops_check["passed"] is True


def test_existing_local_rendered_and_ops_behavior_remains(harness, session, app_settings):
    run, signal, _ = valid_email_run(session, app_settings)
    local = session.scalar(
        select(DeliveryFeedback).where(
            DeliveryFeedback.run_id == run.run_id,
            DeliveryFeedback.delivery_status == DeliveryStatus.LOCAL_RENDERED,
        )
    )
    assert local is not None and local.delivery_channel is None
    assert signal.pipeline_state == PipelineState.OPS_CHECKED
    assert run.ops_check["passed"] is True


def test_red_team_and_ops_checked_state_are_mandatory(harness, session, app_settings):
    run, signal, _ = valid_email_run(session, app_settings)
    signal.red_team_payload = None
    signal.pipeline_state = PipelineState.RENDERED
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "RED_TEAM_REQUIRED" in result.reasons
    assert "SIGNAL_NOT_OPS_CHECKED" in result.reasons


def test_nonempty_but_invalid_red_team_payload_is_blocked(harness, session, app_settings):
    run, signal, _ = valid_email_run(session, app_settings)
    signal.red_team_payload = {"action": "NO_CHANGE"}
    session.commit()
    result = deliver_run_email(
        session,
        run.run_id,
        FakeEmailAdapter(),
        app_settings=enabled_settings(app_settings),
    )
    assert "RED_TEAM_INVALID" in result.reasons


def test_tampered_analysis_evidence_quote_is_blocked(harness, session, app_settings):
    run, signal, _ = valid_email_run(session, app_settings)
    payload = copy.deepcopy(signal.analysis_payload)
    payload["research_contract"]["extracted_claims"][0]["evidence_references"][0][
        "quote"
    ] = "This sentence does not exist in the source document."
    signal.analysis_payload = payload
    session.commit()
    result = deliver_run_email(
        session,
        run.run_id,
        FakeEmailAdapter(),
        app_settings=enabled_settings(app_settings),
    )
    assert "SIGNAL_EVIDENCE_INVALID" in result.reasons


def test_alert_count_must_match_actual_send_alert_signals(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    run.alerts_selected = 2
    session.commit()
    result = deliver_run_email(
        session,
        run.run_id,
        FakeEmailAdapter(),
        app_settings=enabled_settings(app_settings),
    )
    assert "ALERT_COUNT_MISMATCH" in result.reasons


def test_fixture_cannot_be_promoted_by_mutating_run_kind(harness, session, app_settings):
    run = harness.run("verified_s3")
    run.run_kind = "manual"
    run.replay_mode = "LIVE_MODEL"
    run.llm_schema_validated = True
    session.commit()
    adapter = FakeEmailAdapter()
    result = deliver_run_email(
        session,
        run.run_id,
        adapter,
        app_settings=enabled_settings(app_settings),
    )
    assert "NON_LIVE_ANALYSIS_ADAPTER" in result.reasons
    assert "MANUAL_RUN_PROVENANCE_INVALID" in result.reasons
    assert adapter.calls == []


def test_test_model_audit_is_blocked(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    run.model_name = "test-analysis-model"
    run.red_team_model_name = "test-red-team-model"
    session.commit()
    adapter = FakeEmailAdapter()
    result = deliver_run_email(
        session,
        run.run_id,
        adapter,
        app_settings=enabled_settings(app_settings),
    )
    assert "TEST_MODEL_BLOCKED" in result.reasons
    assert adapter.calls == []


def test_non_live_replay_mode_is_blocked_even_without_replay_pointer(
    harness, session, app_settings
):
    run, _, _ = valid_email_run(session, app_settings)
    run.replay_mode = "FROZEN_OUTPUT"
    run.replay_of = None
    session.commit()
    result = deliver_run_email(
        session,
        run.run_id,
        FakeEmailAdapter(),
        app_settings=enabled_settings(app_settings),
    )
    assert "REPLAY_MODE_NOT_DELIVERABLE" in result.reasons


def test_blocked_event_state_fails_closed(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    event = session.scalar(select(Event).where(Event.run_id == run.run_id))
    event.pipeline_state = PipelineState.CONFLICTED
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "BLOCKED_EVENT_STATE_PRESENT" in result.reasons


def test_brief_must_be_recorded_in_run_artifacts(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    run.artifact_paths = [path for path in run.artifact_paths if not path.endswith("05-brief.html")]
    session.commit()
    result = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=enabled_settings(app_settings))
    assert "BRIEF_NOT_AUDITED" in result.reasons


def test_frozen_replay_never_auto_sends(harness, session, app_settings):
    original = harness.run("verified_s3")
    configured = enabled_settings(app_settings, email_auto_send=True)
    replayed, diff = replay_run(session, original.run_id, configured)
    attempts = session.scalars(
        select(DeliveryFeedback).where(
            DeliveryFeedback.run_id == replayed.run_id,
            DeliveryFeedback.delivery_channel == EMAIL_CHANNEL,
        )
    ).all()
    assert diff["stable"] is True
    assert replayed.replay_of == original.run_id
    assert attempts == []


def test_plaintext_smtp_security_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, smtp_security="NONE")


def test_message_id_and_recipient_fingerprint_are_deterministic(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    configured = enabled_settings(app_settings)
    first = deliver_run_email(session, run.run_id, None, app_settings=configured, dry_run=True)
    second = deliver_run_email(session, run.run_id, None, app_settings=configured, dry_run=True)
    assert first.status == EmailDeliveryOutcome.READY
    assert first.idempotency_key == second.idempotency_key
    assert first.recipient_count == 2


def test_recipient_order_does_not_change_idempotency_key(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    first = deliver_run_email(
        session,
        run.run_id,
        None,
        app_settings=enabled_settings(
            app_settings,
            smtp_to_addresses="first@example.com,second@example.com",
        ),
        dry_run=True,
    )
    second = deliver_run_email(
        session,
        run.run_id,
        None,
        app_settings=enabled_settings(
            app_settings,
            smtp_to_addresses="second@example.com,first@example.com",
        ),
        dry_run=True,
    )
    assert first.idempotency_key == second.idempotency_key


def test_brief_change_changes_key_but_cannot_bypass_run_channel_lock(
    harness, session, app_settings
):
    run, _, brief = valid_email_run(session, app_settings)
    configured = enabled_settings(app_settings)
    before = deliver_run_email(session, run.run_id, None, app_settings=configured, dry_run=True)
    original = brief.read_text(encoding="utf-8")
    brief.write_text(original.replace("</body>", "<!-- reviewed -->\n</body>"), encoding="utf-8")
    after = deliver_run_email(session, run.run_id, None, app_settings=configured, dry_run=True)
    assert before.idempotency_key != after.idempotency_key
    sent = deliver_run_email(session, run.run_id, FakeEmailAdapter(), app_settings=configured)
    brief.write_text(original, encoding="utf-8")
    repeat_adapter = FakeEmailAdapter()
    repeat = deliver_run_email(session, run.run_id, repeat_adapter, app_settings=configured)
    assert sent.status == EmailDeliveryOutcome.SENT
    assert repeat.status == EmailDeliveryOutcome.ALREADY_SENT
    assert repeat_adapter.calls == []


def test_dry_run_does_not_read_password_or_write_attempt(
    harness, session, app_settings, monkeypatch
):
    run, _, _ = valid_email_run(session, app_settings)
    configured = enabled_settings(app_settings)

    def fail_if_read(_self):
        raise AssertionError("dry-run read SMTP password")

    monkeypatch.setattr(SecretStr, "get_secret_value", fail_if_read)
    result = deliver_run_email(
        session,
        run.run_id,
        None,
        app_settings=configured,
        dry_run=True,
    )
    assert result.status == EmailDeliveryOutcome.READY
    assert email_attempt(session, run.run_id) is None


def test_smtp_configuration_repr_masks_password(app_settings):
    password = "smtp-test-password"
    configuration, errors = resolve_smtp_configuration(enabled_settings(app_settings))
    assert errors == []
    assert password not in repr(configuration)


def test_cli_exit_codes_distinguish_ready_blocked_and_failure():
    assert result_exit_code(
        EmailDeliveryResult(run_id="run", status=EmailDeliveryOutcome.READY)
    ) == 0
    assert result_exit_code(
        EmailDeliveryResult(run_id="run", status=EmailDeliveryOutcome.BLOCKED)
    ) == 2
    assert result_exit_code(
        EmailDeliveryResult(run_id="run", status=EmailDeliveryOutcome.UNKNOWN)
    ) == 1


def test_blocked_gate_reports_dry_run_without_adapter(session, app_settings):
    result = deliver_run_email(
        session,
        "missing-run",
        None,
        app_settings=app_settings,
        dry_run=True,
    )
    assert result.status == EmailDeliveryOutcome.BLOCKED
    assert result.dry_run is True


def test_pending_attempt_requires_manual_review_without_retry(harness, session, app_settings):
    run, signal, brief = valid_email_run(session, app_settings)
    pending = DeliveryFeedback(
        signal_id=signal.id,
        run_id=run.run_id,
        delivery_status=DeliveryStatus.EMAIL_PENDING,
        rendered_path=str(brief),
        delivery_channel=EMAIL_CHANNEL,
        idempotency_key="p" * 64,
    )
    session.add(pending)
    session.commit()
    adapter = FakeEmailAdapter()
    result = deliver_run_email(session, run.run_id, adapter, app_settings=enabled_settings(app_settings))
    assert result.status == EmailDeliveryOutcome.UNKNOWN
    assert adapter.calls == []


def test_provider_message_id_mismatch_becomes_unknown(harness, session, app_settings):
    run, _, _ = valid_email_run(session, app_settings)
    result = deliver_run_email(
        session,
        run.run_id,
        FakeEmailAdapter(message_id="<different@example.com>"),
        app_settings=enabled_settings(app_settings),
    )
    assert result.status == EmailDeliveryOutcome.UNKNOWN
    assert email_attempt(session, run.run_id).delivery_status == DeliveryStatus.EMAIL_UNKNOWN


def test_smtp_message_contains_html_and_plain_alternatives():
    request = EmailDeliveryRequest(
        run_id="run-1",
        from_address="alerts@example.com",
        to_addresses=("recipient@example.com",),
        subject="Alert",
        html_body="<html><body>Exact brief</body></html>",
        text_body="Plain fallback",
        message_id="<aim-test@example.com>",
    )
    message = SMTPEmailDeliveryAdapter.build_message(request)
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "Plain fallback"
    assert "Exact brief" in message.get_body(preferencelist=("html",)).get_content()
    assert message["To"] == "undisclosed-recipients:;"
    assert "recipient@example.com" not in str(message)


def test_partial_recipient_acceptance_is_unknown_and_envelope_is_private(
    harness, session, app_settings, monkeypatch
):
    run, _, _ = valid_email_run(session, app_settings)
    observed = {}

    class PartialSMTP:
        def __init__(self, host, port, timeout):
            observed.update(host=host, port=port, timeout=timeout)

        def ehlo(self):
            return None

        def starttls(self, context):
            observed["tls_context"] = context

        def login(self, username, password):
            observed["logged_in"] = bool(username and password)

        def send_message(self, message, *, from_addr, to_addrs):
            observed["to_header"] = message["To"]
            observed["envelope"] = tuple(to_addrs)
            observed["from_addr"] = from_addr
            return {to_addrs[-1]: (550, b"rejected")}

        def quit(self):
            return None

    monkeypatch.setattr("app.delivery.email_adapter.smtplib.SMTP", PartialSMTP)
    configured = enabled_settings(app_settings)
    result = deliver_run_email(
        session,
        run.run_id,
        SMTPEmailDeliveryAdapter(configured),
        app_settings=configured,
    )
    assert result.status == EmailDeliveryOutcome.UNKNOWN
    assert result.reasons == ["SMTP_PARTIAL_RECIPIENT_ACCEPTANCE"]
    assert observed["to_header"] == "undisclosed-recipients:;"
    assert observed["envelope"] == ("first@example.com", "second@example.com")
    assert email_attempt(session, run.run_id).delivery_status == DeliveryStatus.EMAIL_UNKNOWN


def test_starttls_failure_never_downgrades_to_plaintext(app_settings, monkeypatch):
    observed = {"connections": 0, "login": False, "send": False}

    class FailingStartTLS:
        def __init__(self, host, port, timeout):
            observed["connections"] += 1

        def ehlo(self):
            return None

        def starttls(self, context):
            raise smtplib.SMTPException("TLS unavailable")

        def login(self, username, password):
            observed["login"] = True

        def send_message(self, message, *, from_addr, to_addrs):
            observed["send"] = True
            return {}

        def quit(self):
            return None

    monkeypatch.setattr("app.delivery.email_adapter.smtplib.SMTP", FailingStartTLS)
    configured = enabled_settings(app_settings)
    adapter = SMTPEmailDeliveryAdapter(configured)
    request = EmailDeliveryRequest(
        run_id="run-1",
        from_address="alerts@example.com",
        to_addresses=("first@example.com", "second@example.com"),
        subject="Alert",
        html_body="<html><body>Brief</body></html>",
        text_body="Plain fallback",
        message_id="<aim-test@example.com>",
    )
    with pytest.raises(EmailProviderError) as error:
        adapter.send(request)
    assert error.value.code == "SMTP_NETWORK_ERROR"
    assert error.value.outcome_unknown is False
    assert observed == {"connections": 1, "login": False, "send": False}


def test_incomplete_configuration_never_constructs_smtp_socket(app_settings, monkeypatch):
    def fail_if_connected(*_args, **_kwargs):
        raise AssertionError("SMTP socket was constructed")

    monkeypatch.setattr("app.delivery.email_adapter.smtplib.SMTP", fail_if_connected)
    adapter = SMTPEmailDeliveryAdapter(
        app_settings.model_copy(update={"email_delivery_enabled": True})
    )
    request = EmailDeliveryRequest(
        run_id="run-1",
        from_address="alerts@example.com",
        to_addresses=("recipient@example.com",),
        subject="Alert",
        html_body="<html><body>Brief</body></html>",
        text_body="Plain fallback",
        message_id="<aim-test@example.com>",
    )
    with pytest.raises(EmailProviderError) as error:
        adapter.send(request)
    assert error.value.code == "SMTP_HOST_INVALID"


def test_concurrent_delivery_allows_only_one_adapter_call(
    session, app_settings, monkeypatch
):
    run, _, _ = valid_email_run(session, app_settings)
    configured = enabled_settings(app_settings)
    barrier = Barrier(2)
    original_gate = delivery_service.evaluate_email_delivery_gate

    def synchronized_gate(*args, **kwargs):
        decision = original_gate(*args, **kwargs)
        barrier.wait(timeout=5)
        return decision

    monkeypatch.setattr(delivery_service, "evaluate_email_delivery_gate", synchronized_gate)

    class LockedAdapter(FakeEmailAdapter):
        def __init__(self):
            super().__init__()
            self.lock = Lock()

        def send(self, request):
            with self.lock:
                return super().send(request)

    adapter = LockedAdapter()
    engine = build_engine(app_settings.database_url)
    maker = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def worker():
        with maker() as worker_session:
            return deliver_run_email(
                worker_session,
                run.run_id,
                adapter,
                app_settings=configured,
            ).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _index: worker(), range(2)))
    engine.dispose()
    assert len(adapter.calls) == 1
    assert EmailDeliveryOutcome.SENT in statuses
    assert set(statuses) <= {
        EmailDeliveryOutcome.SENT,
        EmailDeliveryOutcome.ALREADY_SENT,
        EmailDeliveryOutcome.UNKNOWN,
    }


def test_coordinator_isolates_unexpected_delivery_exception(
    session, app_settings, monkeypatch
):
    run, _, _ = valid_email_run(session, app_settings)

    def fail_delivery(*_args, **_kwargs):
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr("app.delivery.coordinator.deliver_run_email", fail_delivery)
    result = coordinate_post_ops_email_delivery(
        session,
        run.run_id,
        app_settings=enabled_settings(app_settings, email_auto_send=True),
        adapter=FakeEmailAdapter(),
    )
    session.refresh(run)
    assert result.status == EmailDeliveryOutcome.UNKNOWN
    assert result.reasons == ["POST_OPS_DELIVERY_COORDINATOR_ERROR"]
    assert run.status == RunStatus.PASSED


def test_manual_analysis_service_calls_thin_post_ops_coordinator(
    session, app_settings, monkeypatch
):
    created = submit_manual_document(
        session,
        ManualDocumentCreate(
            title="HBM delivery hook test",
            source_name="Test Source",
            source_tier="T1",
            published_at="2026-07-18T00:00:00Z",
            language="en",
            raw_text="A sufficiently long controlled source body for testing the delivery hook only.",
        ),
    )
    run = RunAudit(
        run_id="manual-auto-delivery-hook",
        scenario="manual:test",
        status=RunStatus.PASSED,
        coverage_id="dram_hbm",
        coverage_version="test",
        coverage_snapshot={},
        thesis_version="test",
        prompt_version="test",
        model_adapter="FakeAnalysisAdapter",
        adapter_versions={},
        errors=[],
        artifact_paths=[],
        ops_check={"passed": True},
        run_kind="manual",
        replay_mode="LIVE_MODEL",
    )
    session.add(run)
    session.commit()

    class FakeHarness:
        def run(self, *args, **kwargs):
            return run

    monkeypatch.setattr(
        "app.manual.service.build_manual_harness", lambda *args, **kwargs: (FakeHarness(), {})
    )
    calls = []
    monkeypatch.setattr(
        "app.delivery.coordinator.coordinate_post_ops_email_delivery",
        lambda db, run_id, *, app_settings: calls.append(run_id),
    )
    configured = app_settings.model_copy(update={"email_auto_send": True})
    result = analyze_manual_document(session, created.document_id, configured)
    assert result.run_id == run.run_id
    assert calls == [run.run_id]
