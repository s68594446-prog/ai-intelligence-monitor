import re

from app.daily_ai.email_digest import parse_digest_sections, render_bodies


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

公司披露了第{index}项可核验产业链变化 & 后续进展。

💡 分析：该事件影响{sector}环节，并提供新的供需信号。

📍 涉及：【{sector}】公司A、公司B；传导路径：需求 → 供给

🔗 来源：https://example.com/source-{index}?a=1&b=2（转述报道，原始出处：公司公告）
　　其他来源：https://other.example.com/item-{index}、https://third.example.com/item-{index}
"""
        )
    return "AI产业链每日重点\n\n" + "\n───\n".join(sections)


def test_email_parser_and_header_counts_match_digest():
    sections = parse_digest_sections(sample_digest())
    text, html = render_bodies("2026-08-09", sample_digest())

    assert len(sections) == 7
    assert sum(len(section.items) for section in sections) == 7
    assert text == sample_digest()
    assert "AI 产业链日报" in html
    assert "2026-08-09" in html
    assert re.search(r">7 <span[^>]*>条</span>", html)
    assert re.search(r">7 <span[^>]*>个</span>", html)
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
