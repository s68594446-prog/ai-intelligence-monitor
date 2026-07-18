import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import Adjudication, DeliveryStatus, PipelineState, RunStatus, SignalLevel
from app.core.settings import Settings, settings
from app.delivery.contracts import (
    EmailDeliveryOutcome,
    EmailDeliveryRequest,
    EmailDeliveryResult,
)
from app.delivery.email_adapter import (
    EmailDeliveryAdapter,
    EmailProviderError,
    SMTPConfiguration,
    resolve_smtp_configuration,
)
from app.models import DeliveryFeedback, Document, Event, RunAudit, Signal, StateTransitionAudit
from app.pipeline.contracts import AnalysisResult, RedTeamResult


EMAIL_CHANNEL = "EMAIL"
MAX_BRIEF_BYTES = 1_048_576
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_LIVE_MANUAL_ADAPTERS = {
    "OpenAIAnalysisAdapter": (
        "openai-responses-analysis-v1",
        "openai-responses-red-team-v1",
    ),
    "DeepSeekAnalysisAdapter": (
        "deepseek-chat-completions-analysis-v1",
        "deepseek-chat-completions-red-team-v1",
    ),
}
_NON_PRODUCTION_MODEL_MARKERS = (
    "test",
    "fake",
    "mock",
    "offline",
    "fixture",
    "deterministic",
)


@dataclass
class _GateDecision:
    result: EmailDeliveryResult
    run: RunAudit | None = None
    signal_id: int | None = None
    brief_path: Path | None = None
    brief_html: str | None = None
    configuration: SMTPConfiguration | None = None
    idempotency_key: str | None = None
    recipient_fingerprint: str | None = None
    message_id: str | None = None


def _existing_result(run_id: str, feedback: DeliveryFeedback) -> EmailDeliveryResult:
    mapping = {
        DeliveryStatus.EMAIL_SENT: EmailDeliveryOutcome.ALREADY_SENT,
        DeliveryStatus.EMAIL_FAILED: EmailDeliveryOutcome.FAILED,
        DeliveryStatus.EMAIL_PENDING: EmailDeliveryOutcome.UNKNOWN,
        DeliveryStatus.EMAIL_UNKNOWN: EmailDeliveryOutcome.UNKNOWN,
        DeliveryStatus.BLOCKED: EmailDeliveryOutcome.BLOCKED,
    }
    reason = {
        DeliveryStatus.EMAIL_SENT: "EMAIL_ALREADY_SENT",
        DeliveryStatus.EMAIL_FAILED: "EMAIL_PREVIOUSLY_FAILED",
        DeliveryStatus.EMAIL_PENDING: "EMAIL_ATTEMPT_REQUIRES_MANUAL_REVIEW",
        DeliveryStatus.EMAIL_UNKNOWN: "EMAIL_DELIVERY_STATE_UNKNOWN",
        DeliveryStatus.BLOCKED: "EMAIL_PREVIOUSLY_BLOCKED",
    }.get(feedback.delivery_status, "EMAIL_ATTEMPT_ALREADY_EXISTS")
    return EmailDeliveryResult(
        run_id=run_id,
        status=mapping.get(feedback.delivery_status, EmailDeliveryOutcome.BLOCKED),
        reasons=[reason],
        attempt_id=feedback.id,
        idempotency_key=feedback.idempotency_key,
        provider_message_id=feedback.provider_message_id,
    )


def _sanitize_error_code(code: str) -> str:
    return code if _SAFE_ERROR_CODE.fullmatch(code) else "EMAIL_PROVIDER_ERROR"


def _safe_run_brief(settings: Settings, run_id: str) -> tuple[Path | None, list[str]]:
    if not run_id or "\r" in run_id or "\n" in run_id:
        return None, ["RUN_ID_INVALID"]
    runs_root = Path(settings.runs_dir).resolve()
    run_dir_candidate = runs_root / run_id
    if run_dir_candidate.is_symlink():
        return None, ["RUN_PATH_INVALID"]
    run_dir = run_dir_candidate.resolve()
    if run_dir.parent != runs_root:
        return None, ["RUN_PATH_INVALID"]
    requested = run_dir / "05-brief.html"
    if requested.is_symlink():
        return None, ["BRIEF_PATH_INVALID"]
    if not requested.exists():
        return None, ["BRIEF_MISSING"]
    resolved = requested.resolve()
    if resolved.parent != run_dir or not resolved.is_file():
        return None, ["BRIEF_PATH_INVALID"]
    return resolved, []


def _read_brief(path: Path) -> tuple[str | None, list[str]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None, ["BRIEF_UNREADABLE"]
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            return None, ["BRIEF_PATH_INVALID"]
        if file_stat.st_size > MAX_BRIEF_BYTES:
            return None, ["BRIEF_TOO_LARGE"]
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(MAX_BRIEF_BYTES + 1)
        if len(payload) > MAX_BRIEF_BYTES:
            return None, ["BRIEF_TOO_LARGE"]
        try:
            return payload.decode("utf-8"), []
        except UnicodeDecodeError:
            return None, ["BRIEF_ENCODING_INVALID"]
    finally:
        os.close(descriptor)


def _validate_manual_run_provenance(
    session: Session,
    run: RunAudit,
    reasons: list[str],
) -> list[Document]:
    if run.replay_mode != "LIVE_MODEL":
        reasons.append("REPLAY_MODE_NOT_DELIVERABLE")
    if run.llm_schema_validated is not True:
        reasons.append("LLM_SCHEMA_NOT_VALIDATED")
    expected_versions = _LIVE_MANUAL_ADAPTERS.get(run.model_adapter)
    versions = run.adapter_versions or {}
    if expected_versions is None:
        reasons.append("NON_LIVE_ANALYSIS_ADAPTER")
    elif (
        versions.get("collector") != "manual-collector-v1"
        or versions.get("analysis") != expected_versions[0]
        or versions.get("red_team") != expected_versions[1]
    ):
        reasons.append("MANUAL_ADAPTER_PROVENANCE_INVALID")
    if not (
        run.analysis_prompt_version
        and run.red_team_prompt_version
        and run.model_name
        and run.red_team_model_name
    ):
        reasons.append("MANUAL_MODEL_AUDIT_INCOMPLETE")
    elif any(
        marker in model_name.lower()
        for model_name in (run.model_name, run.red_team_model_name)
        for marker in _NON_PRODUCTION_MODEL_MARKERS
    ):
        reasons.append("TEST_MODEL_BLOCKED")

    documents = session.scalars(select(Document).where(Document.run_id == run.run_id)).all()
    snapshot = run.manual_input_snapshot
    if len(documents) != 1 or not isinstance(snapshot, dict):
        reasons.append("MANUAL_RUN_PROVENANCE_INVALID")
        return documents
    document = documents[0]
    raw_text = snapshot.get("raw_text")
    if not isinstance(raw_text, str):
        reasons.append("MANUAL_RUN_PROVENANCE_INVALID")
        return documents
    snapshot_matches = (
        document.ingestion_id is not None
        and document.id == snapshot.get("document_id")
        and document.ingestion_id == snapshot.get("ingestion_id")
        and document.raw_text == raw_text
        and document.content_hash == snapshot.get("content_hash")
        and document.content_hash == hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        and document.title == snapshot.get("title")
        and document.url == (snapshot.get("url") or "")
        and document.source.name == snapshot.get("source_name")
        and document.source.trust_tier.value == snapshot.get("source_tier")
        and run.scenario == f"manual:{document.id}"
    )
    if not snapshot_matches:
        reasons.append("MANUAL_RUN_PROVENANCE_INVALID")
    return documents


def _validate_signal_audit(
    session: Session,
    signal: Signal,
    event_by_id: dict[int, Event],
    document_by_reference: dict[str, Document],
    reasons: list[str],
) -> None:
    try:
        analysis = AnalysisResult.model_validate(signal.analysis_payload)
    except ValidationError:
        reasons.append("ANALYSIS_AUDIT_INVALID")
    else:
        if not analysis.schema_validated or analysis.research_contract is None:
            reasons.append("ANALYSIS_AUDIT_INVALID")
        if (
            analysis.affected_variable != signal.affected_variable
            or analysis.direction != signal.direction
            or analysis.adjudication != Adjudication.SEND_ALERT
        ):
            reasons.append("ANALYSIS_SIGNAL_MISMATCH")
        if analysis.research_contract is not None and (
            analysis.research_contract.proposed_signal_level != analysis.signal_level
            or not analysis.research_contract.affected_variables
            or analysis.research_contract.affected_variables[0] != analysis.affected_variable
        ):
            reasons.append("ANALYSIS_SIGNAL_MISMATCH")
        document_reference_ids = set(document_by_reference)
        for fact in analysis.facts:
            if signal.signal_level in {SignalLevel.S2, SignalLevel.S3} and not (
                fact.source_refs or fact.missing_marker
            ):
                reasons.append("SIGNAL_EVIDENCE_INVALID")
            if set(fact.source_refs) - document_reference_ids:
                reasons.append("SIGNAL_EVIDENCE_INVALID")
        if analysis.research_contract is not None:
            claims = analysis.research_contract.extracted_claims
            if [fact.statement for fact in analysis.facts] != [claim.statement for claim in claims]:
                reasons.append("ANALYSIS_EVIDENCE_MISMATCH")
            for claim in claims:
                if signal.signal_level in {SignalLevel.S2, SignalLevel.S3} and not (
                    claim.evidence_references or claim.missing_marker
                ):
                    reasons.append("SIGNAL_EVIDENCE_INVALID")
                for reference in claim.evidence_references:
                    document = document_by_reference.get(reference.reference_id)
                    if document is None or reference.quote not in document.raw_text:
                        reasons.append("SIGNAL_EVIDENCE_INVALID")

    try:
        review = RedTeamResult.model_validate(signal.red_team_payload)
    except (ValidationError, TypeError):
        reasons.append("RED_TEAM_INVALID")
    else:
        if not review.schema_validated or review.research_contract is None:
            reasons.append("RED_TEAM_INVALID")
        if review.resulting_level != signal.signal_level:
            reasons.append("RED_TEAM_SIGNAL_MISMATCH")
        if (
            review.research_contract is not None
            and review.research_contract.recommended_signal_level != review.resulting_level
        ):
            reasons.append("RED_TEAM_SIGNAL_MISMATCH")

    event = event_by_id.get(signal.event_id)
    if event is None or event.pipeline_state != PipelineState.ADJUDICATED:
        reasons.append("EVENT_NOT_ADJUDICATED")

    transitions = session.scalars(
        select(StateTransitionAudit).where(
            StateTransitionAudit.run_id == signal.run_id,
            StateTransitionAudit.entity_type == "Signal",
            StateTransitionAudit.entity_id == signal.id,
        )
    ).all()
    audited_steps = {(item.from_state, item.to_state) for item in transitions}
    required_steps = {
        (PipelineState.ANALYZED, PipelineState.RED_TEAMED),
        (PipelineState.RED_TEAMED, PipelineState.ADJUDICATED),
        (PipelineState.ADJUDICATED, PipelineState.RENDERED),
        (PipelineState.RENDERED, PipelineState.OPS_CHECKED),
    }
    if not required_steps.issubset(audited_steps):
        reasons.append("SIGNAL_TRANSITION_AUDIT_INCOMPLETE")


def _fingerprint_recipients(recipients: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(recipients).encode("utf-8")).hexdigest()


def _idempotency_key(run_id: str, brief_html: str, recipient_fingerprint: str) -> str:
    payload = json.dumps(
        {
            "run_id": run_id,
            "delivery_channel": EMAIL_CHANNEL,
            "brief_sha256": hashlib.sha256(brief_html.encode("utf-8")).hexdigest(),
            "recipient_fingerprint": recipient_fingerprint,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_email_delivery_gate(
    session: Session,
    run_id: str,
    *,
    app_settings: Settings = settings,
) -> _GateDecision:
    existing = session.scalar(
        select(DeliveryFeedback).where(
            DeliveryFeedback.run_id == run_id,
            DeliveryFeedback.delivery_channel == EMAIL_CHANNEL,
        )
    )
    if existing is not None:
        return _GateDecision(result=_existing_result(run_id, existing))

    run = session.scalar(select(RunAudit).where(RunAudit.run_id == run_id))
    reasons: list[str] = []
    if run is None:
        return _GateDecision(
            result=EmailDeliveryResult(
                run_id=run_id, status=EmailDeliveryOutcome.BLOCKED, reasons=["RUN_NOT_FOUND"]
            )
        )
    if run.replay_of is not None:
        reasons.append("REPLAY_RUN_BLOCKED")
    if run.run_kind != "manual":
        reasons.append("RUN_KIND_NOT_DELIVERABLE")
        documents: list[Document] = []
    else:
        documents = _validate_manual_run_provenance(session, run, reasons)
    if run.status != RunStatus.PASSED:
        reasons.append("RUN_NOT_PASSED")
    if not run.ops_check or run.ops_check.get("passed") is not True:
        reasons.append("OPS_CHECK_NOT_PASSED")
    if run.alerts_selected <= 0:
        reasons.append("NO_ALERT_SELECTED")

    signals = session.scalars(select(Signal).where(Signal.run_id == run_id)).all()
    alert_signals = [item for item in signals if item.adjudication == Adjudication.SEND_ALERT]
    if run.signals_created != len(signals):
        reasons.append("SIGNAL_COUNT_MISMATCH")
    if run.alerts_selected != len(alert_signals):
        reasons.append("ALERT_COUNT_MISMATCH")
    if not alert_signals:
        reasons.append("NO_SEND_ALERT_SIGNAL")
    for signal in signals:
        if signal.adjudication != Adjudication.SEND_ALERT:
            reasons.append("NON_ALERT_ADJUDICATION_PRESENT")
        if signal.signal_level in {SignalLevel.S0, SignalLevel.S1}:
            reasons.append("LOW_SIGNAL_LEVEL_BLOCKED")
        if signal.pipeline_state != PipelineState.OPS_CHECKED:
            reasons.append("SIGNAL_NOT_OPS_CHECKED")
    for signal in alert_signals:
        if signal.signal_level not in {SignalLevel.S2, SignalLevel.S3}:
            reasons.append("ALERT_SIGNAL_LEVEL_INVALID")
        if not signal.red_team_payload:
            reasons.append("RED_TEAM_REQUIRED")

    blocked_event_states = {
        PipelineState.OUT_OF_SCOPE,
        PipelineState.CONFLICTED,
        PipelineState.INSUFFICIENT_EVIDENCE,
        PipelineState.UNVERIFIED,
        PipelineState.REJECTED,
        PipelineState.FAILED,
    }
    events = session.scalars(select(Event).where(Event.run_id == run_id)).all()
    event_by_id = {event.id: event for event in events}
    if any(event.pipeline_state in blocked_event_states for event in events):
        reasons.append("BLOCKED_EVENT_STATE_PRESENT")
    document_by_reference = {
        str(document.fixture_payload.get("fixture_id")): document
        for document in documents
        if isinstance(document.fixture_payload, dict)
        and document.fixture_payload.get("fixture_id")
    }
    for signal in alert_signals:
        _validate_signal_audit(
            session,
            signal,
            event_by_id,
            document_by_reference,
            reasons,
        )

    brief_path, brief_errors = _safe_run_brief(app_settings, run_id)
    reasons.extend(brief_errors)
    brief_html = None
    if brief_path is not None:
        audited_paths = {str(Path(path).resolve()) for path in run.artifact_paths or []}
        if str(brief_path) not in audited_paths:
            reasons.append("BRIEF_NOT_AUDITED")
        brief_html, read_errors = _read_brief(brief_path)
        reasons.extend(read_errors)
        if brief_html is not None and not (
            "ops-check-status:PASSED" in brief_html
            and re.search(r"<code\s+data-ops-check-status>PASSED</code>", brief_html)
        ):
            reasons.append("BRIEF_OPS_STATUS_NOT_PASSED")

    if not app_settings.email_delivery_enabled:
        reasons.append("EMAIL_DELIVERY_DISABLED")
    configuration, configuration_errors = resolve_smtp_configuration(app_settings)
    reasons.extend(configuration_errors)
    reasons = list(dict.fromkeys(reasons))
    if reasons or configuration is None or brief_path is None or brief_html is None:
        return _GateDecision(
            result=EmailDeliveryResult(
                run_id=run_id,
                status=EmailDeliveryOutcome.BLOCKED,
                reasons=reasons or ["EMAIL_GATE_BLOCKED"],
            ),
            run=run,
        )

    recipient_fingerprint = _fingerprint_recipients(configuration.recipients)
    idempotency_key = _idempotency_key(run_id, brief_html, recipient_fingerprint)
    message_id = f"<aim-{idempotency_key[:32]}@{configuration.message_id_domain}>"
    return _GateDecision(
        result=EmailDeliveryResult(
            run_id=run_id,
            status=EmailDeliveryOutcome.READY,
            idempotency_key=idempotency_key,
            recipient_count=len(configuration.recipients),
            dry_run=True,
        ),
        run=run,
        signal_id=alert_signals[0].id,
        brief_path=brief_path,
        brief_html=brief_html,
        configuration=configuration,
        idempotency_key=idempotency_key,
        recipient_fingerprint=recipient_fingerprint,
        message_id=message_id,
    )


def deliver_run_email(
    session: Session,
    run_id: str,
    adapter: EmailDeliveryAdapter | None,
    *,
    app_settings: Settings = settings,
    dry_run: bool = False,
) -> EmailDeliveryResult:
    decision = evaluate_email_delivery_gate(session, run_id, app_settings=app_settings)
    if decision.result.status != EmailDeliveryOutcome.READY:
        return (
            decision.result.model_copy(update={"dry_run": True})
            if dry_run
            else decision.result
        )
    if dry_run:
        return decision.result
    if adapter is None:
        return decision.result.model_copy(
            update={"status": EmailDeliveryOutcome.BLOCKED, "reasons": ["EMAIL_ADAPTER_REQUIRED"]}
        )

    assert decision.brief_path is not None
    assert decision.brief_html is not None
    assert decision.configuration is not None
    assert decision.idempotency_key is not None
    assert decision.recipient_fingerprint is not None
    assert decision.message_id is not None
    now = datetime.now(timezone.utc)
    attempt = DeliveryFeedback(
        signal_id=decision.signal_id,
        run_id=run_id,
        delivery_status=DeliveryStatus.EMAIL_PENDING,
        rendered_path=str(decision.brief_path),
        feedback="Email delivery attempt pending",
        delivery_channel=EMAIL_CHANNEL,
        idempotency_key=decision.idempotency_key,
        recipient_fingerprint=decision.recipient_fingerprint,
        provider_message_id=decision.message_id,
        attempted_at=now,
        sent_at=None,
        error_code=None,
    )
    session.add(attempt)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(DeliveryFeedback).where(
                DeliveryFeedback.run_id == run_id,
                DeliveryFeedback.delivery_channel == EMAIL_CHANNEL,
            )
        )
        if existing is not None:
            return _existing_result(run_id, existing)
        return EmailDeliveryResult(
            run_id=run_id,
            status=EmailDeliveryOutcome.UNKNOWN,
            reasons=["EMAIL_IDEMPOTENCY_CONFLICT"],
        )

    request = EmailDeliveryRequest(
        run_id=run_id,
        from_address=decision.configuration.from_address,
        to_addresses=decision.configuration.recipients,
        subject=f"AI Intelligence Monitor Alert — {run_id}",
        html_body=decision.brief_html,
        text_body=(
            "AI Intelligence Monitor alert\n"
            f"Run: {run_id}\n"
            "The attached HTML alternative contains the authoritative local brief."
        ),
        message_id=decision.message_id,
    )
    try:
        provider_result = adapter.send(request)
    except EmailProviderError as exc:
        attempt.delivery_status = (
            DeliveryStatus.EMAIL_UNKNOWN if exc.outcome_unknown else DeliveryStatus.EMAIL_FAILED
        )
        attempt.error_code = _sanitize_error_code(exc.code)
        attempt.feedback = f"Email delivery did not complete: {attempt.error_code}"
        session.commit()
        return EmailDeliveryResult(
            run_id=run_id,
            status=(
                EmailDeliveryOutcome.UNKNOWN
                if exc.outcome_unknown
                else EmailDeliveryOutcome.FAILED
            ),
            reasons=[attempt.error_code],
            attempt_id=attempt.id,
            idempotency_key=attempt.idempotency_key,
            provider_message_id=attempt.provider_message_id,
            recipient_count=len(decision.configuration.recipients),
        )
    except Exception:
        attempt.delivery_status = DeliveryStatus.EMAIL_UNKNOWN
        attempt.error_code = "EMAIL_PROVIDER_UNEXPECTED_ERROR"
        attempt.feedback = "Email delivery state requires manual review"
        session.commit()
        return EmailDeliveryResult(
            run_id=run_id,
            status=EmailDeliveryOutcome.UNKNOWN,
            reasons=[attempt.error_code],
            attempt_id=attempt.id,
            idempotency_key=attempt.idempotency_key,
            provider_message_id=attempt.provider_message_id,
            recipient_count=len(decision.configuration.recipients),
        )

    provider_message_id = provider_result.provider_message_id
    if provider_message_id != decision.message_id:
        attempt.delivery_status = DeliveryStatus.EMAIL_UNKNOWN
        attempt.error_code = "PROVIDER_MESSAGE_ID_INVALID"
        attempt.feedback = "Email may have been sent; provider result was invalid"
        session.commit()
        return EmailDeliveryResult(
            run_id=run_id,
            status=EmailDeliveryOutcome.UNKNOWN,
            reasons=[attempt.error_code],
            attempt_id=attempt.id,
            idempotency_key=attempt.idempotency_key,
            provider_message_id=attempt.provider_message_id,
            recipient_count=len(decision.configuration.recipients),
        )
    attempt.delivery_status = DeliveryStatus.EMAIL_SENT
    attempt.provider_message_id = provider_message_id
    attempt.sent_at = datetime.now(timezone.utc)
    attempt.error_code = None
    attempt.feedback = "Email delivery accepted by SMTP provider"
    try:
        session.commit()
    except Exception:
        session.rollback()
        pending = session.scalar(
            select(DeliveryFeedback).where(DeliveryFeedback.id == attempt.id)
        )
        if pending is not None:
            pending.delivery_status = DeliveryStatus.EMAIL_UNKNOWN
            pending.error_code = "POST_SEND_AUDIT_PERSISTENCE_ERROR"
            pending.feedback = "Email may have been sent; audit persistence requires manual review"
            session.commit()
        return EmailDeliveryResult(
            run_id=run_id,
            status=EmailDeliveryOutcome.UNKNOWN,
            reasons=["POST_SEND_AUDIT_PERSISTENCE_ERROR"],
            attempt_id=attempt.id,
            idempotency_key=decision.idempotency_key,
            provider_message_id=decision.message_id,
            recipient_count=len(decision.configuration.recipients),
        )
    return EmailDeliveryResult(
        run_id=run_id,
        status=EmailDeliveryOutcome.SENT,
        attempt_id=attempt.id,
        idempotency_key=attempt.idempotency_key,
        provider_message_id=attempt.provider_message_id,
        recipient_count=len(decision.configuration.recipients),
    )
