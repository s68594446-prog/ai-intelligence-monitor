import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import Settings, settings
from app.harness.runner import Harness
from app.harness.service import summarize_run
from app.manual.collector import ManualSnapshotCollector
from app.models import RunAudit
from app.pipeline.adapters import AnalysisAdapter, RedTeamAdapter
from app.pipeline.contracts import AnalysisResult, DocumentInput, RedTeamResult
from app.pipeline.provider import build_live_adapters


class FrozenAnalysisAdapter(AnalysisAdapter):
    name = "FrozenAnalysisAdapter"
    version = "frozen-analysis-v1"

    def __init__(self, payload: dict, prompt_version: str | None, model: str | None):
        self.payload = payload
        self.prompt_version = prompt_version
        self.model = model

    def analyze(self, document: DocumentInput) -> AnalysisResult:
        result = AnalysisResult.model_validate(self.payload)
        return result.model_copy(
            update={
                "adapter_name": self.name,
                "adapter_version": self.version,
                "schema_validated": True,
            }
        )


class FrozenRedTeamAdapter(RedTeamAdapter):
    name = "FrozenRedTeamAdapter"
    version = "frozen-red-team-v1"

    def __init__(self, payload: dict | None, prompt_version: str | None, model: str | None):
        self.payload = payload
        self.prompt_version = prompt_version
        self.model = model

    def review(
        self,
        document: DocumentInput,
        analysis: AnalysisResult | None = None,
    ) -> RedTeamResult:
        if self.payload is None:
            raise ValueError("frozen run did not contain a Red Team output")
        payload = {key: value for key, value in self.payload.items() if key != "signal_id"}
        result = RedTeamResult.model_validate(payload)
        return result.model_copy(
            update={
                "adapter_name": self.name,
                "adapter_version": self.version,
                "schema_validated": True,
            }
        )


def replay_manual_run(
    session: Session,
    run_id: str,
    *,
    mode: str = "frozen-output",
    app_settings: Settings = settings,
    analysis_adapter: AnalysisAdapter | None = None,
    red_team_adapter: RedTeamAdapter | None = None,
) -> tuple[RunAudit, dict]:
    original = session.scalar(select(RunAudit).where(RunAudit.run_id == run_id))
    if original is None or original.run_kind != "manual":
        raise ValueError(f"manual run not found: {run_id}")
    if mode not in {"frozen-output", "live-model"}:
        raise ValueError("manual replay mode must be frozen-output or live-model")
    if not original.manual_input_snapshot:
        raise ValueError("manual run has no frozen input snapshot")
    original_dir = Path(app_settings.runs_dir) / original.run_id
    analysis_artifact = json.loads((original_dir / "03-analysis.json").read_text(encoding="utf-8"))
    red_team_artifact = json.loads((original_dir / "04-red-team.json").read_text(encoding="utf-8"))
    if not analysis_artifact.get("adapter_outputs"):
        raise ValueError("manual run has no frozen structured analysis output")

    if mode == "frozen-output":
        analysis = FrozenAnalysisAdapter(
            analysis_artifact["adapter_outputs"][0]["analysis"],
            original.analysis_prompt_version,
            original.model_name,
        )
        red_team = FrozenRedTeamAdapter(
            red_team_artifact["reviews"][0] if red_team_artifact["reviews"] else None,
            original.red_team_prompt_version,
            original.red_team_model_name,
        )
        replay_mode = "FROZEN_OUTPUT"
    else:
        thesis_snapshot = {
            "statement": original.coverage_snapshot["thesis"],
            "assumptions": original.coverage_snapshot["supporting_assumptions"],
            "disconfirming_conditions": original.coverage_snapshot["disconfirming_conditions"],
            "reverse_triggers": original.coverage_snapshot["reverse_triggers"],
            "version": original.thesis_version,
        }
        if analysis_adapter is None or red_team_adapter is None:
            default_analysis, default_red_team = build_live_adapters(
                app_settings,
                original.coverage_snapshot,
                thesis_snapshot,
            )
        analysis = analysis_adapter or default_analysis
        red_team = red_team_adapter or default_red_team
        replay_mode = "LIVE_MODEL"

    harness = Harness(
        session=session,
        runs_dir=app_settings.runs_dir,
        coverage_path=app_settings.coverage_path,
        collector=ManualSnapshotCollector(original.manual_input_snapshot),
        analysis=analysis,
        red_team=red_team,
        prompt_version=original.prompt_version,
        analysis_prompt_version=original.analysis_prompt_version,
        red_team_prompt_version=original.red_team_prompt_version,
        model_name=getattr(analysis, "model", original.model_name),
        red_team_model_name=getattr(red_team, "model", original.red_team_model_name),
    )
    replay = harness.run(
        original.scenario,
        replay_of=original.run_id,
        coverage_snapshot=original.coverage_snapshot,
        run_kind="manual",
        replay_mode=replay_mode,
        manual_input_snapshot=original.manual_input_snapshot,
    )
    original_summary = _manual_summary(original_dir)
    replay_dir = Path(app_settings.runs_dir) / replay.run_id
    replay_summary = _manual_summary(replay_dir)
    comparison = {
        field: {
            "original": original_summary[field],
            "replay": replay_summary[field],
            "equal": original_summary[field] == replay_summary[field],
        }
        for field in original_summary
    }
    stable = all(item["equal"] for item in comparison.values())
    diff = {
        "mode": mode,
        "original_run_id": original.run_id,
        "replay_run_id": replay.run_id,
        "stable": stable,
        "comparison": comparison,
    }
    diff_path = replay_dir / "replay-diff.json"
    diff_path.write_text(json.dumps(diff, indent=2, sort_keys=True, default=str), encoding="utf-8")
    replay.artifact_paths = list(replay.artifact_paths) + [str(diff_path)]
    session.commit()
    return replay, diff


def _manual_summary(run_dir: Path) -> dict:
    summary = summarize_run(run_dir)
    analysis = json.loads((run_dir / "03-analysis.json").read_text(encoding="utf-8"))
    red_team = json.loads((run_dir / "04-red-team.json").read_text(encoding="utf-8"))
    summary["analysis_contracts"] = [
        item["analysis"].get("research_contract")
        for item in analysis.get("adapter_outputs", [])
    ]
    summary["red_team_contracts"] = [
        {key: value for key, value in item.items() if key != "signal_id"}
        .get("research_contract")
        for item in red_team.get("reviews", [])
    ]
    return summary
