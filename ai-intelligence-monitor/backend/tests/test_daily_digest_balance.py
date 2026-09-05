from pathlib import Path
from types import SimpleNamespace

import yaml

from app.daily_ai.dedup import cluster_texts
from app.daily_ai.global_contracts import GlobalAiAnalysisContract
from app.daily_ai.global_digest import render_global_digest, select_global_digest
from app.daily_ai.gemini_analysis import GeminiGlobalAiAnalysisAdapter
from app.scripts.daily_global_ai import _take_domain_diverse
from app.web_discovery.contracts import ClassifiedTier
from app.web_discovery.source_policy import classify_url, load_source_policy


CONFIG_DIR = Path(__file__).parents[2] / "config/daily_ai"


def analysis(*, headline="公司宣布扩产", announced="ANNOUNCED_ONLY"):
    return GlobalAiAnalysisContract(
        headline=headline,
        sector="INFRASTRUCTURE",
        importance_criterion="MAJOR_ORDER_CAPEX",
        chain_mapping=[],
        transmission_path="项目公告 → 数据中心建设",
        region_section="GLOBAL",
        region_basis="事件发生在全球市场",
        what_happened="公司宣布建设新的数据中心。",
        why_it_matters="该事项改变了基础设施项目规划。",
        contains_verifiable_numbers=False,
        sourcing="FIRSTHAND_OFFICIAL",
        original_source="",
        novelty="NEW_FACT",
        announced_vs_delivered=announced,
        risks_and_counterevidence=["融资安排尚未披露"],
        evidence=[{"quote": "公司宣布建设新的数据中心", "source_url": "https://openai.com/test"}],
        uncertainties=["实际开工日期未知"],
        importance_score=70,
        importance_reason="存在新的资本开支计划",
        investment_advice=False,
    )


def test_reader_digest_exposes_existing_risks_uncertainties_and_plan_status():
    items = select_global_digest([analysis()])
    rendered = render_global_digest(items)

    assert "⚠️ 风险与不确定性" in rendered
    assert "仅为已宣布计划" in rendered
    assert "融资安排尚未披露" in rendered
    assert "实际开工日期未知" in rendered
    assert "重要程度：70/100" in rendered


def test_text_digest_carries_runtime_model_and_prompt_metadata():
    rendered = render_global_digest(
        select_global_digest([analysis()]),
        analysis_model="gemini-3.5-flash",
        prompt_version=GeminiGlobalAiAnalysisAdapter.prompt_version,
    )

    assert "分析元数据：Gemini 3.5 Flash · Prompt v9" in rendered


def test_text_digest_discloses_quota_fallback_model_chain():
    rendered = render_global_digest(
        [],
        analysis_model=(
            "gemini-3.5-flash → gemini-3.5-flash-lite（额度降级）"
        ),
        prompt_version="daily-global-ai-analysis-v9",
    )

    assert (
        "Gemini 3.5 Flash → Gemini 3.5 Flash Lite（额度降级）"
        in rendered
    )


def test_same_company_safety_incident_is_clustered_across_different_wording():
    texts = {
        "a": "OpenAI因智能体越狱及安全风险暂停前沿模型Astra大量训练工作",
        "b": "OpenAI因Hugging Face安全事件收紧模型测试规范",
        "c": "OpenAI完成新一轮融资并扩建数据中心",
    }
    clusters = cluster_texts(texts)

    assert clusters["a"] == clusters["b"]
    assert clusters["a"] != clusters["c"]


def test_official_treasury_and_fed_feeds_are_t1_and_bounded():
    policy = load_source_policy(CONFIG_DIR / "daily_ai_source_policy.yaml")
    feeds = yaml.safe_load(
        (CONFIG_DIR / "rss_sources.yaml").read_text(encoding="utf-8")
    )["feeds"]
    macro = {item["name"]: item for item in feeds if item["name"].startswith(("treasury_", "fed_"))}

    assert set(macro) == {
        "treasury_offering_announcements",
        "treasury_auction_results",
        "fed_monetary_policy",
    }
    assert all(item["max_items"] <= 3 and item["prefilter"] is False for item in macro.values())
    for url in (
        "https://home.treasury.gov/test",
        "https://treasurydirect.gov/test",
        "https://www.federalreserve.gov/test",
    ):
        assert classify_url(url, policy)[0] == ClassifiedTier.T1


def test_source_cap_groups_subdomains_of_the_same_publisher():
    candidates = [
        SimpleNamespace(source_domain="nvidianews.nvidia.com", name="news"),
        SimpleNamespace(source_domain="blogs.nvidia.com", name="blog"),
        SimpleNamespace(source_domain="developer.nvidia.com", name="developer"),
        SimpleNamespace(source_domain="example.com", name="other"),
    ]

    selected = _take_domain_diverse(candidates, 3, cap=2)

    assert [item.name for item in selected] == ["news", "blog", "other"]


def test_zero_fetch_share_never_selects_an_extra_candidate():
    candidates = [SimpleNamespace(source_domain="example.com")]
    assert _take_domain_diverse(candidates, 0) == []
    assert _take_domain_diverse(candidates, -1) == []
    assert _take_domain_diverse(candidates, 1, cap=0) == []
