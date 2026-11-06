import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.settings import Settings
from app.daily_ai.email_digest import deliver_latest, EmailLedger
from app.daily_ai.final_review import sha
from app.daily_ai.watchdog import diagnose
from test_final_review import save_reviewed

ROOT = Path(__file__).resolve().parents[3]


def settings(**changes):
    return Settings(_env_file=None, email_delivery_enabled=True, smtp_host="smtp.example.com",
                    smtp_port=587, smtp_username="sender@example.com", smtp_password="fake",
                    smtp_from="sender@example.com", smtp_to=changes.get("smtp_to", "reader@example.com"))


@pytest.mark.parametrize("changed_body,changed_recipient", [(True, False), (False, True), (True, True)])
def test_sent_day_cannot_resend_a_different_content_or_recipient(tmp_path, monkeypatch, changed_body, changed_recipient):
    save_reviewed(tmp_path, "五年期2.5亿美元算力承诺。" + ("研究支持计划。" if changed_body else ""))
    ledger = tmp_path / "ledger"
    EmailLedger(ledger).append({"day": "2026-10-15", "status": "SENT", "idempotency_key": "old-body-and-recipient"})
    before = ledger.read_bytes()
    monkeypatch.setattr("app.daily_ai.email_digest.SMTPEmailDeliveryAdapter", lambda *_: pytest.fail("no SMTP"))
    result = deliver_latest(settings(smtp_to="new@example.com" if changed_recipient else "reader@example.com"),
                            digests_dir=tmp_path, ledger_path=ledger, send=True)
    assert result["status"] == "ALREADY_SENT"
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("content", ["broken JSON", "[]", '{"day":"bad","status":"SENT","idempotency_key":"k"}', '{"day":"2026-10-15","status":"READY","idempotency_key":"k"}'])
def test_corrupt_ledger_fails_closed_with_sanitized_reason(tmp_path, monkeypatch, content):
    save_reviewed(tmp_path)
    ledger = tmp_path / "ledger"
    ledger.write_text(content)
    monkeypatch.setattr("app.daily_ai.email_digest.SMTPEmailDeliveryAdapter", lambda *_: pytest.fail("no SMTP"))
    result = deliver_latest(settings(), digests_dir=tmp_path, ledger_path=ledger, send=True)
    assert result["reasons"] == ["DELIVERY_LEDGER_INVALID"]
    assert ledger.read_text() == content


def test_local_concurrent_sender_blocks_and_dry_run_does_not_write_lock_or_ledger(tmp_path):
    save_reviewed(tmp_path)
    ledger = tmp_path / "ledger"
    assert deliver_latest(settings(), digests_dir=tmp_path, ledger_path=ledger, send=False)["status"] == "READY"
    assert not ledger.exists() and not ledger.with_suffix(".lock").exists()
    with EmailLedger(ledger).lock():
        result = deliver_latest(settings(), digests_dir=tmp_path, ledger_path=ledger, send=True)
    assert result["reasons"] == ["DELIVERY_IN_PROGRESS"]
    assert not ledger.exists()


def test_accepted_body_hash_is_retained_without_recipient_addresses(tmp_path, monkeypatch):
    from app.delivery.contracts import EmailProviderResult
    path, review = save_reviewed(tmp_path)
    class FakeSMTP:
        def __init__(self, *_): pass
        def send(self, request):
            return EmailProviderResult(provider_message_id="accepted")
    monkeypatch.setattr("app.daily_ai.email_digest.SMTPEmailDeliveryAdapter", FakeSMTP)
    ledger = tmp_path / "ledger"
    result = deliver_latest(settings(), digests_dir=tmp_path, ledger_path=ledger, send=True)
    assert result["status"] == "SENT"
    rows = EmailLedger(ledger)._entries()
    assert [r["status"] for r in rows] == ["PENDING", "SENT"]
    assert rows[-1]["body_sha256"] == review["body_hashes"]["markdown"]
    assert "reader@example.com" not in ledger.read_text()


NOW = datetime(2026, 11, 6, 1, 31, tzinfo=timezone.utc)
RUN = {"id": 1, "head_branch": "main", "created_at": "2026-11-05T22:30:00Z", "status": "completed", "conclusion": "failure"}


@pytest.mark.parametrize("step,status", [
    ("Run daily digest (RSS + Juhe discovery, safe fetch, Gemini, render)", "GENERATION_FAILED"),
    ("Verify digest Ops Gate", "REVIEW_BLOCKED"),
    ("Email the digest (Gate-checked; sends only if Ops passed)", "DELIVERY_FAILED"),
    ("Commit delivered digest and email ledger", "ARCHIVE_FAILED"),
    ("Validate SMTP configuration (no network)", "DELIVERY_CONFIGURATION_FAILED"),
    ("Persist accepted delivery ledger", "DELIVERY_UNCERTAIN"),
])
def test_watchdog_separates_failure_stages(step, status):
    report = diagnose(now=NOW, runs=[RUN], jobs=[{"steps": [{"name": step, "conclusion": "failure"}]}], ledger_state="NOT_SENT", archive_exists=False)
    assert report["status"] == status and not report["passed"]
    assert report["model_calls"] == report["smtp_calls"] == 0


@pytest.mark.parametrize("state,archive,matches,status", [
    ("SENT", True, True, "SMTP_ACCEPTED_ARCHIVED"),
    ("SENT", False, True, "ARCHIVE_FAILED"),
    ("SENT", True, False, "ARCHIVE_FAILED"),
    ("UNRESOLVED", True, True, "DELIVERY_UNCERTAIN"),
    ("INVALID", True, True, "LEDGER_INVALID"),
    ("NOT_SENT", False, True, "NOT_STARTED"),
])
def test_watchdog_never_confuses_smtp_with_inbox_or_draft_with_publication(state, archive, matches, status):
    report = diagnose(now=NOW, runs=[], jobs=[], ledger_state=state, archive_exists=archive, archive_matches=matches)
    assert report["status"] == status
    assert report["inbox_delivery_verified"] is False
    assert report["passed"] is (status == "SMTP_ACCEPTED_ARCHIVED")


def test_watchdog_successful_dry_run_is_not_delivery_and_stalled_run_is_visible():
    assert diagnose(now=NOW, runs=[{**RUN, "conclusion": "success"}], jobs=[], ledger_state="NOT_SENT", archive_exists=True)["status"] == "DELIVERY_UNCONFIRMED"
    assert diagnose(now=NOW, runs=[{**RUN, "status": "in_progress"}], jobs=[], ledger_state="NOT_SENT", archive_exists=False)["status"] == "RUN_STALLED"
    assert diagnose(now=NOW.replace(hour=0), runs=[], jobs=[], ledger_state="NOT_SENT", archive_exists=False)["status"] == "WAITING_FOR_SCHEDULE"


def test_watchdog_api_failure_is_sanitized_and_not_green(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("watchdog_cli", ROOT / ".github/scripts/digest_watchdog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(module, "api", lambda _: (_ for _ in ()).throw(RuntimeError("secret response body")))
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "OBSERVATION_FAILED" in output and "secret response" not in output
    assert json.loads((tmp_path / "watchdog-report.json").read_text())["passed"] is False


def test_exact_run_and_day_cannot_fall_back_to_newer_passed_run(tmp_path):
    from app.scripts.verify_daily_digest_ops import verify_latest_ops
    path, _ = save_reviewed(tmp_path)
    report = json.loads(path.read_text().split('```json\n')[1].split('\n```')[0])
    run = tmp_path / "20261015T000000Z-daily-ai"
    run.mkdir()
    (run / "ops.json").write_text(json.dumps(report))
    (run / "04-digest.txt").write_text(path.read_text().split('## 审计')[0].strip())
    assert verify_latest_ops(tmp_path, run_dir=run, day="2026-10-15")["passed"]
    assert not verify_latest_ops(tmp_path, run_dir=run, day="2026-10-16")["passed"]
    assert not verify_latest_ops(tmp_path, run_dir=tmp_path / "missing", day="2026-10-15")["passed"]


@pytest.mark.parametrize("state,code,decision", [("SENT", 0, "true"), ("FAILED", 0, "false"), ("PENDING", 1, None), ("UNKNOWN", 1, None), ("invalid", 1, None)])
def test_generation_guard_is_fail_closed_and_force_cannot_bypass_sent(tmp_path, monkeypatch, state, code, decision):
    spec = importlib.util.spec_from_file_location("guard_cli", ROOT / ".github/scripts/digest_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_DAY", "2026-11-06")
    monkeypatch.setenv("FORCE", "true")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    ledger = tmp_path / "digests/.email-log.jsonl"
    ledger.parent.mkdir()
    ledger.write_text("broken" if state == "invalid" else json.dumps({"day": "2026-11-06", "status": state, "idempotency_key": "k"}))
    before = ledger.read_bytes()
    assert module.main() == code
    assert ledger.read_bytes() == before
    assert (output.read_text().strip() if output.exists() else None) == (f"skip={decision}" if decision else None)


def test_pinned_generator_date_and_workflow_paths_survive_midnight(monkeypatch):
    import sys
    from app.scripts.daily_global_ai import _args
    monkeypatch.setattr(sys, "argv", ["daily", "--day", "2026-11-06"])
    assert _args().day.isoformat() == "2026-11-06"
    text = (ROOT / ".github/workflows/daily-digest.yml").read_text()
    assert '--day "$DIGEST_DAY"' in text
    assert "tail -1" not in text
    assert "DAILY_RUN_DIR: ${{ steps.generation.outputs.run_dir }}" in text


def test_watchdog_is_independent_read_only_and_never_receives_smtp_or_model_secrets():
    import yaml
    workflow = yaml.safe_load((ROOT / ".github/workflows/digest-watchdog.yml").read_text())
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow[True]["schedule"] == [{"cron": "30 1 * * *"}]
    assert workflow["concurrency"]["group"] != "daily-digest"
    assert "secrets." not in (ROOT / ".github/workflows/digest-watchdog.yml").read_text()
    assert not any("send_digest_email" in s.get("run", "") for s in workflow["jobs"]["inspect"]["steps"])
