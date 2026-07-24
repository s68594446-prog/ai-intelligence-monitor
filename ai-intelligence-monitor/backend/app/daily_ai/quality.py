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
