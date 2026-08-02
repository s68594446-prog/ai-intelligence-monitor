from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github/workflows"


def load_steps(name: str) -> list[dict]:
    workflow = yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))
    return workflow["jobs"]["digest"]["steps"]


def test_daily_workflow_gates_before_publish_and_delivery():
    steps = load_steps("daily-digest.yml")
    names = [step.get("name") for step in steps]

    upload = names.index("Upload full run artifacts (audit trail)")
    gate = names.index("Verify digest Ops Gate")
    prepare = names.index("Prepare audited digest for delivery")
    email = names.index("Email the digest (Gate-checked; sends only if Ops passed)")
    commit = names.index("Commit delivered digest and email ledger")

    assert upload < gate < prepare < email < commit

    assert steps[gate]["run"].strip() == "python -m app.scripts.verify_daily_digest_ops"
    assert "git commit" not in steps[prepare]["run"]
    assert steps[email]["run"].strip() == "python -m app.scripts.send_digest_email --send"
    assert "||" not in steps[email]["run"]
    assert "git add" in steps[commit]["run"]
    assert "git push origin main" in steps[commit]["run"]


def test_workflows_use_node24_action_majors():
    for name in ("daily-digest.yml", "email-digest.yml", "env-probe.yml"):
        text = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
        assert "actions/checkout@v4" not in text
        assert "actions/setup-python@v5" not in text
        assert "actions/upload-artifact@v4" not in text

    daily = (WORKFLOWS_DIR / "daily-digest.yml").read_text(encoding="utf-8")
    assert "actions/checkout@v5" in daily
    assert "actions/setup-python@v6" in daily
    assert "actions/upload-artifact@v6" in daily
