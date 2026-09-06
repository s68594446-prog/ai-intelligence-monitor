"""September 6 language incident: admission, Ops and email must agree."""
import pytest

from app.daily_ai.email_digest import evaluate_digest
from app.daily_ai.global_contracts import GlobalAiAnalysisContract
from app.daily_ai.global_digest import select_global_digest, render_global_digest
from app.daily_ai.global_ops import (
    check_global_channel, output_language_violations, output_quality_violations,
)
from app.scripts.daily_global_ai import SOURCE_POLICY_PATH, source_acquisition_checks
from app.web_discovery.source_policy import load_source_policy
from test_gemini_global_analysis import valid_output, SOURCE_URL, RAW_TEXT


def ops_for(items, **kwargs):
    return check_global_channel(
        analyses=items, raw_text_by_url={SOURCE_URL: RAW_TEXT},
        digest_items=select_global_digest(items),
        policy=load_source_policy(SOURCE_POLICY_PATH), **kwargs,
    )


@pytest.mark.parametrize("field", [
    "headline", "what_happened", "why_it_matters", "importance_reason",
    "transmission_path", "risks_and_counterevidence", "uncertainties",
])
@pytest.mark.parametrize("value", ["English analysis without Chinese.", "   "])
def test_every_required_narrative_field_is_checked_without_touching_evidence(field, value):
    if field == "transmission_path" and not value.strip():
        item = GlobalAiAnalysisContract.model_validate(valid_output(transmission_path=value))
        assert not output_language_violations(item)
        return  # Optional empty path is intentionally legal.
    output = valid_output(**{field: [value] if field in {"risks_and_counterevidence", "uncertainties"} else value})
    item = GlobalAiAnalysisContract.model_validate(output)
    original = item.model_dump()
    expected = field + "[0]" if isinstance(output[field], list) else field
    assert expected in output_language_violations(item)
    assert not ops_for([item]).checks["output_in_chinese"]
    assert item.model_dump() == original


def test_english_quotes_company_names_and_empty_optional_fields_remain_legal():
    item = GlobalAiAnalysisContract.model_validate(valid_output(
        transmission_path="", risks_and_counterevidence=[], uncertainties=[],
    ))
    assert not output_language_violations(item)
    assert ops_for([item]).passed


def test_september6_bad_model_prose_is_excluded_and_remaining_digest_passes_email_gate(tmp_path):
    good = GlobalAiAnalysisContract.model_validate(valid_output())
    bad = good.model_copy(update={
        "headline": "Hanmi Semiconductor 2.5D bonders pass Taiwan foundry qualification",
        "what_happened": "South Korea's Hanmi Semiconductor has begun supplying 2.5D packaging bonders to a Taiwanese foundry after its equipment passed mass-production qualification.",
        "why_it_matters": "This event affects the advanced packaging equipment link of the AI supply chain, involving Hanmi Semiconductor as an equipment provider and an unnamed Taiwanese foundry.",
    })
    assert not ops_for([bad]).passed
    accepted = [item for item in [bad, good] if not output_language_violations(item)]
    report = ops_for(accepted, analysis_provider_required=True, analysis_candidate_count=2,
                     analysis_attempts=2, provider_validated_outputs=2,
                     provider_failure_codes=["OUTPUT_LANGUAGE_INVALID"])
    assert report.passed
    digest = tmp_path / "2026-09-06.md"
    digest.write_text(render_global_digest(select_global_digest(accepted)) +
                      "\n## 审计\n### ops.json\n```json\n" + report.model_dump_json() + "\n```\n")
    assert evaluate_digest(digest).ok
    assert len(accepted) == 1


def test_quality_screen_includes_risks_and_ops_retains_a_backstop():
    item = GlobalAiAnalysisContract.model_validate(valid_output(uncertainties=["设备设备设备设备设备"]))
    assert output_quality_violations(item) == ["REPEATED_PHRASE:uncertainties[0]"]
    assert not ops_for([item]).checks["output_text_quality"]


def test_coverage_cannot_pass_just_because_halted_flag_is_missing():
    assert not ops_for([], analysis_candidate_count=2, analysis_attempts=1,
                       provider_halted=False).checks["analysis_coverage_complete"]


def test_all_language_rejected_is_not_a_healthy_empty_day():
    report = ops_for([], analysis_provider_required=True, analysis_candidate_count=2,
                     analysis_attempts=2, provider_validated_outputs=2,
                     provider_failure_codes=["OUTPUT_LANGUAGE_INVALID"] * 2)
    assert not report.checks["analysis_language_usable"]


@pytest.mark.parametrize("statuses,planned,fetched,expected", [
    ({"feed": "UPSTREAM_ERROR:HTTP_503"}, 0, 0, False),
    ({"feed": "DISABLED"}, 0, 0, False),
    ({}, 0, 0, False),
    ({"feed": "SUCCESS"}, 5, 0, False),
    ({"feed": "SUCCESS"}, 0, 0, True),
    ({"one": "SUCCESS", "two": "UPSTREAM_ERROR:HTTP_403"}, 5, 2, True),
])
def test_acquisition_distinguishes_unavailable_from_healthy_empty(statuses, planned, fetched, expected):
    checks = source_acquisition_checks(discovery_status=statuses, fetch_requested=True,
                                       planned_fetch_count=planned, fetched_count=fetched)
    assert all(checks.values()) is expected
