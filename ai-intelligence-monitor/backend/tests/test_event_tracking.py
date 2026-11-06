import json
from datetime import date

import pytest

from app.daily_ai.event_tracking import load_published_threads, track_events, thread_id, review_judgments
from app.daily_ai.final_review import sha
from app.daily_ai.global_contracts import GlobalAiAnalysisContract
from app.daily_ai.global_digest import select_global_digest
from app.daily_ai.delivery_ledger import audit_hash
from test_gemini_global_analysis import valid_output, SOURCE_URL


def track(previous=None, day=date(2026, 10, 16), **changes):
    analysis = GlobalAiAnalysisContract.model_validate(valid_output(**changes))
    return track_events(select_global_digest([analysis]), analyses=[analysis], previous=previous or {},
                        day=day, aliases={})


def test_unchanged_evidence_suppressed_but_genuinely_new_evidence_retained():
    items, records = track()
    assert len(items) == 1 and records[0]["change"] == "NEW"
    previous = {records[0]["thread_id"]: records[0]}
    kept, unchanged = track(previous, day=date(2026, 10, 17), headline="公司重述同一进展")
    assert not kept
    assert unchanged[0]["change"] == "UNCHANGED"
    kept, updated = track(previous, day=date(2026, 10, 17),
                          evidence=[{"quote": "200 systems", "source_url": SOURCE_URL}])
    assert kept and updated[0]["change"] == "NEW_EVIDENCE"
    assert updated[0]["last_reported_conclusion"] == records[0]["conclusion"]
    assert len(updated[0]["evidence_chain"]) == 2
    assert not updated[0]["trend_candidate"]
    _, third = track({updated[0]["thread_id"]: updated[0]}, day=date(2026, 10, 18),
                     evidence=[{"quote": "300 systems", "source_url": SOURCE_URL}])
    assert len(third[0]["evidence_chain"]) == 3
    assert third[0]["trend_candidate"]


def test_status_change_and_reviewed_aliases_without_company_fuzzy_merge():
    _, records = track(announced_vs_delivered="ANNOUNCED_ONLY")
    kept, updated = track({records[0]["thread_id"]: records[0]})
    assert not kept and updated[0]["change"] == "STATUS_REVIEW_REQUIRED"
    kept, updated = track({records[0]["thread_id"]: records[0]}, evidence=[{"quote": "Delivery confirmed: 100 systems", "source_url": SOURCE_URL}])
    assert kept and updated[0]["change"] == "STATUS_CHANGED"
    other = "https://openai.com/index/unrelated"
    assert thread_id(SOURCE_URL, {}) != thread_id(other, {})
    assert thread_id(SOURCE_URL, {SOURCE_URL: "reviewed-event"}) == thread_id(other, {other: "reviewed-event"})


def test_only_body_matched_sent_archive_is_published_not_fetch_or_dry_run(tmp_path):
    _, records = track(day=date(2026, 10, 15))
    from app.daily_ai.global_digest import render_global_digest
    items, _ = track(day=date(2026, 10, 15))
    content = render_global_digest(items, analysis_model="mock", prompt_version="v9")
    archive = tmp_path / "2026-10-15.md"
    report = {"passed": True, "audit": {"event_threads": records}}
    archive.write_text(content + '\n## 审计\n### ops.json\n```json\n'
                       + json.dumps(report) + '\n```')
    ledger = tmp_path / ".email-log.jsonl"
    for status, digest_hash in [("PENDING", sha(content)), ("UNKNOWN", sha(content)),
                                ("SENT", sha("different draft"))]:
        ledger.write_text(json.dumps({"day": "2026-10-15", "idempotency_key": "key", "status": status, "body_sha256": digest_hash}))
        loaded, warnings = load_published_threads(tmp_path, current_day=date(2026, 10, 16))
        assert loaded == {} and not warnings
    ledger.write_text(json.dumps({"day": "2026-10-15", "idempotency_key": "key", "status": "SENT", "body_sha256": sha(content), "audit_sha256": audit_hash(report)}))
    loaded, _ = load_published_threads(tmp_path, current_day=date(2026, 10, 16))
    assert loaded[records[0]["thread_id"]]["publication_status"] == "PUBLISHED"
    assert load_published_threads(tmp_path, current_day=date(2026, 10, 15))[0] == {}
    report["audit"]["event_threads"][0]["conclusion"] = "篡改的结论"
    archive.write_text(content + '\n## 审计\n### ops.json\n```json\n' + json.dumps(report) + '\n```')
    loaded, warnings = load_published_threads(tmp_path, current_day=date(2026, 10, 16))
    assert loaded == {} and warnings == ["THREAD_AUDIT_NOT_BOUND_TO_SEND:2026-10-15"]


@pytest.mark.parametrize("status", ["FULFILLED", "NOT_FULFILLED", "PARTIAL"])
def test_due_judgment_needs_reviewed_source_evidence_not_a_model_verdict(status):
    judgment = {"claim_id": "capacity-review", "thread_id": "capacity-event", "review_date": "2026-10-16",
                "verification_condition": "以官方投产披露核对是否按期交付。",
                "confirmed_review": {"date": "2026-10-16", "confirmed_by": "human", "status": status,
                                     "source_url": SOURCE_URL, "quote": "100 systems"}}
    assert review_judgments([judgment], today=date(2026, 10, 15), raw_by_url={SOURCE_URL: "100 systems"})[0]["status"] == "PENDING"
    assert review_judgments([judgment], today=date(2026, 10, 16), raw_by_url={})[0]["status"] == "PENDING"
    assert review_judgments([judgment], today=date(2026, 10, 16), raw_by_url={SOURCE_URL: "100 systems"})[0]["status"] == status
    judgment["confirmed_review"]["confirmed_by"] = "model"
    assert review_judgments([judgment], today=date(2026, 10, 16), raw_by_url={SOURCE_URL: "100 systems"})[0]["status"] == "PENDING"


def test_empty_judgments_are_legitimate():
    assert review_judgments([], today=date(2026, 10, 16), raw_by_url={}) == []
