import json

import pytest

from app.core.settings import Settings
from app.daily_ai.email_digest import EmailLedger, deliver_latest, evaluate_digest, idempotency_key
from app.scripts.verify_daily_digest_ops import verify_latest_ops


def prepare(tmp_path, report, day="2026-09-05", body="测试正文"):
    path = tmp_path / f"{day}.md"
    path.write_text(body + "\n\n## 审计\n### ops.json\n```json\n" + json.dumps(report) + "\n```\n")
    return path


@pytest.mark.parametrize("report", [
    {"passed": "false"}, {"passed": 1}, {"passed": "true"}, [], None,
    {"passed": True, "checks": {"coverage": False}},
    {"passed": True, "checks": {"coverage": "true"}},
    {"passed": True, "checks": []},
    {"passed": True, "errors": ["FAILED"]},
])
def test_both_publication_boundaries_reject_invalid_or_conflicting_reports(tmp_path, report):
    assert not evaluate_digest(prepare(tmp_path, report)).ok
    run = tmp_path / "20260905-daily-ai"
    run.mkdir()
    (run / "ops.json").write_text(json.dumps(report))
    result = verify_latest_ops(tmp_path)
    assert not result["passed"]
    assert result["errors"]


def test_ops_in_reader_body_cannot_substitute_for_audited_report(tmp_path):
    path = tmp_path / "2026-09-05.md"
    path.write_text('标题\n### ops.json\n```json\n{"passed":true}\n```')
    assert not evaluate_digest(path).ok
    path = prepare(tmp_path, {"passed": True})
    path.write_text(path.read_text() + '\n### ops.json\n```json\n{"passed":true}\n```')
    assert not evaluate_digest(path).ok


@pytest.mark.parametrize("day,content,reason", [
    ("2026-02-30", b"test", "DIGEST_DATE_INVALID"),
    ("2026-09-05", b"\xff", "DIGEST_ENCODING_INVALID"),
    ("2026-09-05", b"x" * 1_000_001, "DIGEST_TOO_LARGE"),
])
def test_bad_digest_is_a_named_block_not_an_exception(tmp_path, day, content, reason):
    path = tmp_path / f"{day}.md"
    path.write_bytes(content)
    assert evaluate_digest(path).reasons == [reason]


def test_exact_date_never_falls_back_to_older_digest(tmp_path):
    prepare(tmp_path, {"passed": True}, day="2026-09-02")
    settings = Settings(_env_file=None, email_delivery_enabled=True)
    result = deliver_latest(settings, digests_dir=tmp_path, ledger_path=tmp_path / "ledger",
                            send=True, day="2026-09-05")
    assert result["status"] == "BLOCKED"
    assert result["day"] == "2026-09-05"
    assert not (tmp_path / "ledger").exists()
    result = deliver_latest(settings, digests_dir=tmp_path, ledger_path=tmp_path / "ledger",
                            send=True, day="../../2026-09-02")
    assert result["reasons"] == ["DIGEST_DATE_INVALID"]


@pytest.mark.parametrize("status", ["PENDING", "UNKNOWN"])
@pytest.mark.parametrize("changed_body", [False, True])
def test_uncertain_delivery_blocks_same_day_even_after_regeneration(tmp_path, monkeypatch, status, changed_body):
    prepare(tmp_path, {"passed": True})
    settings = Settings(
        _env_file=None, email_delivery_enabled=True,
        smtp_host="smtp.example.com", smtp_port=587, smtp_username="sender@example.com",
        smtp_password="fake", smtp_from="sender@example.com", smtp_to="reader@example.com",
    )
    key = idempotency_key("2026-09-05", "旧正文" if changed_body else "测试正文", ("reader@example.com",))
    ledger = tmp_path / "ledger"
    ledger.write_text(json.dumps({"day": "2026-09-05", "idempotency_key": key, "status": status}) + "\n")
    before = ledger.read_bytes()
    monkeypatch.setattr("app.daily_ai.email_digest.SMTPEmailDeliveryAdapter",
                        lambda *_: pytest.fail("uncertain delivery must not call SMTP"))
    result = deliver_latest(settings, digests_dir=tmp_path, ledger_path=ledger, send=True)
    assert result["reasons"] == ["PRIOR_DELIVERY_OUTCOME_UNRESOLVED"]
    assert ledger.read_bytes() == before


def test_completed_attempt_resolves_pending_but_not_another_uncertain_attempt(tmp_path):
    ledger = EmailLedger(tmp_path / "ledger")
    for status in ("PENDING", "FAILED"):
        ledger.append({"day": "2026-09-05", "idempotency_key": "first", "status": status})
    assert not ledger.unresolved_attempt("new-content", "2026-09-05")
    ledger.append({"day": "2026-09-05", "idempotency_key": "second", "status": "UNKNOWN"})
    ledger.append({"day": "2026-09-05", "idempotency_key": "first", "status": "SENT"})
    assert ledger.unresolved_attempt("new-content", "2026-09-05")
    ledger.append({"day": "2026-09-05", "idempotency_key": "second", "status": "SENT"})
    assert not ledger.unresolved_attempt("new-content", "2026-09-05")
