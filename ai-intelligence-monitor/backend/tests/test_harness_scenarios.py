import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.enums import Adjudication, PipelineState, RedTeamAction, RunStatus, SignalLevel
from app.harness.service import replay_run
from app.models import Event, Signal, StateTransitionAudit
from app.pipeline.adapters import FixtureCollector
from app.pipeline.contracts import SourceInput
from app.pipeline.state_machine import InvalidStateTransition, transition


def artifact(run, name: str):
    path = Path(run.artifact_paths[0]).parent / name
    if name.endswith(".json"):
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


def test_no_material_change_allows_zero_alerts(harness):
    run = harness.run("no_material_change")
    assert run.status == RunStatus.PASSED
    assert run.events_created == 1
    assert run.signals_created == 0
    assert run.alerts_selected == 0
    assert "brief-summary:NO_MATERIAL_CHANGE" in artifact(run, "05-brief.html")


def test_duplicate_event_creates_one_event_and_signal(harness, session):
    run = harness.run("duplicate_event")
    events = session.scalars(select(Event).where(Event.run_id == run.run_id)).all()
    assert len(events) == 1
    assert events[0].duplicate_count == 1
    assert run.signals_created == 1


def test_t3_only_rumor_cannot_be_s3(harness, session):
    run = harness.run("t3_only_rumor")
    signal = session.scalar(select(Signal).where(Signal.run_id == run.run_id))
    assert signal.signal_level == SignalLevel.S1
    assert signal.pipeline_state == PipelineState.UNVERIFIED
    assert signal.adjudication == Adjudication.REQUEST_HUMAN_REVIEW


def test_verified_s3_passes_red_team_and_adjudication(harness, session):
    run = harness.run("verified_s3")
    signal = session.scalar(select(Signal).where(Signal.run_id == run.run_id))
    assert run.status == RunStatus.PASSED
    assert signal.signal_level == SignalLevel.S3
    assert signal.red_team_payload["action"] == RedTeamAction.CHALLENGE.value
    assert signal.adjudication == Adjudication.SEND_ALERT
    assert signal.pipeline_state == PipelineState.OPS_CHECKED
    assert len(list(Path(run.artifact_paths[0]).parent.glob("0*"))) == 5
    session.expire(run, ["artifact_paths"])
    assert len(run.artifact_paths) == 5


def test_conflicted_sources_are_held_for_human_review(harness, session):
    run = harness.run("conflicted_sources")
    signal = session.scalar(select(Signal).where(Signal.run_id == run.run_id))
    assert run.status == RunStatus.PASSED
    assert signal.pipeline_state == PipelineState.CONFLICTED
    assert signal.adjudication == Adjudication.REQUEST_HUMAN_REVIEW
    assert "published-signal-ids:[]" in artifact(run, "05-brief.html")


def test_missing_number_source_fails_ops_check(harness):
    run = harness.run("missing_number_source")
    assert run.status == RunStatus.FAILED_OPS_CHECK
    assert any("number has no source" in error for error in run.ops_check["errors"])


def test_red_team_downgrades_s3_to_s2(harness, session):
    run = harness.run("red_team_downgrade")
    signal = session.scalar(select(Signal).where(Signal.run_id == run.run_id))
    assert signal.signal_level == SignalLevel.S2
    assert signal.red_team_payload["action"] == RedTeamAction.DOWNGRADE.value
    assert signal.adjudication == Adjudication.INCLUDE_WEEKLY


def test_red_team_resurrects_discarded_candidate(harness, session):
    run = harness.run("red_team_resurrect")
    signal = session.scalar(select(Signal).where(Signal.run_id == run.run_id))
    assert run.status == RunStatus.PASSED
    assert signal.signal_level == SignalLevel.S2
    assert signal.red_team_payload["action"] == RedTeamAction.RESURRECT.value
    assert signal.adjudication == Adjudication.INCLUDE_WEEKLY


def test_replay_stability_creates_new_run_and_diff(harness, session, app_settings):
    original = harness.run("verified_s3")
    replayed, diff = replay_run(session, original.run_id, app_settings)
    assert replayed.run_id != original.run_id
    assert replayed.replay_of == original.run_id
    assert diff["stable"] is True
    assert (app_settings.runs_dir / replayed.run_id / "replay-diff.json").is_file()


def test_invalid_state_transition_is_rejected_and_not_audited(session):
    event = Event(
        run_id="invalid-transition",
        canonical_title="invalid",
        event_key="invalid",
        summary="invalid",
        document_ids=[],
        duplicate_count=0,
        pipeline_state=PipelineState.COLLECTED,
    )
    session.add(event)
    session.flush()
    with pytest.raises(InvalidStateTransition, match="COLLECTED -> RENDERED"):
        transition(
            session,
            run_id="invalid-transition",
            entity=event,
            to_state=PipelineState.RENDERED,
            reason="test illegal jump",
        )
    audits = session.scalars(
        select(StateTransitionAudit).where(StateTransitionAudit.run_id == "invalid-transition")
    ).all()
    assert audits == []


def test_invalid_adapter_payload_is_rejected(tmp_path):
    (tmp_path / "broken.json").write_text(
        '{"scenario":"broken","description":"bad","documents":[{"fixture_id":"x"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        FixtureCollector(tmp_path).collect("broken")


def test_illegal_adapter_enum_is_rejected():
    with pytest.raises(ValidationError):
        SourceInput.model_validate({
            "key": "bad-tier",
            "name": "Bad Tier",
            "source_type": "FORUM",
            "trust_tier": "T4",
            "url": "https://example.com/bad-tier",
        })


def test_invalid_adapter_payload_is_logged_by_harness(harness, session):
    from app.models import RunAudit

    with pytest.raises(ValidationError):
        harness.run("invalid_json")
    failed = session.scalar(
        select(RunAudit).where(RunAudit.scenario == "invalid_json").order_by(RunAudit.started_at.desc())
    )
    assert failed.status == RunStatus.FAILED
    assert any("ValidationError" in error for error in failed.errors)


def test_all_transition_audits_follow_state_graph(harness, session):
    run = harness.run("verified_s3")
    audits = session.scalars(
        select(StateTransitionAudit).where(StateTransitionAudit.run_id == run.run_id)
    ).all()
    assert len(audits) >= 8
    assert run.ops_check["passed"] is True
