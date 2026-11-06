import httpx
import pytest

from app.daily_ai.gemini_analysis import GeminiRequestGovernor
from app.daily_ai.global_analysis import ProviderCallError
from test_gemini_global_analysis import adapter, RAW_TEXT, SOURCE_URL, valid_output, response_for


@pytest.mark.parametrize("failure", [503, "timeout"])
def test_local_budget_failure_does_not_halt_peer(failure):
    governor = GeminiRequestGovernor(minimum_interval_seconds=0, max_run_seconds=2400,
                                    max_provider_requests=3, sleep_fn=lambda _: None)
    def fail(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("offline", request=request)
        return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
    bad = adapter(fail, governor=governor, max_provider_requests=1, max_retries=2)
    good = adapter(lambda _: httpx.Response(200, json=response_for(valid_output())),
                   governor=governor, max_provider_requests=10)
    with pytest.raises(ProviderCallError) as caught:
        bad.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    assert caught.value.diagnostics["halt_scope"] == "department_budget"
    assert not caught.value.diagnostics["halt_provider_for_run"]
    good.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    good.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    with pytest.raises(ProviderCallError) as caught:
        good.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    assert caught.value.diagnostics["reason_code"] == "GEMINI_GLOBAL_BUDGET_EXHAUSTED"
    assert caught.value.diagnostics["halt_provider_for_run"]
    assert governor.request_count == bad.request_count + good.request_count == 3


def test_persistent_503_has_bounded_recovery_without_global_outage_claim():
    analyzer = adapter(lambda _: httpx.Response(503, json={}), max_retries=2,
                       max_provider_requests=30)
    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    assert analyzer.request_count == 6  # three tries plus one shared recovery
    assert caught.value.diagnostics["recovery_exhausted"] is True
    assert caught.value.diagnostics["halt_scope"] == "request"
    assert caught.value.diagnostics["halt_provider_for_run"] is False
