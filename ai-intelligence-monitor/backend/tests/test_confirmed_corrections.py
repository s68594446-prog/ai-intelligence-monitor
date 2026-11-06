import json

import pytest

from app.daily_ai.corrections import load_corrections, recurrence_findings
from app.daily_ai.final_review import review_fields
from test_final_review import SOURCE, URL


def test_confirmed_error_is_found_in_admission_and_replay_without_rewriting():
    fields = {"uncertainties": "原文未披露该五亿美元算力承诺的时间表。"}
    before = fields.copy()
    result = review_fields(fields, URL, SOURCE)
    recurrence = [f for f in result["findings"] if f["error_type"] == "CONFIRMED_ERROR_RECURRENCE"]
    assert recurrence[0]["claim_id"] == "nvidia-uk-compute-20261013-total-usd"
    assert recurrence[0]["evidence"] in SOURCE
    assert fields == before


def test_no_forced_issues_and_no_cross_event_or_new_evidence_false_positive():
    assert recurrence_findings({"body": "五年期2.5亿美元算力承诺"}, URL, SOURCE) == []
    assert recurrence_findings({"body": "五亿美元算力承诺"}, "https://example.com/other", SOURCE) == []
    assert recurrence_findings({"body": "五亿美元算力承诺"}, URL,
                               SOURCE + " Updated commitment: $500 million.") == []


@pytest.mark.parametrize("changes", [{"status": "SUSPECTED"}, {"confirmation_basis": "model_opinion"}, {"evidence": ""}])
def test_unconfirmed_reviews_cannot_enter_registry(tmp_path, changes):
    entry = {**load_corrections()[0], **changes}
    path = tmp_path / "corrections.json"
    path.write_text(json.dumps([entry]))
    with pytest.raises(ValueError):
        load_corrections(path)


def test_known_error_variants_and_registry_updates_are_checked_at_send_boundary(tmp_path, monkeypatch):
    from test_final_review import save_reviewed
    from app.daily_ai.email_digest import evaluate_digest
    assert recurrence_findings({"body": "五年算力承诺为5亿美元。"}, URL, SOURCE)
    path, _ = save_reviewed(tmp_path)
    assert evaluate_digest(path).ok
    monkeypatch.setattr("app.daily_ai.final_review.registry_digest", lambda: "changed-registry")
    assert "FINAL_REVIEW_MISSING_OR_STALE" in evaluate_digest(path).reasons
