"""Offline, byte-preserving Phase 3C replay; frozen harnesses stay untouched.

Checksums detect accidental changes, not an attacker replacing both the files
and their manifest. Historical runs without a seal are explicitly unverified.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from app.daily_ai.department_harness import (
    CONTENT_DEPARTMENTS, DEPARTMENT_CONTRACT_VERSION, DepartmentHandoff,
    DepartmentId, DepartmentInput, DepartmentManifest, DepartmentOpsResult,
    ProviderUsage, artifact_sha256,
)

VERSION = "phase3c-replay-integrity-v1"
DEPARTMENT_FILES = (
    "input.json", "discovery.json", "fetch.json", "analysis.json",
    "evidence.json", "ops.json", "provider-usage.json", "handoff.json",
)
ROOT_FILES = (
    "department-manifest.json", "ops.json", "source-bundle.json",
    "00-search-plan.json", "01-search-results.json", "02-fetch.json",
    "03-analysis-output.json", "analysis-failures.json",
    "provider-request-events.json", "final-review.json",
    "event-threads.json", "judgment-reviews.json", "published-source-history.json",
)
OPTIONAL_FILES = {"04-digest.txt", "05-a-share.json"}
REQUIRED_FILES = set(ROOT_FILES) | {
    f"departments/{department.value}/{name}"
    for department in DepartmentId for name in DEPARTMENT_FILES
}
SEAL_NAME = "artifact-integrity.json"


class ReplayIntegrityError(ValueError):
    """Sanitized failure code; never embeds source text or credentials."""


def _read(run_dir: Path, relative: str) -> bytes:
    # Callers pass only fixed allowlisted relative paths.
    path = run_dir / relative
    if run_dir.is_symlink() or any(part.is_symlink() for part in (path, *path.parents)):
        raise ReplayIntegrityError("REPLAY_SYMLINK_REJECTED")
    try:
        return path.read_bytes()
    except OSError:
        raise ReplayIntegrityError("REPLAY_ARTIFACT_MISSING") from None


def _json(payload: bytes):
    try:
        return json.loads(payload)
    except (ValueError, UnicodeError):
        raise ReplayIntegrityError("REPLAY_INVALID_JSON") from None


def _contracts(snapshot: dict[str, bytes]) -> DepartmentManifest:
    try:
        manifest = DepartmentManifest.model_validate(_json(snapshot["department-manifest.json"]))
        if len(manifest.entries) != len(DepartmentId) or {
            entry.department_id for entry in manifest.entries
        } != set(DepartmentId):
            raise ReplayIntegrityError("REPLAY_DEPARTMENT_SET_MISMATCH")
        for entry in manifest.entries:
            prefix = f"departments/{entry.department_id.value}/"
            if entry.handoff_path != prefix + "handoff.json":
                raise ReplayIntegrityError("REPLAY_HANDOFF_PATH_MISMATCH")
            values = {name: _json(snapshot[prefix + name]) for name in DEPARTMENT_FILES}
            handoff = DepartmentHandoff.model_validate(values["handoff.json"])
            ops = DepartmentOpsResult.model_validate(values["ops.json"])
            usage = ProviderUsage.model_validate(values["provider-usage.json"])
            if (handoff.run_id != manifest.run_id
                    or handoff.department_id != entry.department_id
                    or ops.department_id != entry.department_id
                    or handoff.status != entry.status or ops.status != entry.status):
                raise ReplayIntegrityError("REPLAY_IDENTITY_MISMATCH")
            if artifact_sha256(values["handoff.json"]) != entry.handoff_sha256:
                raise ReplayIntegrityError("REPLAY_HANDOFF_HASH_MISMATCH")
            for filename, expected in (
                ("input.json", handoff.input_sha256), ("analysis.json", handoff.output_sha256),
                ("evidence.json", handoff.evidence_sha256), ("ops.json", handoff.ops_sha256),
            ):
                if artifact_sha256(values[filename]) != expected:
                    raise ReplayIntegrityError("REPLAY_REFERENCE_HASH_MISMATCH")
            if entry.department_id in CONTENT_DEPARTMENTS:
                department_input = DepartmentInput.model_validate(values["input.json"])
                if (department_input.run_id != manifest.run_id
                        or department_input.department_id != entry.department_id
                        or department_input.request_budget != usage.request_budget
                        or department_input.model_name != handoff.model_name
                        or department_input.base_prompt_version != handoff.base_prompt_version
                        or department_input.department_prompt_version != handoff.department_prompt_version):
                    raise ReplayIntegrityError("REPLAY_INPUT_MISMATCH")
            elif values["input.json"].get("contract_version") != DEPARTMENT_CONTRACT_VERSION:
                raise ReplayIntegrityError("REPLAY_STAGE_CONTRACT_MISMATCH")
        root_ops = _json(snapshot["ops.json"])
        if type(root_ops.get("passed")) is not bool or root_ops["passed"] != manifest.delivery_allowed:
            raise ReplayIntegrityError("REPLAY_DELIVERY_MISMATCH")
        if _json(snapshot["source-bundle.json"]).get("run_id") != manifest.run_id:
            raise ReplayIntegrityError("REPLAY_RUN_MISMATCH")
        return manifest
    except ReplayIntegrityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ReplayIntegrityError("REPLAY_CONTRACT_INVALID") from None


def seal_run(run_dir: Path) -> None:
    """Called once by generation, after all Phase 3C artifacts have been written."""
    paths = REQUIRED_FILES | {name for name in OPTIONAL_FILES if (run_dir / name).exists()}
    snapshot = {name: _read(run_dir, name) for name in sorted(paths)}
    manifest = _contracts(snapshot)
    seal = {
        "version": VERSION, "run_id": manifest.run_id,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in snapshot.items()},
    }
    with (run_dir / SEAL_NAME).open("x", encoding="utf-8") as handle:
        json.dump(seal, handle, sort_keys=True, indent=2)
        handle.write("\n")


def _verified_snapshot(run_dir: Path) -> tuple[dict, dict[str, bytes]]:
    if not (run_dir / SEAL_NAME).exists():
        raise ReplayIntegrityError("REPLAY_LEGACY_UNSEALED")
    seal_bytes = _read(run_dir, SEAL_NAME)
    seal = _json(seal_bytes)
    if not isinstance(seal, dict) or seal.get("version") != VERSION:
        raise ReplayIntegrityError("REPLAY_SEAL_VERSION_INVALID")
    files = seal.get("files")
    if not isinstance(files, dict) or not REQUIRED_FILES <= files.keys() or not files.keys() <= REQUIRED_FILES | OPTIONAL_FILES:
        raise ReplayIntegrityError("REPLAY_FILE_SET_MISMATCH")
    if files.keys() != REQUIRED_FILES | {name for name in OPTIONAL_FILES if (run_dir / name).exists()}:
        raise ReplayIntegrityError("REPLAY_FILE_SET_MISMATCH")
    snapshot = {name: _read(run_dir, name) for name in sorted(files)}
    for name, payload in snapshot.items():
        if hashlib.sha256(payload).hexdigest() != files[name]:
            raise ReplayIntegrityError("REPLAY_ARTIFACT_HASH_MISMATCH")
    manifest = _contracts(snapshot)
    if seal.get("run_id") != manifest.run_id:
        raise ReplayIntegrityError("REPLAY_RUN_MISMATCH")
    report = {
        "version": VERSION, "run_id": manifest.run_id, "integrity_passed": True,
        "delivery_allowed": manifest.delivery_allowed, "verified_artifacts": len(files),
        "network_calls": 0, "llm_calls": 0, "smtp_calls": 0,
        "source_seal_sha256": hashlib.sha256(seal_bytes).hexdigest(),
    }
    snapshot[SEAL_NAME] = seal_bytes
    return report, snapshot


def verify_run(run_dir: Path) -> dict:
    return _verified_snapshot(run_dir)[0]


def replay_run(run_dir: Path, target: Path) -> dict:
    """Validate everything before writing, then publish a cached byte snapshot."""
    report, snapshot = _verified_snapshot(run_dir)
    source, destination = run_dir.resolve(), target.resolve()
    if target.exists() or target.is_symlink() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ReplayIntegrityError("REPLAY_TARGET_UNSAFE")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".phase3c-replay-", dir=target.parent))
    try:
        for name, data in snapshot.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (staging / "replay-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        # Reserve destination without overwriting even an empty existing directory.
        target.mkdir()
        try:
            os.replace(staging, target)
        except OSError:
            target.rmdir()
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return report
