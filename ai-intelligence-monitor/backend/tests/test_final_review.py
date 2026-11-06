import json
from pathlib import Path

import pytest

from app.daily_ai.final_review import facts, review_fields, review_rendered, leak_findings
from app.daily_ai.email_digest import evaluate_digest, render_bodies

URL = "https://blogs.nvidia.com/blog/uk-ecosystem-deployment/"
# Exact archived 2026-10-15 quote; no fresh LLM or network needed to replay it.
SOURCE = "NVIDIA is putting that principle into practice, including through a five-year, $250 million compute commitment to strengthen the U.K.’s agentic safety and security research."


def test_historical_oct15_amount_conflict_is_held_with_source_trace():
    result = review_fields({"what": "五年期2.5亿美元算力承诺。",
                            "uncertainty": "原文未披露该五亿美元算力承诺的具体分年度拨付时间表。"}, URL, SOURCE)
    assert not result["passed"]
    assert result["findings"][0]["error_type"] == "AMOUNT_UNVERIFIED"
    claim = result["claims"][0]
    assert claim["amount"] == "2.5E+8"
    assert claim["currency"] == "USD"
    assert claim["unit"] == "compute_access"
    assert claim["period"] == "duration:5y"
    assert claim["fact_status"] == "commitment"
    assert claim["evidence_ids"]
    assert result["source_facts"][0]["original_text"] in SOURCE


@pytest.mark.parametrize("source,prose,passed", [
    ("A five-year $250 million compute commitment. $50 million each year.", "承诺五年提供2.5亿美元算力，每年5000万美元。", True),
    ("A five-year $250 million compute commitment.", "每年2.5亿美元算力承诺。", False),
    ("A five-year $250 million compute commitment.", "已支付2.5亿美元现金。", False),
    ("A five-year $250 million compute commitment.", "提供价值2.5亿美元现金。", False),
    ("Estimated spending is $250 million.", "支出2.5亿美元。", False),
    ("Estimated spending is $250 million.", "预计支出2.5亿美元。", True),
    ("At least $250 million.", "至少2.5亿美元。", True),
    ("At least $250 million.", "金额为2.5亿美元。", False),
    ("About $250 million.", "约2.5亿美元。", True),
    ("From $250 million to $300 million.", "2.5亿美元至3亿美元。", True),
    ("$250 million in 2025. $300 million in 2026.", "2025年2.5亿美元；2026年3亿美元。", True),
    ("China: $250 million. Global: $500 million.", "中国2.5亿美元；全球5亿美元。", True),
    ("Revenue was EUR 250 million.", "收入2.5亿美元。", False),
    ("Revenue was $250 million.", "收入250万美元。", False),
    ("Annual allocation of $50 million.", "原文未披露年度分配金额。", False),
    ("Annual allocation of $50 million.", "具体付款日期未披露。", True),
])
def test_comparable_basis_and_legitimate_differences(source, prose, passed):
    assert review_fields({"body": prose}, URL, source)["passed"] is passed


@pytest.mark.parametrize("value", ["250,000,000美元", "2.5亿美元", "两亿五千万美元", "$250 million"])
def test_equal_currency_units_normalize(value):
    assert facts(value, URL)[0]["amount"] == "2.5E+8"


@pytest.mark.parametrize("text", [
    "涉及CoreWeave、Meta、微软、英伟达、黑石等明确被raw_text提及的公司。",
    "&quot;raw_text&quot;: 数据", "Traceback (most recent call last)",
])
def test_historical_internal_field_leak(text):
    assert leak_findings(text)
    assert not review_fields({"analysis": text}, URL, "公开文本")["passed"]


def body(prose="五年期2.5亿美元算力承诺。"):
    return ("AI产业链每日重点\n\n═══ 【基础设施】 ═══\n\n【算力支持承诺】\n\n"
            f"重要程度：75/100\n\n{prose}\n\n💡 分析：研究支持。\n\n"
            f"🔗 来源：{URL}")


def save_reviewed(tmp_path, prose="五年期2.5亿美元算力承诺。"):
    content = body(prose)
    review = review_rendered(day="2026-10-15", body=content, raw_by_url={URL: SOURCE}, expected_urls={URL})
    path = tmp_path / "2026-10-15.md"
    report = {"passed": True, "audit": {"final_review": review}}
    path.write_text(content + '\n\n## 审计\n### ops.json\n```json\n' + json.dumps(report) + '\n```')
    return path, review


def test_rendered_review_zero_issues_and_internal_audit_stays_private(tmp_path):
    path, review = save_reviewed(tmp_path)
    assert review["passed"] and review["findings"] == []
    gate = evaluate_digest(path)
    assert gate.ok
    assert "claim_id" not in gate.body
    assert SOURCE not in gate.body


def test_missing_or_changed_final_review_cannot_send(tmp_path):
    path, _ = save_reviewed(tmp_path)
    path.write_text(path.read_text().replace("五年期2.5亿美元", "五年期5亿美元", 1))
    assert "FINAL_REVIEW_MISSING_OR_STALE" in evaluate_digest(path).reasons
    path.write_text(body() + '\n## 审计\n### ops.json\n```json\n{"passed":true}\n```')
    assert not evaluate_digest(path).ok


def test_html_only_renderer_corruption_blocks_review(monkeypatch):
    monkeypatch.setattr("app.daily_ai.email_digest.render_bodies",
                        lambda day, content: (content, "<p>raw_text</p>"))
    review = review_rendered(day="2026-10-15", body=body(), raw_by_url={URL: SOURCE})
    assert not review["passed"]
    assert {f["error_type"] for f in review["findings"]} >= {"HTML_CONTENT_MISMATCH", "INTERNAL_FIELD_LEAK"}


def test_extra_number_in_html_or_unparsed_body_is_not_silently_ignored(monkeypatch):
    def corrupt(day, content):
        text, markup = render_bodies(day, content)
        return text, markup.replace("</body>", "<p>$500 million</p></body>")
    monkeypatch.setattr("app.daily_ai.email_digest.render_bodies", corrupt)
    review = review_rendered(day="2026-10-15", body=body(), raw_by_url={URL: SOURCE})
    assert "RENDERED_NUMBER_MISMATCH" in {f["error_type"] for f in review["findings"]}


def test_actual_mime_inputs_are_rechecked_before_smtp(tmp_path, monkeypatch):
    from app.core.settings import Settings
    from app.daily_ai import email_digest
    from app.daily_ai.email_digest import deliver_latest
    save_reviewed(tmp_path)
    original = email_digest.build_request
    def corrupt(settings, day, content):
        request, errors, key = original(settings, day, content)
        return request.model_copy(update={"html_body": request.html_body + "raw_text"}), errors, key
    monkeypatch.setattr(email_digest, "build_request", corrupt)
    monkeypatch.setattr(email_digest, "SMTPEmailDeliveryAdapter", lambda *_: pytest.fail("no SMTP"))
    settings = Settings(_env_file=None, email_delivery_enabled=True, smtp_host="smtp.example.com",
                        smtp_port=587, smtp_username="sender@example.com", smtp_password="fake",
                        smtp_from="sender@example.com", smtp_to="reader@example.com")
    result = deliver_latest(settings, digests_dir=tmp_path, ledger_path=tmp_path / "ledger", send=True)
    assert result["reasons"] == ["FINAL_BODY_CHANGED"]
    assert not (tmp_path / "ledger").exists()


def test_nov6_ymtc_money_and_incomplete_source_are_independent_holds():
    fixture = json.loads((Path(__file__).parent / "fixtures/daily_20261106_money_review.json").read_text())
    url, raw = fixture["source_url"], fixture["source_excerpt"]
    bad = review_fields({"headline": fixture["incorrect_prose"]}, url, raw)
    assert not bad["passed"]
    assert bad["claims"][0]["amount"] == "3.3E+9"
    assert bad["findings"][0]["error_type"] == "AMOUNT_UNVERIFIED"
    good_units = review_fields({"body": fixture["unit_correct_prose"]}, url, raw)
    assert good_units["passed"]
    incomplete = review_fields({"body": fixture["unit_correct_prose"]}, url, raw + '\n' + fixture["access_notice"])
    assert not incomplete["passed"]
    assert incomplete["findings"][0]["error_type"] == "SOURCE_INCOMPLETE"
    for record in good_units["source_facts"]:
        a, b = record["source_span"]
        assert raw[a:b] == record["value_text"]
        assert record["source_url"] == url


@pytest.mark.parametrize("text,amount,currency", [
    ("330亿元人民币", "3.3E+10", "CNY"), ("330亿元", "3.3E+10", "CNY"),
    ("CNY33 billion", "3.3E+10", "CNY"), ("人民币330亿", "3.3E+10", "CNY"),
    ("一万亿美元", "1E+12", "USD"), ("两亿五千万美元", "2.5E+8", "USD"),
    ("港币25亿元", "2.5E+9", "HKD"), ("HK$250 million", "2.5E+8", "HKD"),
    ("€250 million", "2.5E+8", "EUR"), ("日元300亿", "3E+10", "JPY"),
])
def test_unit_and_currency_normalization(text, amount, currency):
    records = facts(text, URL)
    assert len(records) == 1
    assert (records[0]["amount"], records[0]["currency"]) == (amount, currency)


@pytest.mark.parametrize("source,prose,passed", [
    ("From $250 million to $300 million.", "2.5–3亿美元。", True),
    ("Up to $250 million.", "不超过2.5亿美元。", True),
    ("Up to $250 million.", "至少2.5亿美元。", False),
    ("$250 million annually.", "五年共2.5亿美元。", False),
    ("$250 million.", "每年2.5亿美元。", False),
    ("$250 million in Q1 2026. $300 million in Q2 2026.", "2026年第一季度2.5亿美元；2026年第二季度3亿美元。", True),
    ("China: $250 million. Global: $500 million.", "全球2.5亿美元。", False),
    ("Revenue was $250 million, net profit was $50 million.", "净利润为2.5亿美元。", False),
    ("Revenue was $250 million, net profit was $50 million.", "营收2.5亿美元，净利润5000万美元。", True),
    ("A $250 million compute commitment, not cash.", "承诺2.5亿美元算力支持，并非现金。", True),
    ("A $250 million compute commitment.", "算力项目已支付2.5亿美元现金。", False),
    ("Paid out $250 million in compute access. A $250 million cash commitment.", "已支付2.5亿美元现金。", False),
    ("A $250 million compute commitment and paid out $50 million cash.", "已支付2.5亿美元现金。", False),
    ("CNY33 billion (approx. US$4.93 billion).", "49.3亿美元。", False),
    ("CNY33 billion (approx. US$4.93 billion).", "约49.3亿美元。", True),
    ("The U.K. pledged $250 million.", "英国承诺2.5亿美元。", True),
])
def test_comparable_claims_do_not_borrow_dimensions_from_other_facts(source, prose, passed):
    assert review_fields({"body": prose}, URL, source)["passed"] is passed


@pytest.mark.parametrize("alteration", [
    lambda b: b + '\n\n这段说明不能在HTML中消失。',
    lambda b: b.replace('💡 分析：研究支持。', ''),
    lambda b: '无法解析的摘要',
    lambda b: '今日无达到标准的产业链重点新闻\n但还有一段未经审核的说明',
])
def test_incomplete_or_unrendered_prose_is_held(alteration):
    review = review_rendered(day="2026-11-06", body=alteration(body()), raw_by_url={URL: SOURCE})
    assert not review["passed"]


@pytest.mark.parametrize("surface", ["text_body", "html_body"])
def test_actual_mime_non_numeric_change_is_blocked_before_ledger(tmp_path, monkeypatch, surface):
    from app.core.settings import Settings
    from app.daily_ai import email_digest
    save_reviewed(tmp_path)
    original = email_digest.build_request
    def corrupt(settings, day, content):
        request, errors, key = original(settings, day, content)
        return request.model_copy(update={surface: getattr(request, surface) + "未经审核的新说法"}), errors, key
    monkeypatch.setattr(email_digest, "build_request", corrupt)
    monkeypatch.setattr(email_digest, "SMTPEmailDeliveryAdapter", lambda *_: pytest.fail("no SMTP"))
    settings = Settings(_env_file=None, email_delivery_enabled=True, smtp_host="smtp.example.com",
                        smtp_port=587, smtp_username="sender@example.com", smtp_password="fake",
                        smtp_from="sender@example.com", smtp_to="reader@example.com")
    result = email_digest.deliver_latest(settings, digests_dir=tmp_path, ledger_path=tmp_path / "ledger", send=True)
    assert result["reasons"] == ["FINAL_BODY_CHANGED"]
    assert not (tmp_path / "ledger").exists()


def test_link_change_or_omitted_non_numeric_field_in_html_is_held(monkeypatch):
    def corrupt(day, content):
        text, markup = render_bodies(day, content)
        return text, markup.replace(URL, "https://example.org/other").replace("研究支持。", "")
    monkeypatch.setattr("app.daily_ai.email_digest.render_bodies", corrupt)
    review = review_rendered(day="2026-11-06", body=body(), raw_by_url={URL: SOURCE})
    assert {f["error_type"] for f in review["findings"]} >= {"HTML_CONTENT_MISMATCH", "RENDERED_LINK_MISMATCH"}


def test_stateful_renderer_cannot_replace_stored_seal_with_two_identical_bad_renders(tmp_path, monkeypatch):
    from app.core.settings import Settings
    from app.daily_ai import email_digest
    save_reviewed(tmp_path)
    calls = []
    def stateful(day, content):
        text, markup = render_bodies(day, content)
        calls.append(1)
        return text, markup if len(calls) == 1 else markup.replace("研究支持。", "发布未经审核的结论。")
    monkeypatch.setattr(email_digest, "render_bodies", stateful)
    monkeypatch.setattr(email_digest, "SMTPEmailDeliveryAdapter", lambda *_: pytest.fail("no SMTP"))
    settings = Settings(_env_file=None, email_delivery_enabled=True, smtp_host="smtp.example.com",
                        smtp_port=587, smtp_username="sender@example.com", smtp_password="fake",
                        smtp_from="sender@example.com", smtp_to="reader@example.com")
    result = email_digest.deliver_latest(settings, digests_dir=tmp_path, ledger_path=tmp_path / "ledger", send=True)
    assert result["reasons"] == ["FINAL_BODY_CHANGED"]
    assert not (tmp_path / "ledger").exists()


def test_review_seal_is_bound_to_date(tmp_path):
    path, _ = save_reviewed(tmp_path)
    changed = path.with_name("2026-10-16.md")
    changed.write_bytes(path.read_bytes())
    assert "FINAL_REVIEW_MISSING_OR_STALE" in evaluate_digest(changed).reasons
