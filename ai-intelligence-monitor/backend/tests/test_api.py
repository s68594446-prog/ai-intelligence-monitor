from fastapi.testclient import TestClient

from app.api import routes
from app.core.database import get_session
from app.coverage.service import ensure_coverage
from app.harness.service import build_harness, replay_run
from app.main import app


def test_minimal_read_api(session, app_settings):
    ensure_coverage(session, app_settings.coverage_path)
    session.commit()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json()["llm"] is False
            coverage = client.get("/api/coverage")
            assert coverage.status_code == 200
            assert coverage.json()[0]["coverage_id"] == "dram_hbm"
            assert client.get("/api/runs").status_code == 200
            assert client.get("/api/signals").status_code == 200
            assert client.get("/api/runs/not-found").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_demo_and_replay_api(session, app_settings, monkeypatch):
    monkeypatch.setattr(routes, "build_harness", lambda db: build_harness(db, app_settings))
    monkeypatch.setattr(
        routes,
        "replay_run",
        lambda db, run_id: replay_run(db, run_id, app_settings),
    )

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            demo = client.post("/api/demo/run")
            assert demo.status_code == 200
            assert demo.json()["status"] == "PASSED"
            run_id = demo.json()["run_id"]
            replay = client.post(f"/api/runs/{run_id}/replay")
            assert replay.status_code == 200
            assert replay.json()["diff"]["stable"] is True
            assert replay.json()["run"]["replay_of"] == run_id
    finally:
        app.dependency_overrides.clear()
