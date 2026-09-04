"""Immutable Phase 3C department contracts and artifact harnesses.

This module is additive. It does not import or mutate the frozen Phase 0-2
Harness, state machine, Evidence, Replay, Signal, or Ops persistence. Phase 3C
departments exchange only versioned JSON artifacts on disk.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.daily_ai.global_contracts import GlobalAiAnalysisContract, quote_matches


DEPARTMENT_CONTRACT_VERSION = "phase3c-department-contract-v1"
BASE_PROMPT_VERSION = "daily-global-ai-analysis-v9"


class ImmutableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DepartmentId(StrEnum):
    CHINA = "china"
    GLOBAL = "global"
    MACRO = "macro"
    EVIDENCE_REVIEW = "evidence-review"
    EDITORIAL = "editorial"
    DELIVERY = "delivery"


CONTENT_DEPARTMENTS = (
    DepartmentId.CHINA,
    DepartmentId.GLOBAL,
    DepartmentId.MACRO,
)


class DepartmentStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    HEALTHY = "HEALTHY"
    HEALTHY_EMPTY = "HEALTHY_EMPTY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


HEALTHY_STATUSES = {DepartmentStatus.HEALTHY, DepartmentStatus.HEALTHY_EMPTY}


def allocate_department_request_budgets(
    max_requests: int,
    *,
    candidate_counts: dict[DepartmentId, int],
) -> dict[DepartmentId, int]:
    """Allocate independent desk budgets within one immutable run ceiling."""

    budgets = {department: 0 for department in CONTENT_DEPARTMENTS}
    total_candidates = sum(candidate_counts.values())
    if max_requests <= 0 or total_candidates <= 0:
        return budgets

    remaining = max_requests
    if total_candidates <= max_requests:
        budgets.update(candidate_counts)
        remaining -= total_candidates
    else:
        for department in CONTENT_DEPARTMENTS:
            count = candidate_counts[department]
            if count and remaining:
                share = max(1, (max_requests * count) // total_candidates)
                granted = min(count, share, remaining)
                budgets[department] = granted
                remaining -= granted

    weighted = sorted(
        CONTENT_DEPARTMENTS,
        key=lambda department: (-candidate_counts[department], department.value),
    )
    while remaining:
        eligible = [
            department
            for department in weighted
            if candidate_counts[department] > 0
        ]
        if not eligible:
            break
        for department in eligible:
            if not remaining:
                break
            budgets[department] += 1
            remaining -= 1
    return budgets


class DepartmentCandidate(ImmutableModel):
    candidate_id: str
    canonical_url: str
    title: str
    source_domain: str
    source_tier: Literal["T1", "T2"]
    event_cluster_key: str
    content_sha256: str | None = None
    raw_text: str = ""


class DepartmentInput(ImmutableModel):
    contract_version: Literal["phase3c-department-contract-v1"] = (
        DEPARTMENT_CONTRACT_VERSION
    )
    run_id: str
    department_id: DepartmentId
    base_prompt_version: str = BASE_PROMPT_VERSION
    department_prompt_version: str
    model_name: str
    request_budget: int = Field(ge=0, le=40)
    candidates: tuple[DepartmentCandidate, ...] = ()


class ProviderUsage(ImmutableModel):
    request_budget: int = Field(ge=0, le=40)
    request_count: int = Field(ge=0)
    candidate_attempts: int = Field(ge=0)
    validated_outputs: int = Field(ge=0)
    models_used: tuple[str, ...] = ()
    model_transitions: tuple[dict[str, str], ...] = ()
    provider_halted: bool = False
    failure_codes: tuple[str, ...] = ()


class EvidenceDecision(ImmutableModel):
    candidate_id: str
    source_url: str
    decision: Literal["PASS", "FAIL"]
    failure_codes: tuple[str, ...] = ()
    analysis_sha256: str


class DepartmentOpsResult(ImmutableModel):
    contract_version: Literal["phase3c-department-contract-v1"] = (
        DEPARTMENT_CONTRACT_VERSION
    )
    department_id: DepartmentId
    status: DepartmentStatus
    passed: bool
    checks: dict[str, bool]
    errors: tuple[str, ...] = ()


class DepartmentHandoff(ImmutableModel):
    contract_version: Literal["phase3c-department-contract-v1"] = (
        DEPARTMENT_CONTRACT_VERSION
    )
    run_id: str
    department_id: DepartmentId
    status: DepartmentStatus
    input_sha256: str
    output_sha256: str
    evidence_sha256: str
    ops_sha256: str
    downstream_consumer: str
    model_name: str
    models_used: tuple[str, ...] = ()
    base_prompt_version: str
    department_prompt_version: str


class DepartmentManifestEntry(ImmutableModel):
    department_id: DepartmentId
    status: DepartmentStatus
    handoff_path: str
    handoff_sha256: str


class DepartmentManifest(ImmutableModel):
    contract_version: Literal["phase3c-department-contract-v1"] = (
        DEPARTMENT_CONTRACT_VERSION
    )
    run_id: str
    entries: tuple[DepartmentManifestEntry, ...]
    delivery_allowed: bool


def canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")

    def encode(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        raise TypeError(f"unsupported artifact value: {type(item).__name__}")

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=encode,
    ).encode("utf-8")


def artifact_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class DepartmentArtifactStore:
    """Write-once store for one department's immutable JSON handoff."""

    def __init__(self, run_dir: Path, department_id: DepartmentId):
        self.directory = run_dir / "departments" / department_id.value
        self.directory.mkdir(parents=True, exist_ok=True)

    def write_once(self, name: str, value: Any) -> tuple[str, str]:
        path = self.directory / name
        payload = canonical_json(value)
        with path.open("xb") as handle:
            handle.write(payload + b"\n")
        return str(path), hashlib.sha256(payload).hexdigest()


PROVIDER_FAILURE_PREFIXES = (
    "GEMINI_TIMEOUT",
    "GEMINI_NETWORK",
    "GEMINI_SERVER",
    "GEMINI_RATE",
    "GEMINI_DAILY",
    "GEMINI_REQUEST_BUDGET",
    "GEMINI_RUN_DEADLINE",
    "GEMINI_HTTP_401",
    "GEMINI_HTTP_403",
    "GEMINI_CONFIGURATION",
)


def department_status(
    *,
    analysis_candidate_count: int,
    candidate_attempts: int,
    accepted_count: int,
    provider_halted: bool,
    failure_codes: list[str],
) -> DepartmentStatus:
    unprocessed = max(0, analysis_candidate_count - candidate_attempts)
    provider_failures = [
        code
        for code in failure_codes
        if code.startswith(PROVIDER_FAILURE_PREFIXES)
    ]
    if provider_halted or unprocessed or provider_failures:
        return DepartmentStatus.DEGRADED
    return DepartmentStatus.HEALTHY if accepted_count else DepartmentStatus.HEALTHY_EMPTY


def evidence_review(
    analyses: list[GlobalAiAnalysisContract],
    *,
    candidate_id_by_url: dict[str, str],
    raw_text_by_url: dict[str, str],
) -> list[EvidenceDecision]:
    """Review without rewriting analysis facts or Evidence quotes."""

    decisions: list[EvidenceDecision] = []
    for analysis in analyses:
        serialized = analysis.model_dump(mode="json")
        digest = artifact_sha256(serialized)
        source_url = str(analysis.evidence[0].source_url)
        failures: list[str] = []
        for entry in analysis.evidence:
            entry_url = str(entry.source_url)
            if entry_url != source_url:
                failures.append("SOURCE_URL_MISMATCH")
            if not quote_matches(entry.quote, raw_text_by_url.get(entry_url, "")):
                failures.append("EVIDENCE_QUOTE_MISMATCH")
        decisions.append(
            EvidenceDecision(
                candidate_id=candidate_id_by_url.get(source_url, source_url),
                source_url=source_url,
                decision="FAIL" if failures else "PASS",
                failure_codes=tuple(sorted(set(failures))),
                analysis_sha256=digest,
            )
        )
        if artifact_sha256(analysis.model_dump(mode="json")) != digest:
            raise RuntimeError("Evidence Review mutated an analysis")
    return decisions


def delivery_gate(
    *,
    content_statuses: dict[DepartmentId, DepartmentStatus],
    evidence_status: DepartmentStatus,
    editorial_status: DepartmentStatus,
    editorial_ops_passed: bool,
    provider_halted: bool,
    unprocessed_candidate_count: int,
    render_passed: bool,
    idempotency_passed: bool,
    smtp_configuration_valid: bool,
) -> DepartmentOpsResult:
    checks = {
        "china_complete": content_statuses.get(DepartmentId.CHINA)
        in HEALTHY_STATUSES,
        "global_complete": content_statuses.get(DepartmentId.GLOBAL)
        in HEALTHY_STATUSES,
        "macro_complete": content_statuses.get(DepartmentId.MACRO)
        in HEALTHY_STATUSES,
        "evidence_review_complete": evidence_status in HEALTHY_STATUSES,
        "editorial_complete": editorial_status in HEALTHY_STATUSES,
        "editorial_ops_passed": editorial_ops_passed,
        "analysis_coverage_complete": not (
            provider_halted and unprocessed_candidate_count > 0
        ),
        "render_passed": render_passed,
        "idempotency_passed": idempotency_passed,
        "smtp_configuration_valid": smtp_configuration_valid,
    }
    errors = tuple(name for name, passed in checks.items() if not passed)
    return DepartmentOpsResult(
        department_id=DepartmentId.DELIVERY,
        status=DepartmentStatus.HEALTHY if not errors else DepartmentStatus.BLOCKED,
        passed=not errors,
        checks=checks,
        errors=errors,
    )


def write_content_department_artifacts(
    *,
    run_dir: Path,
    department_input: DepartmentInput,
    discovery: list[dict],
    fetches: list[dict],
    analyses: list[dict],
    evidence: list[EvidenceDecision],
    provider_usage: ProviderUsage,
    status: DepartmentStatus,
    downstream_consumer: DepartmentId = DepartmentId.EVIDENCE_REVIEW,
) -> DepartmentHandoff:
    store = DepartmentArtifactStore(run_dir, department_input.department_id)
    _, input_sha = store.write_once("input.json", department_input)
    store.write_once("discovery.json", discovery)
    store.write_once("fetch.json", fetches)
    _, output_sha = store.write_once("analysis.json", analyses)
    _, evidence_sha = store.write_once("evidence.json", evidence)
    checks = {
        "contract_valid": True,
        "request_budget_respected": provider_usage.request_count
        <= provider_usage.request_budget,
        "provider_not_halted": not provider_usage.provider_halted,
        "all_candidates_processed": provider_usage.candidate_attempts
        >= len(department_input.candidates),
    }
    ops = DepartmentOpsResult(
        department_id=department_input.department_id,
        status=status,
        passed=status in HEALTHY_STATUSES and all(checks.values()),
        checks=checks,
        errors=tuple(name for name, passed in checks.items() if not passed),
    )
    _, ops_sha = store.write_once("ops.json", ops)
    store.write_once("provider-usage.json", provider_usage)
    handoff = DepartmentHandoff(
        run_id=department_input.run_id,
        department_id=department_input.department_id,
        status=status,
        input_sha256=input_sha,
        output_sha256=output_sha,
        evidence_sha256=evidence_sha,
        ops_sha256=ops_sha,
        downstream_consumer=downstream_consumer.value,
        model_name=department_input.model_name,
        models_used=provider_usage.models_used,
        base_prompt_version=department_input.base_prompt_version,
        department_prompt_version=department_input.department_prompt_version,
    )
    store.write_once("handoff.json", handoff)
    return handoff


def write_stage_department_artifacts(
    *,
    run_dir: Path,
    run_id: str,
    department_id: DepartmentId,
    status: DepartmentStatus,
    input_payload: dict,
    output_payload: Any,
    ops: DepartmentOpsResult,
    downstream_consumer: str,
    model_name: str = "deterministic",
    base_prompt_version: str = BASE_PROMPT_VERSION,
    department_prompt_version: str = "deterministic-v1",
) -> DepartmentHandoff:
    """Write the common artifact shape for review/editorial/delivery stages."""

    store = DepartmentArtifactStore(run_dir, department_id)
    _, input_sha = store.write_once("input.json", input_payload)
    store.write_once("discovery.json", [])
    store.write_once("fetch.json", [])
    _, output_sha = store.write_once("analysis.json", output_payload)
    _, evidence_sha = store.write_once(
        "evidence.json",
        output_payload if department_id == DepartmentId.EVIDENCE_REVIEW else [],
    )
    _, ops_sha = store.write_once("ops.json", ops)
    store.write_once(
        "provider-usage.json",
        ProviderUsage(
            request_budget=0,
            request_count=0,
            candidate_attempts=0,
            validated_outputs=0,
            models_used=(),
        ),
    )
    handoff = DepartmentHandoff(
        run_id=run_id,
        department_id=department_id,
        status=status,
        input_sha256=input_sha,
        output_sha256=output_sha,
        evidence_sha256=evidence_sha,
        ops_sha256=ops_sha,
        downstream_consumer=downstream_consumer,
        model_name=model_name,
        models_used=(),
        base_prompt_version=base_prompt_version,
        department_prompt_version=department_prompt_version,
    )
    store.write_once("handoff.json", handoff)
    return handoff


def replay_department(source_dir: Path, target_run_dir: Path) -> dict[str, Any]:
    """Frozen artifact replay: validates and copies JSON, calls no network/LLM."""

    department_id = DepartmentId(source_dir.name)
    store = DepartmentArtifactStore(target_run_dir, department_id)
    copied: list[str] = []
    for name in (
        "input.json",
        "discovery.json",
        "fetch.json",
        "analysis.json",
        "evidence.json",
        "ops.json",
        "provider-usage.json",
        "handoff.json",
    ):
        path = source_dir / name
        value = json.loads(path.read_text(encoding="utf-8"))
        if name == "input.json":
            if department_id in CONTENT_DEPARTMENTS:
                DepartmentInput.model_validate(value)
            elif value.get("contract_version") != DEPARTMENT_CONTRACT_VERSION:
                raise ValueError("stage input contract version mismatch")
        elif name == "ops.json":
            DepartmentOpsResult.model_validate(value)
        elif name == "handoff.json":
            DepartmentHandoff.model_validate(value)
        store.write_once(name, value)
        copied.append(name)
    replay = {
        "department_id": department_id.value,
        "network_calls": 0,
        "llm_calls": 0,
        "source_handoff_sha256": hashlib.sha256(
            (source_dir / "handoff.json").read_bytes()
        ).hexdigest(),
        "copied_artifacts": copied,
    }
    store.write_once("replay.json", replay)
    return replay
