from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import build_engine
from app.core.settings import PROJECT_DIR, Settings
from app.harness.service import build_harness
from app.models import Base


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        runs_dir=tmp_path / "runs",
        coverage_path=PROJECT_DIR / "config" / "coverage" / "dram_hbm.yaml",
        fixtures_dir=PROJECT_DIR / "fixtures",
        fixed_sources_path=PROJECT_DIR / "config" / "sources" / "official_sources.yaml",
        fetch_audits_dir=tmp_path / "fetches",
        prompt_version="phase0-prompt-v1",
        openai_api_key=None,
    )


@pytest.fixture
def session(app_settings: Settings):
    engine = build_engine(app_settings.database_url)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with maker() as db_session:
        yield db_session
    engine.dispose()


@pytest.fixture
def harness(session: Session, app_settings: Settings):
    return build_harness(session, app_settings)
