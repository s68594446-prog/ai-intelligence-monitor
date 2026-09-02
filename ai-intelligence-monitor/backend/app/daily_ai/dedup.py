"""CJK-aware near-duplicate clustering for digest items.

The shared web_discovery clustering tokenizes on runs of word characters,
which works for space-separated languages but collapses an entire Chinese
title into one token — so two differently-worded Chinese reports of the same
event never overlap. This module clusters on character bigrams for CJK and
word tokens for Latin, which survives rewording.
"""
import re


_CJK = r"一-鿿㐀-䶿"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_LATIN_RUN = re.compile(r"[a-z0-9]+")
_CAPITALIZED_ENTITY = re.compile(
    r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9.-]{2,}(?![A-Za-z0-9])"
)
# Framing words that vary between outlets and carry no event identity.
_STOPWORDS = {
    "宣布", "发布", "表示", "称", "报道", "消息", "官方", "正式", "日前", "近日",
    "announces", "announced", "official", "update", "news", "says", "report",
    "the", "a", "an", "and", "of", "to", "in", "for", "with", "on", "at", "by",
}


def signature(text: str) -> set[str]:
    """Reword-tolerant fingerprint: CJK character bigrams plus Latin word tokens."""
    lowered = text.casefold()
    tokens: set[str] = set()
    for run in _CJK_RUN.findall(lowered):
        cleaned = run
        for word in _STOPWORDS:
            cleaned = cleaned.replace(word, "")
        tokens.update(cleaned[i:i + 2] for i in range(len(cleaned) - 1))
    tokens.update(
        token for token in _LATIN_RUN.findall(lowered)
        if len(token) > 1 and token not in _STOPWORDS
    )
    return tokens


def similarity(left: set[str], right: set[str]) -> float:
    """Overlap coefficient: tolerates one title carrying extra detail."""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


# Calibrated on the 2026-07-24 run: the true duplicate pair (two reports of the
# 阿里真武M890 event) scored 0.455, while the closest genuinely-different pair
# (AMD MI455X vs AMD Helios, same launch event, different products) scored
# 0.167. 0.30 sits in that gap.
DEFAULT_THRESHOLD = 0.30

# Same-company reports about one incident often use almost disjoint Chinese
# wording (for example "agent jailbreak", "sandbox escape", and "safety
# protocol overhaul").  Character overlap alone then misses the duplicate.
# These are event-identity anchors, not sentiment labels: they are used only
# when both texts also name the same capitalized entity.
_EVENT_ANCHOR_GROUPS = {
    "safety_incident": (
        "安全事件", "安全风险", "越狱", "沙盒", "失控", "入侵",
        "reward hacking", "rogue agent", "jailbreak",
    ),
    "supply_shortage": (
        "供应短缺", "产能短缺", "供不应求", "supply shortage",
    ),
    "regulatory_block": (
        "禁令", "禁用", "出口管制", "import ban", "export control",
    ),
}


def _entity_tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _CAPITALIZED_ENTITY.findall(text)
        if token.casefold() not in _STOPWORDS and token.casefold() not in {"the", "ai"}
    }


def _event_anchors(text: str) -> set[str]:
    lowered = text.casefold()
    return {
        name
        for name, terms in _EVENT_ANCHOR_GROUPS.items()
        if any(term.casefold() in lowered for term in terms)
    }


def same_anchored_event(left: str, right: str) -> bool:
    """Conservative duplicate signal: shared named entity and event anchor."""

    return bool(
        _entity_tokens(left) & _entity_tokens(right)
        and _event_anchors(left) & _event_anchors(right)
    )


def cluster_texts(texts: dict[str, str], *, threshold: float = DEFAULT_THRESHOLD) -> dict[str, str]:
    """Map each key to a cluster id; keys with similar text share an id."""
    signatures = {key: signature(text) for key, text in texts.items()}
    parent = {key: key for key in texts}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    keys = list(texts)
    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            if (
                similarity(signatures[left], signatures[right]) >= threshold
                or same_anchored_event(texts[left], texts[right])
            ):
                left_root, right_root = find(left), find(right)
                if left_root != right_root:
                    parent[right_root] = left_root
    return {key: find(key) for key in keys}
