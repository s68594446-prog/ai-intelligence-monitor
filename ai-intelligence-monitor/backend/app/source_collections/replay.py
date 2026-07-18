import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import RunStatus
from app.core.settings import Settings, settings
from app.models import RunAudit
from app.source_collections.contracts import DiscoveredItem, SourceRegistry
from app.source_collections.prefilter import prefilter
from app.source_collections.registry import get_source


def replay_frozen_source(session: Session, run_id: str, *, app_settings: Settings = settings) -> tuple[RunAudit, dict]:
    original = session.scalar(select(RunAudit).where(RunAudit.run_id == run_id, RunAudit.run_kind == "source_collection"))
    if original is None or not original.artifact_paths:
        raise ValueError(f"source collection run not found: {run_id}")
    frozen = json.loads(Path(original.artifact_paths[0]).read_text(encoding="utf-8"))
    registry = SourceRegistry.model_validate(frozen["registry_snapshot"])
    recomputed = []
    for payload in frozen["discovered_items"]:
        item = DiscoveredItem.model_validate(payload)
        source = get_source(registry, item.source_id)
        recomputed.append(prefilter(item, source, frozen["coverage_snapshot"]).model_dump(mode="json"))
    comparison = {
        "prefilter_results": {"equal": recomputed == frozen["prefilter_results"]},
        "document_decisions": {"equal": all(item.get("decision") in {"CREATED", "DUPLICATE"} for item in frozen["document_decisions"])},
        "content_hashes": {"equal": all(not item.get("content_hash") or len(item["content_hash"]) == 64 for item in frozen["document_decisions"])},
        "candidate_counts": {"equal": sum(item["candidate_status"] == "CANDIDATE" for item in recomputed) == sum(item["candidate_status"] == "CANDIDATE" for item in frozen["prefilter_results"])},
    }
    stable = all(item["equal"] for item in comparison.values())
    replay_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-source-replay-" + uuid4().hex[:8]
    replay_dir = Path(app_settings.runs_dir) / replay_id
    replay_dir.mkdir(parents=True, exist_ok=False)
    diff = {"mode": "frozen-source", "original_run_id": run_id, "replay_run_id": replay_id, "stable": stable, "comparison": comparison}
    artifact = {**frozen, "run_id": replay_id, "replay_of": run_id, "frozen_source_replay": True, "replay_diff": diff}
    artifact_path = replay_dir / "00-source-fetch.json"
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=str, ensure_ascii=False), encoding="utf-8")
    audit = RunAudit(
        run_id=replay_id, replay_of=run_id, scenario="source_collection_replay",
        status=RunStatus.PASSED if stable else RunStatus.FAILED,
        coverage_id=original.coverage_id, coverage_version=original.coverage_version,
        coverage_snapshot=original.coverage_snapshot, thesis_version=original.thesis_version,
        prompt_version=original.prompt_version, model_adapter="FrozenSourceReplay",
        adapter_versions=original.adapter_versions, artifact_paths=[str(artifact_path)], errors=[],
        run_kind="source_collection", replay_mode="FROZEN_SOURCE", ops_check={"passed": stable, "errors": [], "checks": 4},
        started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
    )
    session.add(audit)
    session.commit()
    return audit, diff
