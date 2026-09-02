from pathlib import Path

from app.scripts.daily_global_ai import PROMPT_PATH
from app.scripts.daily_global_ai import MACRO_PROMPT_PATH


def test_daily_global_ai_uses_versioned_v9_prompt():
    assert PROMPT_PATH.name == "daily_global_ai_analysis_v9.txt"
    assert PROMPT_PATH.is_file()


def test_v9_prompt_calibrates_threshold_without_padding():
    prompt = Path(PROMPT_PATH).read_text(encoding="utf-8")

    assert "90-100 = 5 stars" in prompt
    assert "80-89 = 4 stars" in prompt
    assert "70-79 = 3 stars" in prompt
    assert "60-69 = 2 stars" in prompt
    assert "0-59 = 1 star" in prompt
    assert "output the numeric score only" in prompt
    assert "company fame alone never qualifies" in prompt
    assert "60-69" in prompt
    assert "directly changes the state of an AI supply-chain link" in prompt
    assert "never raise a score to fill a quota" in prompt
    assert "a day with few or zero qualifying items remains correct" in prompt
    assert "importance_score >= 60 REQUIRES a criterion other than NONE" in prompt


def test_v9_prompt_governs_risk_uncertainty_and_delivery_fields():
    prompt = Path(PROMPT_PATH).read_text(encoding="utf-8")

    assert "Include a company only when raw_text itself supports" in prompt
    assert "leave the companies list EMPTY" in prompt
    assert "risks_and_counterevidence —" in prompt
    assert "uncertainties —" in prompt
    assert "announced_vs_delivered —" in prompt
    assert "return an EMPTY list" in prompt
    assert "never write generic filler" in prompt
    assert "An official source is not automatically DELIVERED" in prompt


def test_macro_prompt_is_supplemental_and_v9_remains_the_primary_prompt():
    assert PROMPT_PATH.name == "daily_global_ai_analysis_v9.txt"
    assert MACRO_PROMPT_PATH.name == "daily_us_macro_analysis_v1.txt"
    prompt = MACRO_PROMPT_PATH.read_text(encoding="utf-8")
    assert "sector=MACRO_POLICY" in prompt
    assert "routine recurring auction" in prompt
    assert "must score below 60" in prompt
    assert "Never use importance_criterion=PRICE_MOVE" in prompt
    assert "PRICE_MOVE in the existing contract is reserved for product or commodity prices" in prompt
    assert "use DELIVERED only" in prompt
    assert "use ANNOUNCED_ONLY" in prompt
    assert "use UNKNOWN" in prompt
