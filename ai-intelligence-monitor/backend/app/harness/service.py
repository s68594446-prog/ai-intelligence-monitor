import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import Settings, settings
from app.harness.runner import Harness
from app.models import RunAudit
from app.pipeline.adapters import (
    DeterministicAnalysisAdapter,
    DeterministicRedTeamAdapter,
    FixtureCollector,
)


def build_harness(session: Session, app_settings: Settings = settings) -> Harness:
    return Harness(
        session=session,
        runs_dir=app_settings.runs_dir,
        coverage_path=app_settings.coverage_path,
        collector=FixtureCollector(app_settings.fixtures_dir),
        analysis=DeterministicAnalysisAdapter(),
        red_team=DeterministicRedTeamAdapter(),
        prompt_version=app_settings.prompt_version,
    )


def replay_run(session: Session, run_id: str, app_settings: Settings = settings) -> tuple[RunAudit, dict]:
    original = session.scalar(select(RunAudit).where(RunAudit.run_id == run_id))
    if original is None:
        raise ValueError(f"run not found: {run_id}")
    harness = build_harness(session, app_settings)
    expected_versions = {
        "collector": harness.collector.version,
        "analysis": harness.analysis.version,
        "red_team": harness.red_team.version,
    }
    if original.adapter_versions != expected_versions:
        raise ValueError("installed deterministic adapter versions do not match the original run")
    replay = harness.run(
        original.scenario,
        replay_of=original.run_id,
        coverage_snapshot=original.coverage_snapshot,
    )
    original_summary = summarize_run(Path(app_settings.runs_dir) / original.run_id)
    replay_summary = summarize_run(Path(app_settings.runs_dir) / replay.run_id)
    fields = ["event_count", "signal_levels", "red_team_actions", "adjudications", "brief_summary"]
    diff = {
        "original_run_id": original.run_id,
        "replay_run_id": replay.run_id,
        "stable": all(original_summary[field] == replay_summary[field] for field in fields),
        "comparison": {
            field: {
                "original": original_summary[field],
                "replay": replay_summary[field],
                "equal": original_summary[field] == replay_summary[field],
            }
            for field in fields
        },
    }
    diff_path = Path(app_settings.runs_dir) / replay.run_id / "replay-diff.json"
    diff_path.write_text(json.dumps(diff, indent=2, sort_keys=True), encoding="utf-8")
    replay.artifact_paths = list(replay.artifact_paths) + [str(diff_path)]
    session.commit()
    return replay, diff


def summarize_run(run_dir: Path) -> dict:
    triage = json.loads((run_dir / "02-triage.json").read_text(encoding="utf-8"))
    analysis = json.loads((run_dir / "03-analysis.json").read_text(encoding="utf-8"))
    red_team = json.loads((run_dir / "04-red-team.json").read_text(encoding="utf-8"))
    brief = (run_dir / "05-brief.html").read_text(encoding="utf-8")
    summary_match = re.search(r"brief-summary:([A-Z_]+)", brief)
    signals = sorted(analysis["signals"], key=lambda item: item["event_key"])
    reviews = sorted(red_team["reviews"], key=lambda item: item["signal_id"])
    return {
        "event_count": len(triage["events"]),
        "signal_levels": [[item["event_key"], item["signal_level"]] for item in signals],
        "red_team_actions": [item["action"] for item in reviews],
        "adjudications": [[item["event_key"], item["adjudication"]] for item in signals],
        "brief_summary": summary_match.group(1) if summary_match else None,
    }

