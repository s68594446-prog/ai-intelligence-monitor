import httpx
import pytest

from app.daily_ai.article_integrity import DailyArticleExtractor, source_integrity_issues
from app.web_discovery.fetch import WebPageFetcher
from test_web_discovery import result, PUBLIC_IP


def fetch(markup, *, daily=True):
    candidate = result()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, text=markup, headers={"content-type": "text/html"}))) as client:
        kwargs = {"text_extractor_factory": DailyArticleExtractor} if daily else {}
        return WebPageFetcher(client=client, resolver=lambda _: PUBLIC_IP,
                              minimum_interval_seconds=0, **kwargs).fetch(
            candidate, allowed_urls={candidate.canonical_url})


def test_article_wins_over_menus_footer_and_related_items_without_breaking_inline_quotes():
    paragraph = "The company announced CNY<b>33 billion</b> in planned funding. " * 4
    markup = f'<html><head><title>Article title</title></head><body><nav>MENU $900 billion</nav><main><div>Index</div><article><h1>Funding</h1><p>{paragraph}</p><aside>RELATED $700 billion</aside></article></main><footer>FOOTER $800 billion</footer></body></html>'
    got = fetch(markup)
    assert got.status.value == "SUCCESS"
    assert "CNY33 billion" in got.raw_text
    assert not any(s in got.raw_text for s in ("MENU", "RELATED", "FOOTER", "Index"))
    assert got.title == "Article title"
    assert got.security_checks["dns_pinned_connection"]
    # The shared fetcher's existing callers retain the original extractor.
    assert "MENU" in fetch(markup, daily=False).raw_text


def test_menu_cannot_make_short_teaser_look_like_full_article():
    got = fetch('<nav>' + 'Menu menu ' * 100 + '</nav><article>Short teaser.</article>')
    assert got.error_code == "TEXT_TOO_SHORT"


def test_paid_notice_outside_article_blocks_even_with_long_navigation():
    got = fetch('<article>' + 'Teaser text. ' * 20 + '</article><footer>The article requires paid subscription.</footer>')
    assert got.error_code == "SOURCE_INCOMPLETE"
    assert got.raw_text == ""


@pytest.mark.parametrize("wrapper", ["main", 'div itemprop="articleBody"', "div"])
def test_complete_official_or_legacy_page_without_paywall_is_usable(wrapper):
    got = fetch(f'<{wrapper}>' + '<p>Verified manufacturing details and shipment information.</p>' * 5 + f'</{wrapper.split()[0]}>')
    assert got.status.value == "SUCCESS"


def test_newsletter_subscription_is_not_article_access_denial():
    got = fetch('<article>' + '<p>Complete public announcement.</p>' * 10 + '</article><footer>Subscribe to our newsletter.</footer>')
    assert got.status.value == "SUCCESS"


@pytest.mark.parametrize("notice", ["The article requires paid subscription.", "Sign in to read the full article", "登录后阅读全文", "正文已截断", "[content truncated]"])
def test_explicit_partial_content_is_audited(notice):
    assert source_integrity_issues(notice) == ["SOURCE_INCOMPLETE"]


def test_hidden_widgets_and_multiple_articles_do_not_pollute_main_story():
    parser = DailyArticleExtractor()
    parser.feed('<article><p>Main story ' + 'verified detail ' * 20 + '</p><div hidden>SECRET</div><script>DEBUG</script></article><article>Related story</article>')
    text = '\n'.join(parser.text)
    assert 'Main story' in text
    assert not any(s in text for s in ('SECRET', 'DEBUG', 'Related story'))
