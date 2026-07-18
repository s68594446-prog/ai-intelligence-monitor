from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import ThesisStatus
from app.models import CoverageTopic, Thesis


def load_coverage(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    required = {
        "coverage_id",
        "name",
        "version",
        "objective",
        "selected",
        "entities",
        "products",
        "monitoring_variables",
        "dependency_sentinels",
        "exclusions",
        "thesis",
        "supporting_assumptions",
        "disconfirming_conditions",
        "reverse_triggers",
        "related_positions",
        "source_requirements",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError(f"coverage is missing fields: {', '.join(missing)}")
    return data


def ensure_coverage_config(session: Session, config: dict[str, Any]) -> tuple[CoverageTopic, Thesis]:
    topic = session.scalar(
        select(CoverageTopic).where(
            CoverageTopic.coverage_id == config["coverage_id"],
            CoverageTopic.version == config["version"],
        )
    )
    if topic is None:
        topic = CoverageTopic(
            coverage_id=config["coverage_id"],
            name=config["name"],
            version=config["version"],
            config_snapshot=config,
            selected=bool(config["selected"]),
        )
        session.add(topic)
        session.flush()
    thesis = session.scalar(
        select(Thesis).where(
            Thesis.coverage_topic_id == topic.id,
            Thesis.version == config["version"],
        )
    )
    if thesis is None:
        thesis = Thesis(
            coverage_topic_id=topic.id,
            statement=config["thesis"],
            assumptions=config["supporting_assumptions"],
            disconfirming_conditions=config["disconfirming_conditions"],
            reverse_triggers=config["reverse_triggers"],
            version=config["version"],
            status=ThesisStatus.ACTIVE,
        )
        session.add(thesis)
        session.flush()
    return topic, thesis


def ensure_coverage(session: Session, path: Path) -> tuple[CoverageTopic, Thesis]:
    return ensure_coverage_config(session, load_coverage(path))
