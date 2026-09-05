import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.daily_ai.gemini_analysis import (
    GeminiGlobalAiAnalysisAdapter,
    GeminiModelController,
    GeminiRequestGovernor,
    gemini_json_schema,
)
from app.daily_ai.global_analysis import (
    AnalysisOutputError,
    EvidenceMismatch,
    ProviderCallError,
)
from app.daily_ai.global_contracts import GlobalAiAnalysisContract


PROMPT_PATH = Path(__file__).parents[1] / "app/prompts/daily_global_ai_analysis_v9.txt"
MACRO_PROMPT_PATH = Path(__file__).parents[1] / "app/prompts/daily_us_macro_analysis_v1.txt"
SOURCE_URL = "https://openai.com/index/example"
RAW_TEXT = "OpenAI announced a verified AI infrastructure update with 100 systems."
API_KEY = "test-gemini-secret"


def valid_output(**overrides) -> dict:
    output = {
        "headline": "OpenAI发布AI基础设施更新",
        "sector": "INFRASTRUCTURE",
        "importance_criterion": "CAPACITY_CHANGE",
        "chain_mapping": [{"sector": "INFRASTRUCTURE", "companies": ["OpenAI"]}],
        "transmission_path": "AI基础设施扩展 → 算力需求",
        "region_section": "GLOBAL",
        "region_basis": "事件由美国公司OpenAI发布",
        "what_happened": "OpenAI发布了包含100套系统的基础设施更新。",
        "why_it_matters": "该更新直接涉及AI基础设施供给，并形成新的算力需求信号。",
        "contains_verifiable_numbers": True,
        "sourcing": "FIRSTHAND_OFFICIAL",
        "original_source": "",
        "novelty": "NEW_FACT",
        "announced_vs_delivered": "DELIVERED",
        "risks_and_counterevidence": ["实际利用率仍需验证"],
        "evidence": [{"quote": "100 systems", "source_url": SOURCE_URL}],
        "uncertainties": ["部署节奏未知"],
        "importance_score": 70,
        "importance_reason": "存在可验证的基础设施变化",
        "investment_advice": False,
    }
    output.update(overrides)
    return output


def response_for(output: dict | str, *, finish_reason: str = "STOP") -> dict:
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    return {
        "candidates": [
            {
                "finishReason": finish_reason,
                "content": {"parts": [{"text": text}]},
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 20},
    }


def adapter(handler, **kwargs) -> GeminiGlobalAiAnalysisAdapter:
    return GeminiGlobalAiAnalysisAdapter(
        api_key=SecretStr(API_KEY),
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-3.5-flash",
        prompt_path=PROMPT_PATH,
        timeout_seconds=1,
        max_retries=kwargs.pop("max_retries", 0),
        sleep_fn=kwargs.pop("sleep_fn", lambda _: None),
        random_fn=kwargs.pop("random_fn", lambda: 0),
        **kwargs,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def assert_no_key(value) -> None:
    assert API_KEY not in json.dumps(value, ensure_ascii=False, default=str)


@pytest.mark.parametrize("request_cap,expected_calls", [(1, 1), (10, 3)])
def test_timeout_retries_respect_http_budget_and_retry_ceiling(request_cap, expected_calls):
    def handler(request):
        raise httpx.ReadTimeout(API_KEY, request=request)

    analyzer = adapter(handler, max_retries=2, max_provider_requests=request_cap)
    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    assert caught.value.diagnostics["reason_code"] == "GEMINI_TIMEOUT"
    assert analyzer.request_count == expected_calls
    assert_no_key(analyzer.request_events)


def test_timeout_recovery_cannot_exceed_shared_deadline():
    governor = GeminiRequestGovernor(
        minimum_interval_seconds=0, max_run_seconds=0.5,
        sleep_fn=lambda _: None, monotonic_fn=lambda: 0,
    )
    def handler(request):
        raise httpx.ReadTimeout(API_KEY, request=request)

    analyzer = adapter(handler, max_retries=2, governor=governor)
    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    assert caught.value.diagnostics["reason_code"] == "GEMINI_RUN_DEADLINE_EXCEEDED"
    assert analyzer.request_count == 1


def test_gemini_uses_header_system_instruction_and_structured_schema_only():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_for(valid_output()))

    contract = adapter(handler).analyze(
        metadata={
            "title": "Infrastructure update",
            "source_domain": "openai.com",
            "source_tier": "T1",
            "unapproved": "must not leave the process",
        },
        raw_text=RAW_TEXT,
        source_url=SOURCE_URL,
    )

    assert isinstance(contract, GlobalAiAnalysisContract)
    assert seen["headers"]["x-goog-api-key"] == API_KEY
    assert API_KEY not in seen["url"]
    assert API_KEY.encode() not in json.dumps(seen["body"]).encode()
    assert seen["url"].endswith("/models/gemini-3.5-flash:generateContent")
    assert seen["body"]["system_instruction"]["parts"][0]["text"] == PROMPT_PATH.read_text(encoding="utf-8").strip()
    assert "tools" not in seen["body"]
    generation = seen["body"]["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseJsonSchema"]["type"] == "object"
    user_payload = json.loads(seen["body"]["contents"][0]["parts"][0]["text"])
    assert set(user_payload) == {"metadata", "source_url", "raw_text"}
    assert "unapproved" not in user_payload["metadata"]
    assert user_payload["raw_text"] == RAW_TEXT


def test_supplemental_macro_rules_append_to_unmodified_v9_system_instruction():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_for(valid_output()))

    analyzer = adapter(handler, supplemental_prompt_path=MACRO_PROMPT_PATH)
    analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    system_text = seen["body"]["system_instruction"]["parts"][0]["text"]

    assert system_text.startswith(PROMPT_PATH.read_text(encoding="utf-8").strip())
    assert system_text.endswith(MACRO_PROMPT_PATH.read_text(encoding="utf-8").strip())


def test_gemini_schema_removes_unsupported_const_and_constraints():
    converted = gemini_json_schema(GlobalAiAnalysisContract.model_json_schema())
    encoded = json.dumps(converted, sort_keys=True)

    for unsupported in ("const", "default", "minLength", "maxLength"):
        assert f'"{unsupported}"' not in encoded
    assert converted["properties"]["investment_advice"]["type"] == "boolean"
    assert converted["properties"]["sector"]["enum"]
    assert converted["$defs"]["EvidenceQuote"]["required"] == ["quote", "source_url"]


@pytest.mark.parametrize(
    ("status", "reason_code"),
    [
        (401, "GEMINI_HTTP_401"),
        (429, "GEMINI_RATE_LIMITED"),
        (503, "GEMINI_SERVER_ERROR"),
    ],
)
def test_gemini_http_failures_are_distinct_and_secret_free(status, reason_code):
    analyzer = adapter(lambda request: httpx.Response(status, json={"error": {"message": API_KEY}}))

    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert caught.value.diagnostics["reason_code"] == reason_code
    assert_no_key(caught.value.diagnostics)
    assert API_KEY not in str(caught.value)


def test_gemini_timeout_is_distinct_and_secret_free():
    def handler(request):
        raise httpx.ReadTimeout("bounded timeout", request=request)

    with pytest.raises(ProviderCallError) as caught:
        adapter(handler).analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert caught.value.diagnostics == {
        "reason_code": "GEMINI_TIMEOUT",
        "request_attempts": 1,
    }
    assert API_KEY not in repr(caught.value)


@pytest.mark.parametrize("first_status", [429, 503])
def test_gemini_retries_bounded_transient_http_failures(first_status):
    statuses = [first_status, 200]
    sleeps = []

    def handler(request):
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(200, json=response_for(valid_output()))
        return httpx.Response(status, headers={"Retry-After": "0"}, json={})

    analyzer = adapter(
        handler,
        max_retries=2,
        max_provider_requests=3,
        sleep_fn=sleeps.append,
    )

    result = analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert isinstance(result, GlobalAiAnalysisContract)
    assert analyzer.request_count == 2
    assert sleeps == [0.0]


def test_gemini_final_429_exposes_only_safe_quota_metadata_and_halts_run():
    body = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "message": API_KEY,
            "details": [
                {
                    "violations": [
                        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                    ]
                }
            ],
        }
    }
    analyzer = adapter(
        lambda request: httpx.Response(
            429, headers={"Retry-After": "0"}, json=body
        ),
        max_retries=1,
        max_provider_requests=2,
    )

    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    diagnostics = caught.value.diagnostics
    assert diagnostics["reason_code"] == "GEMINI_DAILY_QUOTA_EXHAUSTED"
    assert diagnostics["request_attempts"] == 1
    assert diagnostics["provider_requests_total"] == 1
    assert diagnostics["halt_provider_for_run"] is True
    assert diagnostics["provider_status"] == "RESOURCE_EXHAUSTED"
    assert diagnostics["quota_ids"] == [
        "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    ]
    assert_no_key(diagnostics)


def test_gemini_never_exceeds_explicit_provider_request_budget():
    analyzer = adapter(
        lambda request: httpx.Response(503, json={}),
        max_retries=2,
        max_provider_requests=1,
    )

    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert analyzer.request_count == 1
    assert caught.value.diagnostics["reason_code"] == "GEMINI_SERVER_ERROR"
    assert caught.value.diagnostics["request_attempts"] == 1


def test_gemini_minute_limit_uses_one_bounded_cooldown_then_continues():
    sleeps = []
    statuses = [429, 429, 200]

    def handler(request):
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(200, json=response_for(valid_output()))
        return httpx.Response(
            status,
            headers={"Retry-After": "90"},
            json={"error": {"status": "RESOURCE_EXHAUSTED", "details": [{
                "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]
            }]}},
        )

    analyzer = adapter(
        handler,
        max_retries=1,
        sleep_fn=sleeps.append,
        rate_limit_cooldown_seconds=60,
    )

    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert analyzer.request_count == 2
    assert sleeps == [60]
    assert caught.value.diagnostics["retry_after_seconds"] == 90.0
    assert caught.value.diagnostics["halt_provider_for_run"] is False

    result = analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    assert isinstance(result, GlobalAiAnalysisContract)
    assert analyzer.request_count == 3
    assert sleeps == [60]


def test_shared_governor_paces_requests_across_candidates_and_adapters():
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    governor = GeminiRequestGovernor(
        minimum_interval_seconds=4.1,
        max_run_seconds=100,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )
    handler = lambda request: httpx.Response(200, json=response_for(valid_output()))
    first = adapter(handler, governor=governor)
    second = adapter(handler, governor=governor)

    first.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    second.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert clock.now == pytest.approx(4.1)


def test_consecutive_503s_recover_and_later_candidate_is_still_analyzed():
    statuses = [503, 503, 200, 200]

    def handler(request):
        status = statuses.pop(0)
        return (
            httpx.Response(200, json=response_for(valid_output()))
            if status == 200
            else httpx.Response(status, json={"error": {"status": "UNAVAILABLE"}})
        )

    analyzer = adapter(handler, max_retries=2, max_provider_requests=5)
    first = analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    second = analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert isinstance(first, GlobalAiAnalysisContract)
    assert isinstance(second, GlobalAiAnalysisContract)
    assert analyzer.request_count == 4


def test_503_retry_after_is_capped():
    statuses = [503, 200]
    sleeps = []

    def handler(request):
        status = statuses.pop(0)
        return (
            httpx.Response(200, json=response_for(valid_output()))
            if status == 200
            else httpx.Response(503, headers={"Retry-After": "900"}, json={})
        )

    analyzer = adapter(handler, max_retries=1, sleep_fn=sleeps.append)
    analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert sleeps == [10.0]


def test_shared_governor_enforces_maximum_run_time():
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    governor = GeminiRequestGovernor(
        minimum_interval_seconds=1,
        max_run_seconds=10,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )
    analyzer = adapter(
        lambda request: httpx.Response(200, json=response_for(valid_output())),
        governor=governor,
    )
    analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)
    clock.now = 11

    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert caught.value.diagnostics["reason_code"] == "GEMINI_RUN_DEADLINE_EXCEEDED"
    assert caught.value.diagnostics["halt_provider_for_run"] is True


@pytest.mark.parametrize(
    ("response", "reason_code"),
    [
        ({"candidates": []}, "NO_CANDIDATES"),
        ({"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}, "SAFETY_BLOCKED"),
        (response_for(""), "EMPTY_CONTENT"),
        (response_for(valid_output(), finish_reason="MAX_TOKENS"), "TRUNCATED_OUTPUT"),
        (response_for("{not-json"), "INVALID_JSON"),
        (response_for({"headline": "不完整"}), "SCHEMA_VALIDATION_FAILED"),
    ],
)
def test_gemini_output_failures_are_separately_recorded(response, reason_code):
    analyzer = adapter(lambda request: httpx.Response(200, json=response))

    with pytest.raises(AnalysisOutputError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert caught.value.diagnostics["reason_code"] == reason_code


def test_gemini_rejects_evidence_mismatch_after_structured_output():
    output = valid_output(
        evidence=[{"quote": "invented quote 999", "source_url": SOURCE_URL}]
    )
    analyzer = adapter(lambda request: httpx.Response(200, json=response_for(output)))

    with pytest.raises(EvidenceMismatch) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert caught.value.diagnostics[0]["model_quote"] == "invented quote 999"


def test_gemini_rejects_nonofficial_base_url_and_invalid_model():
    with pytest.raises(ValueError, match="GEMINI_BASE_URL"):
        GeminiGlobalAiAnalysisAdapter(
            api_key=API_KEY,
            base_url="https://example.com/v1beta",
            model="gemini-3.5-flash",
            prompt_path=PROMPT_PATH,
        )
    with pytest.raises(ValueError, match="GEMINI_ANALYSIS_MODEL"):
        GeminiGlobalAiAnalysisAdapter(
            api_key=API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-3.5-flash?key=secret",
            prompt_path=PROMPT_PATH,
        )


def test_prompt_path_and_adapter_version_cannot_drift(tmp_path):
    wrong = tmp_path / "daily_global_ai_analysis_v8.txt"
    wrong.write_text("unchanged prompt", encoding="utf-8")

    with pytest.raises(ValueError, match="prompt path/version mismatch"):
        GeminiGlobalAiAnalysisAdapter(
            api_key=API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-3.5-flash",
            prompt_path=wrong,
        )


def test_gemini_analysis_model_defaults_stable_and_flash_lite_is_explicit():
    default = Settings(_env_file=None)
    lite = Settings(_env_file=None, GEMINI_ANALYSIS_MODEL="gemini-3.5-flash-lite")

    assert default.gemini_analysis_model == "gemini-3.5-flash"
    assert lite.gemini_analysis_model == "gemini-3.5-flash-lite"
    assert default.gemini_fallback_model is None
    assert default.gemini_quota_fallback_enabled is False


def test_daily_quota_switches_once_to_configured_lower_model_and_records_it():
    seen_urls = []
    quota = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "details": [{
                "violations": [{
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
                }]
            }],
        }
    }

    def handler(request):
        seen_urls.append(str(request.url))
        assert request.headers["x-goog-api-key"] == API_KEY
        assert API_KEY not in str(request.url)
        assert API_KEY.encode() not in request.content
        if len(seen_urls) == 1:
            return httpx.Response(429, json=quota)
        return httpx.Response(200, json=response_for(valid_output()))

    controller = GeminiModelController(
        primary_model="gemini-3.5-flash",
        fallback_model="gemini-3.5-flash-lite",
        fallback_enabled=True,
    )
    analyzer = adapter(
        handler,
        model_controller=controller,
        max_provider_requests=3,
    )

    result = analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert isinstance(result, GlobalAiAnalysisContract)
    assert seen_urls[0].endswith("/models/gemini-3.5-flash:generateContent")
    assert seen_urls[1].endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert controller.models_used == ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
    assert controller.transitions == [{
        "from_model": "gemini-3.5-flash",
        "to_model": "gemini-3.5-flash-lite",
        "reason_code": "GEMINI_DAILY_QUOTA_FALLBACK",
        "quota_id": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    }]
    assert analyzer.request_count == 2


def test_daily_quota_does_not_switch_when_fallback_is_disabled():
    quota = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "details": [{
                "violations": [{
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
                }]
            }],
        }
    }
    controller = GeminiModelController(
        primary_model="gemini-3.5-flash",
        fallback_model="gemini-3.5-flash-lite",
        fallback_enabled=False,
    )
    analyzer = adapter(
        lambda request: httpx.Response(429, json=quota),
        model_controller=controller,
        max_provider_requests=2,
    )

    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert caught.value.diagnostics["reason_code"] == "GEMINI_DAILY_QUOTA_EXHAUSTED"
    assert controller.transitions == []


def test_fallback_daily_quota_stops_fail_closed_after_one_transition():
    quota = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "details": [{
                "violations": [{
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
                }]
            }],
        }
    }
    controller = GeminiModelController(
        primary_model="gemini-3.5-flash",
        fallback_model="gemini-3.5-flash-lite",
        fallback_enabled=True,
    )
    analyzer = adapter(
        lambda request: httpx.Response(429, json=quota),
        model_controller=controller,
        max_provider_requests=3,
    )

    with pytest.raises(ProviderCallError) as caught:
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert caught.value.diagnostics["reason_code"] == "GEMINI_DAILY_QUOTA_EXHAUSTED"
    assert caught.value.diagnostics["halt_provider_for_run"] is True
    assert analyzer.request_count == 2
    assert len(controller.transitions) == 1


def test_per_minute_quota_never_triggers_model_fallback():
    quota = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "details": [{
                "violations": [{
                    "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
                }]
            }],
        }
    }
    controller = GeminiModelController(
        primary_model="gemini-3.5-flash",
        fallback_model="gemini-3.5-flash-lite",
        fallback_enabled=True,
    )
    analyzer = adapter(
        lambda request: httpx.Response(429, json=quota),
        model_controller=controller,
        max_provider_requests=2,
    )

    with pytest.raises(ProviderCallError):
        analyzer.analyze(metadata={}, raw_text=RAW_TEXT, source_url=SOURCE_URL)

    assert controller.active_model == "gemini-3.5-flash"
    assert controller.transitions == []
