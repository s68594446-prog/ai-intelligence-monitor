"""Run the production orchestrator offline against the September 5 failure shape."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.settings import Settings
from app.daily_ai.gemini_analysis import GeminiGlobalAiAnalysisAdapter, GeminiRequestGovernor
from app.scripts import daily_global_ai as daily
from app.web_discovery.contracts import ClassifiedTier, NormalizedSearchResult, ProviderSearchPage
from test_gemini_global_analysis import valid_output, response_for


@pytest.mark.parametrize(
    "request_cap,persistent_timeout,defect,expected_pass",
    [(65, False, None, True), (35, False, None, False), (65, True, None, True),
     (65, False, "one_english", True), (65, False, "all_english", False),
     (65, False, "china_english", False), (65, False, "risk_repetition", True),
     (65, False, "china_evidence_mismatch", True),
     (65, False, "unexpected", False), (65, False, "fetch_all_blocked", False),
     (65, False, "all_quality_invalid", False),
     (65, False, "global_503", False), (65, False, "global_timeout", False),
     (65, False, "november5", False),
     (65, False, "november6_recovered", True),
     (65, False, "november6_english", False),
     (65, False, "november6_evidence", False),
     (65, False, "november6_schema", False),
     (65, False, "november6_auth", False),
     (65, False, "november6_503", False),
     (32, False, "november6_no_spare", False),
     (65, False, "november6_low_score", True)],
)
def test_full_daily_run_preserves_36_candidates_and_fails_closed(
    monkeypatch, tmp_path, capsys, request_cap, persistent_timeout, defect, expected_pass,
):
    domains = (
        ["openai.com"] * 6 + ["blog.google"] * 6 + ["nvidia.com"] * 6
        + ["microsoft.com"] * 6 + ["amd.com"] * 6
        + ["leiphone.com"] * 6
    )
    november5 = defect == "november5"
    november6 = bool(defect and defect.startswith("november6_"))
    if november5:
        # Same 28-candidate desk sizes as Actions 41956523977. Content and
        # unobserved China responses are synthetic fixtures, never live news.
        domains = (["home.treasury.gov"] + ["openai.com"] * 6 + ["blog.google"] * 6
                   + ["nvidia.com"] * 6 + ["microsoft.com"] * 5 + ["leiphone.com"] * 4)
    if november6:
        # November 6: 26 Global + 5 China + 1 Macro. The actual rejected
        # risk strings were not retained by the old audit; reproduce only
        # the two observed non-Chinese risk-field failures using mock prose.
        domains = (["home.treasury.gov"] + ["openai.com"] * 6 + ["blog.google"] * 6
                   + ["nvidia.com"] * 6 + ["microsoft.com"] * 6 + ["amd.com"] * 2
                   + ["leiphone.com"] * 5)
    historical = json.loads((Path(__file__).parent / "fixtures/daily_20261105_provider_sequence.json").read_text()) if november5 else None
    global_requests = []
    candidates = [
        NormalizedSearchResult(
            candidate_id=f"c-{i}", canonical_url=f"https://{domain}/ai-update-{i}",
            title=f"HBM capacity update {i}", snippet="HBM infrastructure",
            query_ids=["offline"], best_rank=i + 1,
            source_tier=ClassifiedTier.T2 if domain == "leiphone.com" else ClassifiedTier.T1,
            source_policy_version="fixture", source_domain=domain,
        )
        for i, domain in enumerate(domains)
    ]
    raw_text = "OpenAI announced a verified AI infrastructure update with 100 systems."
    class Search:
        def __init__(self, **kwargs):
            pass

        def search(self, query, **kwargs):
            return ProviderSearchPage(
                provider="offline", adapter_version="fixture", query_id=query.query_id,
                endpoint="https://example.com", request_count=0, retry_count=0,
                results=[], raw_response={},
            )

        def close(self):
            pass

    class Fetch:
        def __init__(self, **kwargs):
            pass

        def fetch(self, candidate, **kwargs):
            if defect == "fetch_all_blocked":
                return SimpleNamespace(status=SimpleNamespace(value="BLOCKED"), error_code="NON_PUBLIC_ADDRESS")
            return SimpleNamespace(
                status=SimpleNamespace(value="SUCCESS"), title=candidate.title,
                response_sha256="a" * 64, content_hash="b" * 64, raw_text=raw_text,
            )

    attempts = {}
    inputs_by_url = {}
    seen_models = []
    timeout_url = candidates[1].canonical_url

    def provider(request):
        payload = json.loads(request.content)
        metadata = json.loads(payload["contents"][0]["parts"][0]["text"])
        url = metadata["source_url"]
        if url in inputs_by_url:
            assert metadata == inputs_by_url[url]  # no rewritten source/prompt
        inputs_by_url[url] = metadata
        seen_models.append(request.url.path)
        assert "x-goog-api-key" in request.headers
        assert "offline-secret" not in str(request.url)
        if len(seen_models) == 1 and not november5 and not november6:
            return httpx.Response(429, json={"error": {
                "status": "RESOURCE_EXHAUSTED", "details": [{"violations": [{
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                }]}],
            }})
        attempts[url] = attempts.get(url, 0) + 1
        is_macro = "home.treasury.gov" in url
        if november6 and is_macro and attempts[url] > 1:
            if defect == "november6_auth":
                return httpx.Response(401, json={"error": {"message": "offline-secret"}})
            if defect == "november6_503":
                return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
            if defect == "november6_schema":
                return httpx.Response(200, json=response_for("invalid JSON"))
        if november5 and "leiphone.com" not in url and "home.treasury.gov" not in url:
            request_index = len(global_requests)
            assert request_index < len(historical["global_http_statuses"])
            status = historical["global_http_statuses"][request_index]
            global_requests.append(status)
            if status != 200:
                return httpx.Response(status, json={"error": {"status": "UNAVAILABLE"}})
        if "leiphone.com" not in url and defect == "global_503":
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
        if "leiphone.com" not in url and defect == "global_timeout":
            raise httpx.ReadTimeout("offline failure", request=request)
        if not november5 and not november6 and url == timeout_url and (persistent_timeout or attempts[url] == 1):
            raise httpx.ReadTimeout("offline-secret must not be logged", request=request)
        is_t2 = "leiphone.com" in url
        if defect == "unexpected" and url == timeout_url:
            raise RuntimeError("offline-secret must not be logged")
        output = valid_output(
            evidence=[{"quote": "100 systems", "source_url": url}],
            sourcing="SECONDHAND_REPORT" if is_t2 else "FIRSTHAND_OFFICIAL",
            original_source="公司公告" if is_t2 else "",
        )
        if november6 and is_macro:
            if defect == "november6_low_score":
                output["importance_score"] = 50
            elif attempts[url] == 1 or defect == "november6_english":
                output["risks_and_counterevidence"] = ["Unverified execution risk", "Uncertain timing"]
            elif defect == "november6_evidence":
                output["evidence"][0]["quote"] = "invented exact quote must be rejected"
        if november5 and "leiphone.com" not in url and "home.treasury.gov" not in url:
            if len(global_requests) in historical["invalid_language_request_numbers"]:
                output["headline"] = "English-only headline must remain rejected"
            if len(global_requests) == historical["uncited_numeric_request_number"]:
                output["evidence"][0]["quote"] = "verified AI infrastructure update"
        if (defect == "one_english" and url == timeout_url
                or defect == "all_english"
                or defect == "china_english" and is_t2):
            output.update(headline="Packaging equipment qualification update",
                          what_happened="The supplier passed production qualification.",
                          why_it_matters="Equipment qualification enables supply.")
        if defect == "risk_repetition" and url == timeout_url:
            output["risks_and_counterevidence"] = ["设备设备设备设备设备"]
        if defect == "all_quality_invalid":
            output["uncertainties"] = ["设备设备设备设备设备"]
        if defect == "china_evidence_mismatch" and url == candidates[-1].canonical_url:
            output["evidence"] = [{"quote": "unsupported exact quote", "source_url": url}]
        return httpx.Response(200, json=response_for(output))

    class Adapter(GeminiGlobalAiAnalysisAdapter):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, client=httpx.Client(transport=httpx.MockTransport(provider)))

    settings = Settings(
        _env_file=None, runs_dir=tmp_path / "runs", gemini_api_key="offline-secret",
        gemini_fallback_model="gemini-3.5-flash-lite", gemini_quota_fallback_enabled=True,
        gemini_server_fallback_enabled=november5,
        smtp_configuration_prevalidated=True, llm_max_retries=2,
    )
    monkeypatch.setattr(daily, "Settings", lambda: settings)
    monkeypatch.setattr(daily, "SOURCE_HISTORY_PATH", tmp_path / "missing-history")
    monkeypatch.setattr(daily, "SearXNGWebSearchAdapter", Search)
    monkeypatch.setattr(daily, "normalize_search_pages", lambda *_: candidates)
    monkeypatch.setattr(daily, "WebPageFetcher", Fetch)
    monkeypatch.setattr(daily, "GeminiGlobalAiAnalysisAdapter", Adapter)
    monkeypatch.setattr(daily, "GeminiRequestGovernor", lambda **kwargs: GeminiRequestGovernor(
        **kwargs, sleep_fn=lambda _: None, monotonic_fn=lambda: 0,
    ))
    monkeypatch.setattr(sys, "argv", [
        "daily", "--discover", "--fetch", "--analyze", "--render", "--max-fetch", "50",
        "--max-provider-requests", str(request_cap),
    ])

    daily.run()

    summary = json.loads(capsys.readouterr().out)
    run_dir = next((tmp_path / "runs").iterdir())
    ops = json.loads((run_dir / "ops.json").read_text())
    events = json.loads((run_dir / "provider-request-events.json").read_text())
    failures = json.loads((run_dir / "analysis-failures.json").read_text())
    if november6:
        macro = json.loads((run_dir / "departments/macro/provider-usage.json").read_text())
        macro_ops = json.loads((run_dir / "departments/macro/ops.json").read_text())
        repairs = [e for e in events if e.get("reason_code") == "OUTPUT_LANGUAGE_RETRY_SCHEDULED"]
        assert macro["candidate_attempts"] == macro["validated_outputs"] == 1
        assert summary["analysis_candidate_count"] == 32
        assert summary["provider_request_attempts"] <= request_cap
        assert len(repairs) == (0 if defect in {"november6_no_spare", "november6_low_score"} else 1)
        if repairs:
            assert repairs[0]["fields"] == ["risks_and_counterevidence[0]", "risks_and_counterevidence[1]"]
        assert ops["passed"] is expected_pass
        if defect == "november6_auth":
            assert summary["provider_halted"] is True
            assert summary["unprocessed_candidate_count"] == 31
            assert failures[0]["error_code"] == "GEMINI_HTTP_401"
        else:
            assert summary["provider_halted"] is False
            assert summary["analyze_attempts"] == summary["provider_validated_outputs"] == 32
            assert summary["unprocessed_candidate_count"] == 0
            assert ops["checks"]["china_department_complete"]
            assert ops["checks"]["global_department_complete"]
        if expected_pass:
            assert not failures
            assert macro["request_count"] == (1 if defect == "november6_low_score" else 2)
            assert summary["provider_request_attempts"] == (32 if defect == "november6_low_score" else 33)
            assert ops["checks"]["macro_analysis_language_usable"]
            assert ops["checks"]["department_delivery_ready"]
        else:
            assert macro_ops["status"] == "FAILED"
            assert not ops["checks"]["department_delivery_ready"]
            if defect == "november6_english":
                assert macro["request_count"] == 2  # no third generation
                assert failures[0]["error_code"] == "OUTPUT_LANGUAGE_INVALID"
            if defect == "november6_no_spare":
                assert macro["request_count"] == 1
            if defect == "november6_evidence":
                assert failures[0]["error_code"] == "EVIDENCE_MISMATCH"
        assert "offline-secret" not in json.dumps([events, failures])
        return
    if november5:
        usage = {desk: json.loads((run_dir / f"departments/{desk}/provider-usage.json").read_text())
                 for desk in ("global", "china", "macro")}
        assert global_requests == historical["global_http_statuses"]
        assert usage["global"]["request_budget"] == usage["global"]["request_count"] == 36
        assert usage["global"]["candidate_attempts"] == 15
        assert usage["global"]["validated_outputs"] == 11
        assert usage["china"]["candidate_attempts"] == usage["china"]["validated_outputs"] == 4
        assert usage["china"]["request_budget"] == 16
        assert usage["china"]["provider_halted"] is False
        assert usage["macro"]["validated_outputs"] == 1
        assert summary["analysis_candidate_count"] == 28
        assert summary["provider_request_attempts"] == 41 < 65
        assert summary["unprocessed_candidate_count"] == 8
        assert summary["provider_halted"] is False
        assert sum(e.get("status_code") == 503 for e in events) == 25
        assert usage["global"]["model_transitions"][0]["reason_code"] == "GEMINI_SERVER_ERROR_FALLBACK"
        assert len(usage["global"]["model_transitions"]) == 1
        assert failures[-1]["diagnostics"]["halt_scope"] == "department_budget"
        assert failures[-1]["error_code"] == "GEMINI_SERVER_ERROR"
        assert not ops["passed"]
        assert ops["checks"]["china_department_complete"]
        assert ops["checks"]["macro_department_complete"]
        assert not ops["checks"]["global_provider_response_coverage_sufficient"]
        assert not ops["checks"]["department_delivery_ready"]
        assert "offline-secret" not in json.dumps([events, failures])
        return
    if defect == "fetch_all_blocked":
        assert summary["fetched"] == 0
        assert summary["provider_request_attempts"] == 0
        assert summary["digest_items"] == 0
        assert not ops["passed"]
        assert "source_fetch_usable" in ops["errors"]
        return
    assert summary["analysis_candidate_count"] == 36
    assert summary["fetched"] == 36
    assert summary["fallback_used"] is True
    assert summary["provider_request_attempts"] <= request_cap
    assert ops["passed"] is expected_pass
    assert "offline-secret" not in json.dumps([events, failures])
    if defect in {"global_503", "global_timeout"}:
        china = json.loads((run_dir / "departments/china/provider-usage.json").read_text())
        assert china["candidate_attempts"] == 6
        assert china["validated_outputs"] == 6
        assert china["provider_halted"] is False
        assert summary["provider_halted"] is False
        assert summary["unprocessed_candidate_count"] > 0
        assert not ops["checks"]["department_delivery_ready"]
        return
    assert any(event.get("reason_code") == "GEMINI_TIMEOUT" for event in events)
    if expected_pass:
        assert summary["analyze_attempts"] == 36
        assert summary["provider_validated_outputs"] == (35 if persistent_timeout else 36)
        assert summary["provider_request_attempts"] == (39 if persistent_timeout else 38)
        assert summary["unprocessed_candidate_count"] == 0
        if defect or persistent_timeout:
            assert len(failures) == 1
            expected_code = "GEMINI_TIMEOUT" if persistent_timeout else (
                "OUTPUT_LANGUAGE_INVALID" if defect == "one_english" else
                "EVIDENCE_MISMATCH" if defect == "china_evidence_mismatch" else
                "LOW_TEXT_QUALITY")
            assert failures[0]["error_code"] == expected_code
            assert summary["analyzed"] == 35
        else:
            assert not failures
        assert "额度降级" in (run_dir / "04-digest.txt").read_text()
    elif defect in {"all_english", "china_english"}:
        assert summary["unprocessed_candidate_count"] == 0
        assert "china_analysis_language_usable" in ops["errors"]
        assert len(failures) == (36 if defect == "all_english" else 6)
    elif defect == "all_quality_invalid":
        assert summary["unprocessed_candidate_count"] == 0
        assert "global_analysis_semantic_output_usable" in ops["errors"]
        assert "china_analysis_semantic_output_usable" in ops["errors"]
        assert len(failures) == 36
    elif defect == "unexpected":
        assert summary["unprocessed_candidate_count"] == 0
        assert "global_analysis_execution_complete" in ops["errors"]
        assert failures[0]["error_code"] == "UNEXPECTED_RuntimeError"
    else:
        assert summary["unprocessed_candidate_count"] > 0
        assert "analysis_coverage_complete" in ops["errors"]


@pytest.mark.parametrize("value", ["0", "81"])
def test_request_budget_bounds(monkeypatch, value):
    monkeypatch.setattr(sys, "argv", ["daily", "--max-provider-requests", value])
    with pytest.raises(SystemExit, match="budget out of bounds"):
        daily._args()


def test_legacy_request_budget_does_not_silently_increase(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["daily", "--max-analyze", "3"])
    assert daily._args().max_provider_requests == 3
