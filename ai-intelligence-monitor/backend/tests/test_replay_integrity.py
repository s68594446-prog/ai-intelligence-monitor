import hashlib
import json

import pytest

from app.daily_ai.department_harness import (
    CONTENT_DEPARTMENTS, DepartmentId, DepartmentInput, DepartmentManifest,
    DepartmentManifestEntry, DepartmentOpsResult, DepartmentStatus, ProviderUsage,
    artifact_sha256, write_content_department_artifacts, write_stage_department_artifacts,
)
from app.daily_ai.replay_integrity import (
    DEPARTMENT_FILES, REQUIRED_FILES, ROOT_FILES, SEAL_NAME, ReplayIntegrityError,
    replay_run, seal_run, verify_run,
)


def make_run(path, passed=True):
    path.mkdir()
    handoffs = []
    status = DepartmentStatus.HEALTHY_EMPTY if passed else DepartmentStatus.BLOCKED
    for department in DepartmentId:
        if department in CONTENT_DEPARTMENTS:
            handoff = write_content_department_artifacts(
                run_dir=path, department_input=DepartmentInput(
                    run_id="offline-run", department_id=department,
                    department_prompt_version="fixture-v1", model_name="offline", request_budget=0,
                ), discovery=[], fetches=[], analyses=[], evidence=[],
                provider_usage=ProviderUsage(request_budget=0, request_count=0, candidate_attempts=0, validated_outputs=0),
                status=status,
            )
        else:
            handoff = write_stage_department_artifacts(
                run_dir=path, run_id="offline-run", department_id=department, status=status,
                input_payload={"contract_version": "phase3c-department-contract-v1"}, output_payload=[],
                ops=DepartmentOpsResult(department_id=department, status=status, passed=passed, checks={"fixture": passed}),
                downstream_consumer="offline",
            )
        handoffs.append(handoff)
    for name in ROOT_FILES:
        (path / name).write_text("{}")
    manifest = DepartmentManifest(
        run_id="offline-run", delivery_allowed=passed,
        entries=tuple(DepartmentManifestEntry(
            department_id=h.department_id, status=h.status,
            handoff_path=f"departments/{h.department_id.value}/handoff.json",
            handoff_sha256=artifact_sha256(h),
        ) for h in handoffs),
    )
    (path / "department-manifest.json").write_text(manifest.model_dump_json())
    (path / "source-bundle.json").write_text(json.dumps({"run_id": "offline-run"}))
    (path / "ops.json").write_text(json.dumps({"passed": passed}))
    (path / "04-digest.txt").write_text("离线回放样例\n")
    seal_run(path)
    return path


@pytest.mark.parametrize("passed", [True, False])
def test_replay_keeps_business_result_and_exact_bytes(tmp_path, monkeypatch, passed):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    source = make_run(tmp_path / "source", passed)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    target = tmp_path / "replay"
    result = replay_run(source, target)
    assert result["integrity_passed"] is True
    assert result["delivery_allowed"] is passed
    assert result["network_calls"] == result["llm_calls"] == result["smtp_calls"] == 0
    assert verify_run(target) == result
    for relative, payload in before.items():
        assert (source / relative).read_bytes() == (target / relative).read_bytes() == payload


@pytest.mark.parametrize("relative", [*(f"departments/global/{name}" for name in DEPARTMENT_FILES), *ROOT_FILES, "04-digest.txt"])
def test_any_modified_artifact_blocks_before_output(tmp_path, relative):
    source = make_run(tmp_path / "source")
    (source / relative).write_bytes((source / relative).read_bytes() + b" ")
    with pytest.raises(ReplayIntegrityError, match="HASH_MISMATCH"):
        replay_run(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".phase3c-replay-*"))


@pytest.mark.parametrize("mutation", ["remove", "omit_body", "traversal", "identity", "reference", "duplicate", "delivery", "version"])
def test_seal_cannot_mask_broken_contracts(tmp_path, mutation):
    source = make_run(tmp_path / "source")
    seal_path = source / SEAL_NAME
    seal = json.loads(seal_path.read_text())
    if mutation == "remove":
        seal["files"].pop("departments/global/provider-usage.json")
    elif mutation == "omit_body":
        seal["files"].pop("04-digest.txt")
    elif mutation == "traversal":
        seal["files"]["../outside.json"] = "0" * 64
    elif mutation == "version":
        seal["version"] = "unknown"
    else:
        name = "department-manifest.json"
        value = json.loads((source / name).read_text())
        if mutation == "identity":
            value["run_id"] = "different-run"
        elif mutation == "reference":
            value["entries"][0]["handoff_sha256"] = "0" * 64
        elif mutation == "duplicate":
            value["entries"][-1] = value["entries"][0]
        elif mutation == "delivery":
            value["delivery_allowed"] = False
        (source / name).write_text(json.dumps(value))
        seal["files"][name] = hashlib.sha256((source / name).read_bytes()).hexdigest()
    seal_path.write_text(json.dumps(seal))
    with pytest.raises(ReplayIntegrityError):
        replay_run(source, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_legacy_and_symlinks_remain_unverified(tmp_path):
    source = make_run(tmp_path / "source")
    (source / "02-fetch.json").unlink()
    (source / "02-fetch.json").symlink_to(source / "analysis-failures.json")
    with pytest.raises(ReplayIntegrityError, match="SYMLINK"):
        verify_run(source)
    (source / SEAL_NAME).unlink()
    with pytest.raises(ReplayIntegrityError, match="LEGACY_UNSEALED"):
        verify_run(source)


@pytest.mark.parametrize("target_kind", ["existing", "source", "nested"])
def test_replay_never_overwrites_or_writes_inside_source(tmp_path, target_kind):
    source = make_run(tmp_path / "source")
    target = {"existing": tmp_path / "existing", "source": source, "nested": source / "nested"}[target_kind]
    if target_kind == "existing":
        target.mkdir()
        (target / "mine.txt").write_text("keep")
    with pytest.raises(ReplayIntegrityError, match="TARGET_UNSAFE"):
        replay_run(source, target)
    if target_kind == "existing":
        assert (target / "mine.txt").read_text() == "keep"


def test_seal_is_write_once(tmp_path):
    source = make_run(tmp_path / "source")
    with pytest.raises(FileExistsError):
        seal_run(source)
