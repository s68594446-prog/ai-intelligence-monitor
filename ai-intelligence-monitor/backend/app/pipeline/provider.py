from app.core.settings import Settings
from app.pipeline.adapters import AnalysisAdapter, RedTeamAdapter
from app.pipeline.deepseek_adapters import DeepSeekAnalysisAdapter, DeepSeekRedTeamAdapter
from app.pipeline.openai_adapters import OpenAIAnalysisAdapter, OpenAIRedTeamAdapter


def build_live_adapters(
    app_settings: Settings,
    coverage_snapshot: dict,
    thesis_snapshot: dict,
) -> tuple[AnalysisAdapter, RedTeamAdapter]:
    common = {
        "timeout_seconds": app_settings.llm_timeout_seconds,
        "max_retries": app_settings.llm_max_retries,
        "coverage_snapshot": coverage_snapshot,
        "thesis_snapshot": thesis_snapshot,
    }
    if app_settings.llm_provider == "deepseek":
        analysis = DeepSeekAnalysisAdapter(
            api_key=app_settings.deepseek_api_key,
            base_url=app_settings.deepseek_base_url,
            model=app_settings.deepseek_model,
            prompt_path=app_settings.analysis_prompt_path,
            **common,
        )
        red_team = DeepSeekRedTeamAdapter(
            api_key=app_settings.deepseek_api_key,
            base_url=app_settings.deepseek_base_url,
            model=app_settings.deepseek_red_team_model or app_settings.deepseek_model,
            prompt_path=app_settings.red_team_prompt_path,
            **common,
        )
        return analysis, red_team
    analysis = OpenAIAnalysisAdapter(
        api_key=app_settings.openai_api_key,
        model=app_settings.openai_model,
        prompt_path=app_settings.analysis_prompt_path,
        **common,
    )
    red_team = OpenAIRedTeamAdapter(
        api_key=app_settings.openai_api_key,
        model=app_settings.openai_red_team_model or app_settings.openai_model,
        prompt_path=app_settings.red_team_prompt_path,
        **common,
    )
    return analysis, red_team
