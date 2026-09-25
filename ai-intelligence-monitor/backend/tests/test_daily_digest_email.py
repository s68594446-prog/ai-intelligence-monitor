import json
import re

import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.daily_ai.email_digest import (
    deliver_latest,
    importance_stars,
    parse_digest_sections,
    render_bodies,
)
from app.delivery.contracts import EmailProviderResult


SECTORS = [
    "芯片与算力",
    "存储",
    "封装·设备·材料",
    "网络与光",
    "基础设施",
    "应用与需求端",
    "宏观政策",
]


def sample_digest() -> str:
    sections = []
    for index, sector in enumerate(SECTORS, 1):
        headline = "危险<script>alert(1)</script>" if index == 1 else f"产业新闻{index}"
        sections.append(
            f"""═══ 【{sector}】 ═══

【{headline}】

重要程度：{50 + index * 7}/100

公司披露了第{index}项可核验产业链变化 & 后续进展。

💡 分析：该事件影响{sector}环节，并提供新的供需信号。

📍 涉及：【{sector}】公司A、公司B；传导路径：需求 → 供给

⚠️ 风险与不确定性：计划尚未交付，实际规模仍需验证。

🔗 来源：https://example.com/source-{index}?a=1&b=2（转述报道，原始出处：公司公告）
　　其他来源：https://other.example.com/item-{index}、https://third.example.com/item-{index}
"""
        )
    return (
        "AI产业链每日重点\n\n"
        "分析元数据：Gemini 3.5 Flash · Prompt v9\n\n"
        + "\n───\n".join(sections)
    )


def test_email_parser_and_header_counts_match_digest():
    sections = parse_digest_sections(sample_digest())
    text, html = render_bodies("2026-08-09", sample_digest())

    assert len(sections) == 7
    assert sum(len(section.items) for section in sections) == 7
    assert text == sample_digest()
    assert "AI 产业链日报" in html
    assert "AI SUPPLY CHAIN INTELLIGENCE" in html
    assert "每日产业链关键变化与证据摘要" in html
    assert "分析模型 Gemini 3.5 Flash · Prompt v9" in html
    assert "background-color:#7a1724" in html
    assert "#0d2942" not in html
    assert "2026-08-09" in html
    assert re.search(r">7 <span[^>]*>条</span>", html)
    assert re.search(r">7 <span[^>]*>个</span>", html)
    assert "最高重要程度" in html
    assert "5/5" in html
    for sector in (
        "芯片与算力",
        "存储",
        "封装、设备与材料",
        "网络与光",
        "基础设施",
        "应用与需求端",
        "宏观政策",
    ):
        assert sector in html


def test_email_html_escapes_content_and_makes_sources_clickable():
    _, html = render_bodies("2026-08-09", sample_digest())

    assert "危险&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "变化 &amp; 后续进展" in html
    assert 'href="https://example.com/source-1?a=1&amp;b=2"' in html
    assert 'href="https://other.example.com/item-1"' in html
    assert 'href="https://third.example.com/item-1"' in html
    assert html.count("href=") == 21
    assert "发生了什么" in html
    assert "产业链解读" in html
    assert "涉及公司与传导路径" in html
    assert "风险与不确定性" in html
    assert "计划尚未交付，实际规模仍需验证。" in html
    assert "原始来源" in html
    assert "补充来源" in html


def test_email_is_self_contained_tracking_free_and_has_plain_alternative():
    body = sample_digest()
    text, html = render_bodies("2026-08-09", body)
    lowered = html.casefold()

    assert text == body
    assert "<script" not in lowered
    assert "<img" not in lowered
    assert "<style" not in lowered
    assert "@import" not in lowered
    assert "tracking" not in lowered
    assert "pixel" not in lowered
    assert "javascript:" not in lowered
    assert "内容仅用于产业研究与信息整理，不构成任何投资建议。" in html
    assert 'width="660"' in html
    assert 'role="presentation"' in html


@pytest.mark.parametrize(
    ("score", "stars"),
    [(59, 1), (60, 2), (69, 2), (70, 3), (79, 3), (80, 4),
     (89, 4), (90, 5), (100, 5)],
)
def test_importance_score_star_boundaries(score, stars):
    assert importance_stars(score) == stars


def test_runtime_metadata_is_escaped_not_hardcoded():
    body = sample_digest().replace(
        "Gemini 3.5 Flash", "Gemini <script>alert(1)</script>"
    )
    _, html = render_bodies("2026-09-01", body)

    assert "Gemini &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_unsafe_source_scheme_never_becomes_a_clickable_link():
    body = sample_digest().replace(
        "https://example.com/source-1?a=1&b=2",
        "javascript:alert(1)",
        1,
    )
    _, html = render_bodies("2026-09-01", body)

    assert "javascript:" not in html.casefold()
    assert html.count("href=") == 20


def test_email_body_does_not_render_internal_audit_appendix():
    reader_body = sample_digest()
    full_markdown = reader_body + "\n\n---\n## 审计（本次 run 自报告）\nSECRET_AUDIT"
    from app.daily_ai.email_digest import split_digest_body

    text, html = render_bodies("2026-08-09", split_digest_body(full_markdown))

    assert "审计" not in text
    assert "SECRET_AUDIT" not in html


def test_empty_digest_renders_zero_counts_without_fabricated_item():
    body = "AI产业链每日重点\n\n今日无达到标准的产业链重点新闻"
    text, html = render_bodies("2026-08-09", body)

    assert text == body
    assert "今日无达到标准的产业链重点新闻" in html
    assert re.search(r">0 <span[^>]*>条</span>", html)
    assert re.search(r">0 <span[^>]*>个</span>", html)


def test_html_header_discloses_actual_fallback_model_chain():
    body = sample_digest().replace(
        "Gemini 3.5 Flash · Prompt v9",
        "Gemini 3.5 Flash → Gemini 3.5 Flash Lite（额度降级） · Prompt v9",
    )

    _, html = render_bodies("2026-09-04", body)

    assert "Gemini 3.5 Flash → Gemini 3.5 Flash Lite（额度降级）" in html


def test_html_header_discloses_server_fallback_reason():
    body = sample_digest().replace(
        "Gemini 3.5 Flash · Prompt v9",
        "Gemini 3.5 Flash → Gemini 3.5 Flash Lite（服务故障降级） · Prompt v9",
    )

    _, html = render_bodies("2026-09-25", body)

    assert "Gemini 3.5 Flash → Gemini 3.5 Flash Lite（服务故障降级）" in html


def test_failed_ops_gate_blocks_email_before_smtp(tmp_path, monkeypatch):
    digest = tmp_path / "2026-09-01.md"
    digest.write_text(
        sample_digest()
        + '\n\n---\n## 审计\n### ops.json\n```json\n{"passed": false}\n```\n',
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, EMAIL_DELIVERY_ENABLED=True)
    monkeypatch.setattr(
        "app.daily_ai.email_digest.SMTPEmailDeliveryAdapter",
        lambda *_: (_ for _ in ()).throw(AssertionError("SMTP must not run")),
    )

    result = deliver_latest(
        settings,
        digests_dir=tmp_path,
        ledger_path=tmp_path / ".email-log.jsonl",
        send=True,
    )

    assert result["status"] == "BLOCKED"
    assert result["reasons"] == ["DIGEST_OPS_NOT_PASSED"]
    assert not (tmp_path / ".email-log.jsonl").exists()


def test_sent_ledger_records_message_id_domain_and_acceptance_without_addresses(
    tmp_path, monkeypatch
):
    digest = tmp_path / "2026-08-12.md"
    digest.write_text(
        sample_digest() + '\n\n---\n## 审计\n### ops.json\n```json\n{"passed": true}\n```\n',
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        EMAIL_DELIVERY_ENABLED=True,
        SMTP_HOST="smtp.example.com",
        SMTP_PORT=587,
        SMTP_USERNAME="sender@example.com",
        SMTP_PASSWORD=SecretStr("secret"),
        SMTP_FROM="sender@example.com",
        SMTP_TO="first@example.net,second@example.net",
    )

    class FakeSMTP:
        def __init__(self, settings):
            pass

        def send(self, request):
            return EmailProviderResult(provider_message_id=request.message_id)

    monkeypatch.setattr(
        "app.daily_ai.email_digest.SMTPEmailDeliveryAdapter", FakeSMTP
    )

    result = deliver_latest(
        settings,
        digests_dir=tmp_path,
        ledger_path=tmp_path / ".email-log.jsonl",
        send=True,
    )
    entries = [
        json.loads(line)
        for line in (tmp_path / ".email-log.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    assert result["status"] == "SENT"
    assert result["message_id"].startswith("<aim-digest-")
    assert entries[-1]["message_id"] == result["message_id"]
    assert entries[-1]["provider_message_id"] == result["message_id"]
    assert entries[-1]["sender_domain"] == "example.com"
    assert entries[-1]["smtp_accepted_at"]
    serialized = json.dumps(entries)
    assert "first@example.net" not in serialized
    assert "second@example.net" not in serialized
    assert "secret" not in serialized
