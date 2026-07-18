import json
from pathlib import Path


ANALYSIS_PROMPT_VERSION = "analysis-v1"
RED_TEAM_PROMPT_VERSION = "red-team-v1"


def render_prompt(path: Path, coverage_snapshot: dict, thesis_snapshot: dict) -> str:
    template = Path(path).read_text(encoding="utf-8")
    return template.replace(
        "{{COVERAGE_SNAPSHOT}}",
        json.dumps(coverage_snapshot, ensure_ascii=False, sort_keys=True),
    ).replace(
        "{{THESIS_SNAPSHOT}}",
        json.dumps(thesis_snapshot, ensure_ascii=False, sort_keys=True),
    )

