"""Phase 3C article extraction and explicit incomplete-source detection.

This cannot certify completeness of arbitrary web pages. It rejects known access
barriers, keeps semantic article text, and never substitutes a search snippet.
"""
import re
from html.parser import HTMLParser


ACCESS_NOTICE = re.compile(
    r"(?:the|this) article requires (?:a )?paid subscription|"
    r"(?:subscribe|sign in|log in) to (?:continue|read (?:the )?(?:full|rest of))|"
    r"(?:全文|剩余内容|完整文章).{0,12}(?:需订阅|需要订阅|付费|登录后)|"
    r"(?:订阅|付费|登录)后(?:继续阅读|阅读全文)|本文仅为摘要|正文已截断|"
    r"\[(?:article|content|text) truncated\]", re.I)


def source_integrity_issues(text: str) -> list[str]:
    return ["SOURCE_INCOMPLETE"] if ACCESS_NOTICE.search(text) else []


class DailyArticleExtractor(HTMLParser):
    """Prefer article/articleBody, then main, otherwise cleaned legacy text.

    Paragraph chunks retain inline text without inserting line breaks into money
    or evidence quotes. Independent article containers never get concatenated.
    """
    version = "daily-article-v1"
    _hidden = {"script", "style", "noscript", "svg", "canvas", "iframe", "template"}
    _chrome = {"nav", "footer", "aside", "form"}
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _blocks = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "li", "tr", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.title, self.meta = [], {}
        self.all_text, self.legacy, self.regions = [], [], []

    def _append(self, data):
        self.legacy.append(data)
        for region in self.regions:
            if region["depth"] <= len(self.stack) and region["open"]:
                region["parts"].append(data)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "meta":
            key = (data.get("property") or data.get("name") or "").casefold()
            if key and data.get("content"):
                self.meta[key] = data["content"]
        if tag in self._blocks:
            self._append("\n")
        if tag not in self._void:
            parent_hidden = bool(self.stack and self.stack[-1][1])
            hidden = parent_hidden or tag in self._hidden or "hidden" in data or data.get("aria-hidden") == "true"
            parent_chrome = bool(self.stack and self.stack[-1][2])
            chrome = parent_chrome or tag in self._chrome
            self.stack.append((tag, hidden, chrome))
            rank = 2 if tag == "article" or data.get("itemprop") == "articleBody" else 1 if tag == "main" or data.get("role") == "main" else 0
            if rank and not hidden and not chrome:
                self.regions.append(dict(depth=len(self.stack), rank=rank, parts=[], open=True))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self._blocks:
            self._append("\n")
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is not None:
            for region in self.regions:
                if region["depth"] > index:
                    region["open"] = False
            del self.stack[index:]

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            return
        if any(item[0] == "title" for item in self.stack):
            self.title.append(data)
            return
        self.all_text.append(data)
        if not (self.stack and self.stack[-1][2]):
            self._append(data)

    @property
    def text(self):
        parts = max(self.regions, key=lambda r: (r["rank"], len("".join(r["parts"]))))["parts"] if self.regions else self.legacy
        return "".join(parts).splitlines()

    @property
    def error_code(self):
        # Access notices can sit outside the article container. Ignore scripts,
        # but do not discard a notice merely because it is inside a footer.
        return "SOURCE_INCOMPLETE" if source_integrity_issues(" ".join(self.all_text)) else None
