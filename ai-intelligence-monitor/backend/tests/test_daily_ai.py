import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from pypdf import PdfWriter

from app.daily_ai.contracts import AShareAnalysisContract, AShareUniverse, StableStatus
from app.daily_ai.digest import render_a_share_section, select_a_share_digest
from app.daily_ai.discovery import deduplicate_announcements, in_window, keyword_prefilter, official_source
from app.daily_ai.ops import check_a_share_channel
from app.daily_ai.pdf import SafeOfficialPdfFetcher, extract_pdf_text
from app.daily_ai.replay import frozen_output_replay, frozen_source_replay
from app.daily_ai.tianapi import TIANAPI_ENDPOINT, TianApiAiNewsAdapter, TianApiError
from app.daily_ai.universe import normalize_exchange


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_tianapi_success_is_discovery_only_and_fixed_endpoint():
    seen = {}
    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"code": 200, "msg": "success", "result": {"allnum": 8, "newslist": [{"id": "1", "url": "https://example.com/a", "title": "AI event", "description": "summary", "ctime": "2026-07-22", "source": "x"}]}})
    adapter = TianApiAiNewsAdapter(api_key=SecretStr("fake-secret-value"), client=client(handler))
    page = adapter.discover(num=3)
    assert seen["url"].startswith(TIANAPI_ENDPOINT)
    assert page.result_count == 1 and page.items[0].eligible_as_evidence is False
    assert page.remaining_quota_status == "UNAVAILABLE"
    assert "fake-secret-value" not in page.model_dump_json()


@pytest.mark.parametrize("code,expected", [(130, StableStatus.RATE_LIMITED), (140, StableStatus.PERMISSION_DENIED), (150, StableStatus.USAGE_LIMIT_REACHED), (160, StableStatus.API_NOT_ACTIVATED), (190, StableStatus.AUTHENTICATION_ERROR), (230, StableStatus.AUTHENTICATION_ERROR)])
def test_tianapi_http_200_business_errors_are_stable_and_secret_free(code, expected):
    adapter = TianApiAiNewsAdapter(api_key=SecretStr("fake-secret-value"), client=client(lambda request: httpx.Response(200, json={"code": code, "msg": "provider text"})))
    with pytest.raises(TianApiError) as caught:
        adapter.discover()
    assert caught.value.code == expected
    assert "fake-secret-value" not in repr(caught.value)


def test_tianapi_missing_key_and_one_request_limit():
    with pytest.raises(TianApiError) as caught:
        TianApiAiNewsAdapter(api_key=None)
    assert caught.value.code == StableStatus.MISSING_API_KEY
    with pytest.raises(ValueError):
        TianApiAiNewsAdapter(api_key=SecretStr("x"), client=client(lambda request: httpx.Response(200))).discover(num=6)


def test_official_universe_snapshot_is_deduplicated():
    path = Path(__file__).parents[2] / "config/daily_ai/a_share_universe.json"
    universe = AShareUniverse.model_validate_json(path.read_text())
    codes = [item.security_code for item in universe.constituents]
    assert universe.status == "VERIFIED"
    assert len(codes) == len(set(codes)) == 67
    assert sum(len(item.index_codes) == 2 for item in universe.constituents) == 13


def test_stale_universe_and_exchange_normalization():
    assert normalize_exchange("688001", "上海证券交易所") == "SSE"
    assert normalize_exchange("300001", "深圳证券交易所") == "SZSE"
    assert normalize_exchange("920001", "北京证券交易所") == "BSE"
    data = json.loads((Path(__file__).parents[2] / "config/daily_ai/a_share_universe.json").read_text())
    data["status"] = "STALE_UNIVERSE"
    assert AShareUniverse.model_validate(data).status == "STALE_UNIVERSE"


def test_announcement_window_source_keyword_and_dedupe():
    now = datetime.now(timezone.utc)
    assert in_window(now - timedelta(hours=47), now=now)
    assert not in_window(now - timedelta(hours=49), now=now)
    assert official_source("https://www.sse.com.cn/a.pdf")
    assert not official_source("https://eastmoney.com/a.pdf")
    assert keyword_prefilter("公司签署人工智能合同", ["人工智能"])
    base = {"security_code": "688001", "title": "某某股份有限公司重大合同公告", "announced_at": "2026-07-22", "url": "https://x"}
    result = deduplicate_announcements([{**base, "source": "CNINFO"}, {**base, "source": "SSE"}])
    assert len(result) == 1 and result[0]["source"] == "SSE"


def pdf_bytes(*, encrypted=False, pages=1):
    writer = PdfWriter()
    for _ in range(pages): writer.add_blank_page(width=100, height=100)
    if encrypted: writer.encrypt("secret")
    from io import BytesIO
    out = BytesIO(); writer.write(out); return out.getvalue()


def test_pdf_limits_encrypted_scanned_and_damaged():
    url = "https://www.sse.com.cn/a.pdf"
    assert extract_pdf_text(b"x" * 101, source_url=url, content_type="application/pdf", max_bytes=100).error_code == "PDF_TOO_LARGE"
    assert extract_pdf_text(pdf_bytes(encrypted=True), source_url=url, content_type="application/pdf").error_code == "ENCRYPTED_PDF"
    assert extract_pdf_text(pdf_bytes(), source_url=url, content_type="application/pdf").error_code == "NO_TEXT_LAYER"
    assert extract_pdf_text(b"%PDF-broken", source_url=url, content_type="application/pdf").error_code == "DAMAGED_PDF"
    assert extract_pdf_text(pdf_bytes(pages=3), source_url=url, content_type="application/pdf", max_pages=2).error_code == "PDF_TOO_MANY_PAGES"


def test_pdf_fetcher_rejects_nonofficial_and_private_dns_without_network():
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    fetcher = SafeOfficialPdfFetcher(client=httpx.Client(transport=transport), resolver=lambda host: ["127.0.0.1"])
    assert fetcher.fetch("https://evil.example/a.pdf").error_code == "NON_OFFICIAL_SOURCE"
    assert fetcher.fetch("https://www.sse.com.cn/a.pdf").error_code == "NON_PUBLIC_ADDRESS"
    assert not calls


def analysis(score=70, concept=False):
    return AShareAnalysisContract(company_name="测试公司", security_code="688001", exchange="SSE", announcement_type="重大合同", announced_at="2026-07-22", ai_relevance="CONCEPT_ONLY" if concept else "DIRECT", what_happened="公司披露可验证事项", contains_verifiable_numbers=True, direct_business_impact="存在直接合同影响", china_ai_chain_impact="影响待验证", novelty="NEW_FACT", concept_only=concept, risks_and_counterevidence=["框架协议不等于收入"], follow_up_points=["验证履约"], evidence=[{"quote":"原文证据100万元", "source_url":"https://www.sse.com.cn/a.pdf"}], uncertainties=["收入确认时间未知"], importance_score=score, importance_reason="官方公告含可验证数字", investment_advice=False)


def test_concept_filter_digest_limits_evidence_ops_and_no_advice():
    with pytest.raises(ValidationError): analysis(score=70, concept=True)
    items = select_a_share_digest([analysis()] * 5, china_existing_count=2)
    assert len(items) == 4
    assert "今日无达到标准" in render_a_share_section([])
    ops = check_a_share_channel(tian=None, analyses=[analysis()], raw_text_by_url={"https://www.sse.com.cn/a.pdf":"原文证据100万元"}, digest_items=items[:1])
    assert ops.passed


def test_replays_are_zero_network_and_stable():
    assert frozen_source_replay({"a": 1}, {"a": 1}) == {"stable": True, "network_requests": 0, "search_calls": 0, "deepseek_calls": 0}
    assert frozen_output_replay([{"a": 1}], [{"a": 1}])["stable"] is True
