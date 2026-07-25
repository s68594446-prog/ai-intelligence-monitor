"""Deterministic output-quality screen for model-authored narrative text.

Catches degenerate generation (repeated-phrase lock-ups, runaway token
repetition) and encoding damage before it can reach the digest. A failing
item is discarded fail-soft with a reason code; it never rewrites text.
"""
import re


REPLACEMENT_CHAR = "�"
# CJK text has no spaces, so degeneration shows up as a repeated substring
# rather than a repeated word. Both forms are checked.
_CJK_RUN = re.compile(r"([一-鿿]{2,6})\1{2,}")          # 装置装置装置
_LATIN_RUN = re.compile(r"\b(\w+)(?:\s+\1\b){2,}", re.IGNORECASE)  # foo foo foo
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _token_repetition_ratio(text: str) -> float:
    """Share of the most frequent 2-gram among all 2-grams (CJK-aware)."""
    grams = [text[i:i + 2] for i in range(len(text) - 1) if not text[i].isspace()]
    if len(grams) < 20:
        return 0.0
    counts: dict[str, int] = {}
    for gram in grams:
        counts[gram] = counts.get(gram, 0) + 1
    return max(counts.values()) / len(grams)


# Market-action vocabulary. An item whose HEADLINE leads with these is a
# stock-quote story, not supply-chain news, and is dropped before it can reach
# the digest or trip the advice check. The same words inside the body are not
# grounds for dropping — the model may legitimately describe context.
MARKET_ACTION_TERMS = (
    # price/index action
    "逆势反弹", "涨停", "跌停", "大涨", "大跌", "暴涨", "暴跌", "板块异动",
    "集体走强", "集体走弱", "领涨", "领跌", "主力资金", "北向资金", "换手率",
    "成交额", "股价上涨", "股价下跌", "市值蒸发", "创新高", "创新低",
    "stocks rally", "stocks slump", "shares jump", "shares plunge",
    "stock surge", "market rally", "selloff",
    # broker research, ratings and position advice
    "券商", "研报", "评级", "目标价", "调仓", "加仓", "减仓", "配置建议",
    "投资策略", "投资建议", "金股", "首次覆盖", "上调评级", "下调评级",
    "买入评级", "卖出评级", "增持评级", "减持评级", "看多", "看空", "唱多", "唱空",
    "analyst rating", "price target", "upgrades to buy", "downgrades to",
    "initiates coverage", "overweight", "underweight",
    # investment-banking process gossip
    "承销商", "保荐机构", "保荐人", "承销团", "路演", "询价",
    "underwriter", "bookrunner", "lead manager",
)


def is_market_action_headline(headline: str) -> str | None:
    """Return the matched market-action term when the headline is a quote story."""
    lowered = headline.casefold()
    return next((term for term in MARKET_ACTION_TERMS if term.casefold() in lowered), None)


def check_text_quality(fields: dict[str, str]) -> list[str]:
    """Return reason codes for unusable narrative text; empty means acceptable."""
    reasons: list[str] = []
    for name, value in fields.items():
        if not value:
            continue
        if REPLACEMENT_CHAR in value:
            reasons.append(f"MOJIBAKE:{name}")
        if _CONTROL.search(value):
            reasons.append(f"CONTROL_CHARS:{name}")
        if _CJK_RUN.search(value) or _LATIN_RUN.search(value):
            reasons.append(f"REPEATED_PHRASE:{name}")
        elif _token_repetition_ratio(value) > 0.12:
            reasons.append(f"DEGENERATE_REPETITION:{name}")
    return sorted(set(reasons))
