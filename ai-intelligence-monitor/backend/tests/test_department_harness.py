import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.daily_ai.department_harness import (
    CONTENT_DEPARTMENTS,
    DepartmentCandidate,
    DepartmentId,
    DepartmentInput,
    DepartmentStatus,
    ProviderUsage,
    allocate_department_request_budgets,
    artifact_sha256,
    delivery_gate,
    department_status,
    evidence_review,
    replay_department,
    write_content_department_artifacts,
)
from app.daily_ai.global_contracts import GlobalAiAnalysisContract


SOURCE_URL = "https://example.com/official/update"
RAW_TEXT = "The company delivered 100 AI systems and expanded capacity."
FIXTURES = Path(__file__).parents[2] / "fixtures/departments"


def analysis(**overrides) -> GlobalAiAnalysisContract:
    payload = {
        "headline": "公司交付100套AI系统",
        "sector": "INFRASTRUCTURE",
        "importance_criterion": "CAPACITY_CHANGE",
        "chain_mapping": [{"sector": "INFRASTRUCTURE", "companies": ["Example"]}],
        "transmission_path": "系统交付 → 基础设施容量变化",
        "region_section": "GLOBAL",
        "region_basis": "事件主体位于美国",
        "what_happened": "公司交付了100套AI系统。",
        "why_it_matters": "该事件形成可核验的基础设施容量变化。",
        "contains_verifiable_numbers": True,
        "sourcing": "FIRSTHAND_OFFICIAL",
        "original_source": "",
        "novelty": "NEW_FACT",
        "announced_vs_delivered": "DELIVERED",
        "risks_and_counterevidence": ["后续利用率仍待观察"],
        "evidence": [{"quote": "delivered 100 AI systems", "source_url": SOURCE_URL}],
        "uncertainties": ["部署节奏未披露"],
        "importance_score": 70,
        "importance_reason": "存在实际交付证据",
        "investment_advice": False,
    }
    payload.update(overrides)
    return GlobalAiAnalysisContract.model_validate(payload)


def department_input() -> DepartmentInput:
    return DepartmentInput(
        run_id="fixture-run",
        department_id=DepartmentId.GLOBAL,
        department_prompt_version="global-supply-chain-department-v1",
        model_name="gemini-3.5-flash",
        request_budget=2,
        candidates=(
            DepartmentCandidate(
                candidate_id="candidate-1",
                canonical_url=SOURCE_URL,
                title="Official update",
                source_domain="example.com",
                source_tier="T1",
                event_cluster_key="event-1",
                raw_text=RAW_TEXT,
            ),
        ),
    )


def test_contracts_are_immutable_and_artifacts_are_write_once(tmp_path):
    item = department_input()
    with pytest.raises(ValidationError):
        item.model_name = "changed"

    handoff = write_content_department_artifacts(
        run_dir=tmp_path,
        department_input=item,
        discovery=[],
        fetches=[],
        analyses=[analysis().model_dump(mode="json")],
        evidence=evidence_review(
            [analysis()],
            candidate_id_by_url={SOURCE_URL: "candidate-1"},
            raw_text_by_url={SOURCE_URL: RAW_TEXT},
        ),
        provider_usage=ProviderUsage(
            request_budget=2,
            request_count=1,
            candidate_attempts=1,
            validated_outputs=1,
            models_used=("gemini-3.5-flash",),
        ),
        status=DepartmentStatus.HEALTHY,
    )
    original_hash = artifact_sha256(handoff)
    with pytest.raises(FileExistsError):
        write_content_department_artifacts(
            run_dir=tmp_path,
            department_input=item,
            discovery=[],
            fetches=[],
            analyses=[],
            evidence=[],
            provider_usage=ProviderUsage(
                request_budget=2,
                request_count=0,
                candidate_attempts=0,
                validated_outputs=0,
            ),
            status=DepartmentStatus.HEALTHY_EMPTY,
        )
    assert artifact_sha256(handoff) == original_hash


def test_evidence_review_rejects_mismatch_without_rewriting_analysis():
    item = analysis()
    before = artifact_sha256(item)
    decisions = evidence_review(
        [item],
        candidate_id_by_url={SOURCE_URL: "candidate-1"},
        raw_text_by_url={SOURCE_URL: "different source text"},
    )

    assert decisions[0].decision == "FAIL"
    assert decisions[0].failure_codes == ("EVIDENCE_QUOTE_MISMATCH",)
    assert artifact_sha256(item) == before


@pytest.mark.parametrize(
    ("attempts", "halted", "failures", "accepted", "expected"),
    [
        (1, False, [], 1, DepartmentStatus.HEALTHY),
        (1, False, [], 0, DepartmentStatus.HEALTHY_EMPTY),
        (0, False, [], 0, DepartmentStatus.DEGRADED),
        (1, True, [], 1, DepartmentStatus.DEGRADED),
        (1, False, ["GEMINI_TIMEOUT"], 0, DepartmentStatus.DEGRADED),
    ],
)
def test_department_status_never_hides_provider_or_coverage_failure(
    attempts, halted, failures, accepted, expected
):
    assert department_status(
        analysis_candidate_count=1,
        candidate_attempts=attempts,
        accepted_count=accepted,
        provider_halted=halted,
        failure_codes=failures,
    ) == expected


def test_delivery_gate_blocks_any_required_desk_or_smtp_failure():
    statuses = {department: DepartmentStatus.HEALTHY for department in CONTENT_DEPARTMENTS}
    statuses[DepartmentId.CHINA] = DepartmentStatus.DEGRADED
    result = delivery_gate(
        content_statuses=statuses,
        evidence_status=DepartmentStatus.HEALTHY,
        editorial_status=DepartmentStatus.HEALTHY,
        editorial_ops_passed=True,
        provider_halted=False,
        unprocessed_candidate_count=0,
        render_passed=True,
        idempotency_passed=True,
        smtp_configuration_valid=False,
    )

    assert result.status == DepartmentStatus.BLOCKED
    assert result.passed is False
    assert result.checks["china_complete"] is False
    assert result.checks["smtp_configuration_valid"] is False


def test_department_budgets_are_independent_and_never_exceed_total():
    counts = {
        DepartmentId.CHINA: 6,
        DepartmentId.GLOBAL: 23,
        DepartmentId.MACRO: 0,
    }
    budgets = allocate_department_request_budgets(35, candidate_counts=counts)

    assert sum(budgets.values()) == 35
    assert budgets[DepartmentId.CHINA] >= counts[DepartmentId.CHINA]
    assert budgets[DepartmentId.GLOBAL] >= counts[DepartmentId.GLOBAL]
    assert budgets[DepartmentId.MACRO] == 0


def test_department_replay_is_frozen_and_makes_zero_external_calls(tmp_path):
    source_run = tmp_path / "source"
    write_content_department_artifacts(
        run_dir=source_run,
        department_input=department_input(),
        discovery=[],
        fetches=[],
        analyses=[analysis().model_dump(mode="json")],
        evidence=[],
        provider_usage=ProviderUsage(
            request_budget=2,
            request_count=1,
            candidate_attempts=1,
            validated_outputs=1,
        ),
        status=DepartmentStatus.HEALTHY,
    )
    source_dir = source_run / "departments/global"
    source_hashes = {
        path.name: path.read_bytes() for path in source_dir.glob("*.json")
    }

    replay = replay_department(source_dir, tmp_path / "replay")

    assert replay["network_calls"] == 0
    assert replay["llm_calls"] == 0
    assert source_hashes == {
        path.name: path.read_bytes() for path in source_dir.glob("*.json")
    }


def test_all_department_fixtures_are_versioned_and_frozen():
    expected = {
        "china",
        "global",
        "macro",
        "evidence-review",
        "editorial",
        "delivery",
    }
    found = {path.stem for path in FIXTURES.glob("*.json")}
    assert found == expected
    for path in FIXTURES.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["contract_version"] == "phase3c-department-contract-v1"
        assert payload["department_id"] == path.stem
        assert payload["frozen"] is True
