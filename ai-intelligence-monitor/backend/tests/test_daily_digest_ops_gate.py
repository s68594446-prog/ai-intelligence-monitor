import json

from app.scripts.verify_daily_digest_ops import verify_latest_ops


def write_ops(runs_dir, run_id: str, value) -> None:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    if isinstance(value, str):
        (run_dir / "ops.json").write_text(value, encoding="utf-8")
    else:
        (run_dir / "ops.json").write_text(json.dumps(value), encoding="utf-8")


def test_daily_digest_ops_gate_fails_closed_when_missing_or_invalid(tmp_path):
    missing = verify_latest_ops(tmp_path)
    assert not missing["passed"]
    assert missing["errors"] == ["OPS_RESULT_MISSING"]

    write_ops(tmp_path, "20260802T000000Z-daily-ai", "not-json")
    invalid = verify_latest_ops(tmp_path)
    assert not invalid["passed"]
    assert invalid["errors"] == ["OPS_RESULT_INVALID"]


def test_daily_digest_ops_gate_surfaces_failure_details(tmp_path):
    write_ops(
        tmp_path,
        "20260802T000000Z-daily-ai",
        {
            "passed": False,
            "errors": ["firsthand_tier_consistent"],
            "offenders": {"firsthand_tier_consistent": ["bad item"]},
        },
    )

    result = verify_latest_ops(tmp_path)

    assert not result["passed"]
    assert result["errors"] == ["firsthand_tier_consistent"]
    assert result["offenders"] == {"firsthand_tier_consistent": ["bad item"]}


def test_daily_digest_ops_gate_uses_latest_passing_run(tmp_path):
    write_ops(
        tmp_path,
        "20260801T000000Z-daily-ai",
        {"passed": False, "errors": ["old failure"], "offenders": {}},
    )
    write_ops(
        tmp_path,
        "20260802T000000Z-daily-ai",
        {"passed": True, "errors": [], "offenders": {}},
    )

    result = verify_latest_ops(tmp_path)

    assert result["passed"]
    assert result["errors"] == []
    assert result["ops_path"].endswith("20260802T000000Z-daily-ai/ops.json")
