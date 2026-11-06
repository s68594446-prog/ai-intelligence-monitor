"""A queued sender must observe ledger commits made after its trigger SHA."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from app.daily_ai.email_digest import deliver_latest
from test_daily_maintenance import settings
from test_final_review import save_reviewed

ROOT = Path(__file__).resolve().parents[3]
DAY = "2026-10-15"


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "user.name=Offline fixture", "-c", "user.email=fixture@example.com",
         "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.mark.parametrize("workflow_name,job", [("daily-digest", "digest"), ("email-digest", "email")])
@pytest.mark.parametrize("status", ["SENT", "PENDING", "UNKNOWN", "FAILED"])
def test_queued_sender_sees_previous_tasks_new_ledger(tmp_path, monkeypatch, workflow_name, job, status):
    remote = tmp_path / "repository"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    ledger = remote / "digests/.email-log.jsonl"
    ledger.parent.mkdir()
    ledger.write_text("")
    git(remote, "add", "digests/.email-log.jsonl")
    git(remote, "commit", "-m", "queued workflow trigger")
    trigger_sha = git(remote, "rev-parse", "HEAD")
    # Another task finishes while this workflow waits for shared concurrency.
    ledger.write_text(json.dumps({"day": DAY, "idempotency_key": "previous-task", "status": status}) + "\n")
    git(remote, "add", "digests/.email-log.jsonl")
    git(remote, "commit", "-m", "previous task persisted delivery outcome")
    assert git(remote, "show", f"{trigger_sha}:digests/.email-log.jsonl") == ""

    workflow = yaml.safe_load((ROOT / f".github/workflows/{workflow_name}.yml").read_text())
    assert workflow["concurrency"] == {"group": "daily-digest", "cancel-in-progress": False}
    checkout = next(step for step in workflow["jobs"][job]["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    # checkout's documented default is the event ref/SHA, not the latest branch.
    checkout_ref = checkout.get("with", {}).get("ref") or trigger_sha
    snapshot = git(remote, "show", f"{checkout_ref}:digests/.email-log.jsonl")
    worker = tmp_path / "worker"
    (worker / "digests").mkdir(parents=True)
    worker_ledger = worker / "digests/.email-log.jsonl"
    worker_ledger.write_text(snapshot)
    before = worker_ledger.read_bytes()
    monkeypatch.chdir(worker)
    monkeypatch.setattr("app.daily_ai.email_digest.SMTPEmailDeliveryAdapter", lambda *_: pytest.fail("no SMTP"))
    if workflow_name == "daily-digest":
        spec = importlib.util.spec_from_file_location("queued_guard", ROOT / ".github/scripts/digest_guard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = worker / "outputs"
        monkeypatch.setenv("DIGEST_DAY", DAY)
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        assert module.main() == (1 if status in {"PENDING", "UNKNOWN"} else 0)
        if status in {"SENT", "FAILED"}:
            assert output.read_text().strip() == f"skip={'true' if status == 'SENT' else 'false'}"
        else:
            assert not output.exists()
    else:
        save_reviewed(worker / "digests")
        result = deliver_latest(settings(), digests_dir=worker / "digests", ledger_path=worker_ledger, send=False, day=DAY)
        assert result["status"] == {"SENT": "ALREADY_SENT", "PENDING": "BLOCKED", "UNKNOWN": "BLOCKED", "FAILED": "READY"}[status]
    assert worker_ledger.read_bytes() == before
