from pathlib import Path

from app.daily_ai.global_contracts import GlobalAiAnalysisContract
from app.daily_ai.global_ops import (
    check_global_channel,
    neutrality_violations,
    sourcing_matches_tier,
    sourcing_tier_diagnostics,
)
from app.web_discovery.source_policy import load_source_policy


POLICY_PATH = Path(__file__).parents[2] / "config/daily_ai/daily_ai_source_policy.yaml"


def analysis(
    *,
    url: str,
    sourcing: str,
    original_source: str = "",
    score: int = 70,
) -> GlobalAiAnalysisContract:
    return GlobalAiAnalysisContract(
        headline="测试AI产业链事件",
        sector="CHIPS_COMPUTE",
        importance_criterion="SUPPLY_CHAIN_SHIFT",
        chain_mapping=[],
        transmission_path="供应链变化 → 需求验证",
        region_section="GLOBAL",
        region_basis="事件主体位于全球市场",
        what_happened="公司发布了可核验的产业链更新。",
        why_it_matters="该更新提供了新的供应链事实。",
        contains_verifiable_numbers=False,
        sourcing=sourcing,
        original_source=original_source,
        novelty="NEW_FACT",
        announced_vs_delivered="DELIVERED",
        risks_and_counterevidence=["后续影响仍需验证"],
        evidence=[{"quote": "公司发布了可核验的产业链更新", "source_url": url}],
        uncertainties=["后续规模未知"],
        importance_score=score,
        importance_reason="存在新的供应链事实",
        investment_advice=False,
    )


def test_sourcing_tier_rule_accepts_t1_firsthand_and_t2_secondhand():
    policy = load_source_policy(POLICY_PATH)
    t1 = analysis(url="https://openai.com/index/test", sourcing="FIRSTHAND_OFFICIAL")
    t2 = analysis(
        url="https://www.tomshardware.com/tech-industry/test",
        sourcing="SECONDHAND_REPORT",
        original_source="公司官方公告",
    )

    assert sourcing_matches_tier(t1, policy)
    assert sourcing_matches_tier(t2, policy)


def test_sourcing_tier_mismatch_is_diagnosable_and_blocks_ops_backstop():
    policy = load_source_policy(POLICY_PATH)
    item = analysis(
        url="https://www.tomshardware.com/tech-industry/test",
        sourcing="FIRSTHAND_OFFICIAL",
    )
    diagnostics = sourcing_tier_diagnostics(item, policy)

    assert diagnostics["actual_sourcing"] == "FIRSTHAND_OFFICIAL"
    assert diagnostics["expected_sourcing"] == "SECONDHAND_REPORT"
    assert diagnostics["evidence"][0]["tier"] == "T2"
    assert not sourcing_matches_tier(item, policy)

    ops = check_global_channel(
        analyses=[item],
        raw_text_by_url={
            "https://www.tomshardware.com/tech-industry/test":
                "公司发布了可核验的产业链更新"
        },
        digest_items=[],
        policy=policy,
    )

    assert not ops.passed
    assert ops.errors == ["firsthand_tier_consistent"]
    assert "actual=FIRSTHAND_OFFICIAL" in ops.offenders["firsthand_tier_consistent"][0]
    assert "expected=SECONDHAND_REPORT" in ops.offenders["firsthand_tier_consistent"][0]


def test_valid_published_item_passes_global_ops():
    policy = load_source_policy(POLICY_PATH)
    url = "https://www.tomshardware.com/tech-industry/test"
    item = analysis(
        url=url,
        sourcing="SECONDHAND_REPORT",
        original_source="公司官方公告",
    )

    ops = check_global_channel(
        analyses=[item],
        raw_text_by_url={url: "公司发布了可核验的产业链更新"},
        digest_items=[{"url": url, "sector": "CHIPS_COMPUTE"}],
        policy=policy,
    )

    assert ops.passed
    assert not ops.offenders


def test_attempted_but_unusable_analysis_provider_fails_digest_ops():
    policy = load_source_policy(POLICY_PATH)

    failed = check_global_channel(
        analyses=[],
        raw_text_by_url={},
        digest_items=[],
        policy=policy,
        analysis_provider_required=True,
        analysis_attempts=3,
        provider_validated_outputs=0,
    )
    no_sources = check_global_channel(
        analyses=[],
        raw_text_by_url={},
        digest_items=[],
        policy=policy,
        analysis_provider_required=True,
        analysis_attempts=0,
        provider_validated_outputs=0,
    )

    assert not failed.passed
    assert failed.errors == ["analysis_provider_usable"]
    assert no_sources.passed


def test_halted_provider_with_unprocessed_candidates_fails_coverage_gate():
    policy = load_source_policy(POLICY_PATH)
    ops = check_global_channel(
        analyses=[],
        raw_text_by_url={},
        digest_items=[],
        policy=policy,
        analysis_provider_required=True,
        fetched_count=38,
        analysis_candidate_count=38,
        analysis_attempts=5,
        provider_validated_outputs=1,
        provider_halted=True,
        provider_failure_codes=["GEMINI_SERVER_ERROR", "GEMINI_RATE_LIMITED"],
    )

    assert not ops.passed
    assert not ops.checks["analysis_coverage_complete"]
    assert ops.audit["unprocessed_candidate_count"] == 33
    assert ops.audit["provider_halted"] is True
    assert ops.audit["provider_failure_codes"] == [
        "GEMINI_RATE_LIMITED", "GEMINI_SERVER_ERROR"
    ]


def test_healthy_full_coverage_with_one_qualifying_item_still_passes():
    policy = load_source_policy(POLICY_PATH)
    url = "https://openai.com/index/test"
    item = analysis(url=url, sourcing="FIRSTHAND_OFFICIAL")
    ops = check_global_channel(
        analyses=[item],
        raw_text_by_url={url: "公司发布了可核验的产业链更新"},
        digest_items=[{"url": url, "sector": "CHIPS_COMPUTE"}],
        policy=policy,
        analysis_provider_required=True,
        fetched_count=38,
        analysis_candidate_count=38,
        analysis_attempts=38,
        provider_validated_outputs=38,
        provider_halted=False,
    )

    assert ops.passed
    assert ops.checks["analysis_coverage_complete"]
    assert ops.audit["unprocessed_candidate_count"] == 0


def test_promotional_benefit_framing_is_rejected_as_non_neutral():
    policy = load_source_policy(POLICY_PATH)
    url = "https://openai.com/index/test"
    item = analysis(url=url, sourcing="FIRSTHAND_OFFICIAL").model_copy(
        update={"why_it_matters": "该项目为相关公司注入动力并带来极高确定性。"}
    )

    assert neutrality_violations(item) == ["极高确定性", "注入动力"]
    ops = check_global_channel(
        analyses=[item],
        raw_text_by_url={url: "公司发布了可核验的产业链更新"},
        digest_items=[{"url": url, "sector": "CHIPS_COMPUTE"}],
        policy=policy,
    )

    assert not ops.passed
    assert "neutral_language" in ops.errors
