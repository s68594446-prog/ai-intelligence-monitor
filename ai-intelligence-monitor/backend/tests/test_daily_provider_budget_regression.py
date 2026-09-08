"""Run the production orchestrator offline against the September 5 failure shape."""

import json
import sys
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
     (65, False, "all_quality_invalid", False)],
)
def test_full_daily_run_preserves_36_candidates_and_fails_closed(
    monkeypatch, tmp_path, capsys, request_cap, persistent_timeout, defect, expected_pass,
):
    domains = (
        ["openai.com"] * 6 + ["blog.google"] * 6 + ["nvidia.com"] * 6
        + ["microsoft.com"] * 6 + ["amd.com"] * 6
        + ["leiphone.com"] * 6
    )
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
    seen_models = []
    timeout_url = candidates[1].canonical_url

    def provider(request):
        payload = json.loads(request.content)
        metadata = json.loads(payload["contents"][0]["parts"][0]["text"])
        url = metadata["source_url"]
        seen_models.append(request.url.path)
        assert "x-goog-api-key" in request.headers
        assert "offline-secret" not in str(request.url)
        if len(seen_models) == 1:
            return httpx.Response(429, json={"error": {
                "status": "RESOURCE_EXHAUSTED", "details": [{"violations": [{
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                }]}],
            }})
        attempts[url] = attempts.get(url, 0) + 1
        if url == timeout_url and (persistent_timeout or attempts[url] == 1):
            raise httpx.ReadTimeout("offline-secret must not be logged", request=request)
        is_t2 = "leiphone.com" in url
        if defect == "unexpected" and url == timeout_url:
            raise RuntimeError("offline-secret must not be logged")
        output = valid_output(
            evidence=[{"quote": "100 systems", "source_url": url}],
            sourcing="SECONDHAND_REPORT" if is_t2 else "FIRSTHAND_OFFICIAL",
            original_source="公司公告" if is_t2 else "",
        )
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
