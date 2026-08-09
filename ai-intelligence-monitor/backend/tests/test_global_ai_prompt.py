from pathlib import Path

from app.scripts.daily_global_ai import PROMPT_PATH


def test_daily_global_ai_uses_versioned_v8_prompt():
    assert PROMPT_PATH.name == "daily_global_ai_analysis_v8.txt"
    assert PROMPT_PATH.is_file()


def test_v8_prompt_calibrates_threshold_without_padding():
    prompt = Path(PROMPT_PATH).read_text(encoding="utf-8")

    assert "60-69" in prompt
    assert "directly changes the state of an AI supply-chain link" in prompt
    assert "never raise a score to fill a quota" in prompt
    assert "a day with few or zero qualifying items remains correct" in prompt
    assert "importance_score >= 60 REQUIRES a criterion other than NONE" in prompt
