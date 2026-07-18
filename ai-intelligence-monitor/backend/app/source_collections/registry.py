from pathlib import Path

import yaml

from app.source_collections.contracts import ApprovedSource, SourceRegistry


def load_registry(path: Path) -> SourceRegistry:
    registry = SourceRegistry.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    ids = [source.source_id for source in registry.sources]
    if len(ids) != len(set(ids)):
        raise ValueError("source registry contains duplicate source_id values")
    return registry


def get_source(registry: SourceRegistry, source_id: str, *, require_enabled: bool = True) -> ApprovedSource:
    source = next((item for item in registry.sources if item.source_id == source_id), None)
    if source is None:
        raise KeyError(f"source_id is not approved: {source_id}")
    if require_enabled and not source.enabled:
        raise ValueError(f"approved source is disabled: {source_id}")
    return source
