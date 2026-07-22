import json
from pathlib import Path

import yaml

from app.web_discovery.contracts import QueryPlanConfig, SearchQuery


def load_query_plan(path: Path) -> QueryPlanConfig:
    config = QueryPlanConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    ids = [item.id for item in config.queries]
    texts = [" ".join(item.query.casefold().split()) for item in config.queries]
    if len(ids) != len(set(ids)):
        raise ValueError("query plan contains duplicate query ids")
    if len(texts) != len(set(texts)):
        raise ValueError("query plan contains duplicate query text")
    return config


def build_query_plan(
    path: Path,
    coverage_snapshot: dict,
    *,
    max_queries: int,
) -> tuple[str, list[SearchQuery]]:
    config = load_query_plan(path)
    if config.coverage_id != coverage_snapshot.get("coverage_id"):
        raise ValueError("query plan coverage does not match active coverage")
    coverage_text = json.dumps(coverage_snapshot, ensure_ascii=False, sort_keys=True).casefold().replace("_", " ")
    unmapped = [item.id for item in config.queries if " ".join(item.variable.casefold().split()) not in coverage_text]
    if unmapped:
        raise ValueError("query variables do not map to active coverage: " + ", ".join(unmapped))
    selected = config.queries[:max_queries]
    return config.plan_version, [
        SearchQuery(
            query_id=item.id,
            text=item.query,
            variable=item.variable,
            purpose=item.purpose,
            language=item.language,
            time_range=item.time_range or config.default_time_range,
            plan_version=config.plan_version,
        )
        for item in selected
    ]
