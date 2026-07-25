import re
from urllib.parse import urlsplit

from app.daily_ai.global_contracts import (
    IMPORTANCE_THRESHOLD,
    GlobalAiAnalysisContract,
    GlobalOpsResult,
    quote_matches,
)
from app.daily_ai.global_digest import SECTOR_CAP, TOTAL_LIMIT
from app.web_discovery.contracts import ClassifiedTier
from app.web_discovery.source_policy import WebSourcePolicy, classify_url


def _contains_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


CHINESE_NUMERALS = set("〇零一二两三四五六七八九十百千万亿兆")
# Quantities are not always ASCII digits: Chinese numerals ("数千万种子轮") and
# English number words ("up to two gigawatts") carry them too. A quote using
# any of these forms does cite its number.
ENGLISH_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "dozen", "twenty", "thirty", "forty",
    "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
    "million", "billion", "trillion", "half", "double", "triple", "quarter",
)
_ENGLISH_NUMBER_RE = re.compile(r"\b(" + "|".join(ENGLISH_NUMBER_WORDS) + r")\b", re.IGNORECASE)
# Numbers as they appear in narrative prose, for diagnostics.
NUMBER_IN_PROSE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|percent|[A-Za-z]{1,3}\b)?|[〇零一二两三四五六七八九十百千万亿兆]+"
)


def quote_carries_number(quote: str) -> bool:
    if any(ch.isdigit() or ch in CHINESE_NUMERALS for ch in quote):
        return True
    return bool(_ENGLISH_NUMBER_RE.search(quote))


# Pure judgement/advice words: banned unconditionally. Mapping a company onto a
# supply-chain link is a fact and stays allowed; saying it benefits is not.
PROHIBITED_ADVICE = (
    "利好", "利空", "看好", "看空", "唱多", "唱空",
    "建议关注", "值得关注", "可关注", "可布局", "布局机会", "建议买入", "建议卖出",
    "建议增持", "建议减持", "目标价", "估值提升", "抄底", "加仓", "减仓", "持仓",
    "涨停", "跌停", "股价", "股票代码", "受益股", "概念股炒作",
    "price target", "buy rating", "sell rating", "overweight rating",
    "should buy", "should sell", "bullish on", "bearish on",
)

# Direction words (上涨/下跌/涨幅/跌幅) are legitimate when they describe
# product or commodity prices — PRICE_MOVE is a core materiality criterion
# ("DRAM 合约价上涨20%" is a fact). They are prohibited only when aimed at
# equities. These patterns catch that pairing in either order.
_EQUITY_CONTEXT = r"(股价|股票|个股|市值|股市|A股|港股|美股|大盘|板块指数)"
_DIRECTION = r"(上涨|下跌|涨幅|跌幅|走高|走低|大涨|大跌)"
PROHIBITED_EQUITY_PATTERNS = (
    re.compile(_EQUITY_CONTEXT + r"[^。；\n]{0,15}" + _DIRECTION),
    re.compile(_DIRECTION + r"[^。；\n]{0,15}" + _EQUITY_CONTEXT),
)


def narrative_text(item: GlobalAiAnalysisContract) -> str:
    """Only model-authored prose. Evidence quotes are verbatim source text: an
    article about equity rules legitimately contains equity words, and judging
    our own evidence by the advice rules would flag the source, not the model."""
    parts = [
        item.headline, item.what_happened, item.why_it_matters,
        item.importance_reason, item.transmission_path, item.region_basis,
        *item.risks_and_counterevidence, *item.uncertainties,
        *[company for entry in item.chain_mapping for company in entry.companies],
    ]
    return " ".join(part for part in parts if part)


def advice_violations(item: GlobalAiAnalysisContract) -> list[str]:
    """Matched prohibited terms/patterns, so a failure names its trigger."""
    payload = narrative_text(item)
    lowered = payload.casefold()
    matched = [term for term in PROHIBITED_ADVICE if term.casefold() in lowered]
    matched += [
        match.group(0) for pattern in PROHIBITED_EQUITY_PATTERNS
        for match in [pattern.search(payload)] if match
    ]
    if item.investment_advice:
        matched.append("investment_advice=true")
    return matched


def check_global_channel(
    *,
    analyses: list[GlobalAiAnalysisContract],
    raw_text_by_url: dict[str, str],
    digest_items: list[dict],
    policy: WebSourcePolicy,
    discarded_evidence_failures: int = 0,
) -> GlobalOpsResult:
    def evidence_tier_ok(url: str) -> bool:
        return classify_url(url, policy)[0] in {ClassifiedTier.T1, ClassifiedTier.T2}

    def numeric_cited(item: GlobalAiAnalysisContract) -> bool:
        return not item.contains_verifiable_numbers or any(quote_carries_number(e.quote) for e in item.evidence)

    def numeric_diagnosis(item: GlobalAiAnalysisContract) -> str:
        """Name the claimed numbers and the quotes, so the cause is visible."""
        claimed = NUMBER_IN_PROSE.findall(f"{item.what_happened} {item.why_it_matters}")
        quotes = " ｜ ".join(e.quote[:60] for e in item.evidence)
        return (
            f"{item.headline[:60]} → 声称数字：{'/'.join(claimed[:5]) or '(正文未见数字)'}"
            f" → 实际quote：{quotes[:160] or '(无)'}"
        )

    # Reader-facing rules gate on what actually reaches the reader. An analysis
    # that was filtered or deduped out is never published, so it cannot violate
    # a reader-facing rule — but its findings are still reported below rather
    # than hidden. Evidence exactness applies to every analysis we produced.
    published_urls = {item.get("url") for item in digest_items}
    published = [a for a in analyses if str(a.evidence[0].source_url) in published_urls] if digest_items else []
    unpublished = [a for a in analyses if a not in published]

    def reader_findings(items: list[GlobalAiAnalysisContract]) -> dict[str, list[str]]:
        return {
            "numeric_claims_cited": [numeric_diagnosis(a) for a in items if not numeric_cited(a)],
            "no_investment_advice": [
                f"{a.headline[:70]} → 命中：{'/'.join(advice_violations(a))}"
                for a in items if advice_violations(a)
            ],
            "output_in_chinese": [
                a.headline[:80] for a in items
                if not (_contains_cjk(a.what_happened) and _contains_cjk(a.why_it_matters))
            ],
        }

    offenders = reader_findings(published)
    offenders["evidence_exact"] = [
        a.headline[:80] for a in analyses
        if not all(quote_matches(e.quote, raw_text_by_url.get(str(e.source_url), "")) for e in a.evidence)
    ]
    unpublished_findings = {name: items for name, items in reader_findings(unpublished).items() if items}

    sector_counts: dict[str, int] = {}
    for item in digest_items:
        sector_counts[item.get("sector", "?")] = sector_counts.get(item.get("sector", "?"), 0) + 1
    checks = {
        "total_limit": len(digest_items) <= TOTAL_LIMIT,
        "sector_cap": all(count <= SECTOR_CAP for count in sector_counts.values()),
        "arxiv_quota": sum((urlsplit(item.get("url", "")).hostname or "").endswith("arxiv.org") for item in digest_items) <= 1,
        "important_has_criterion": all(
            a.importance_criterion != "NONE" for a in analyses if a.importance_score >= IMPORTANCE_THRESHOLD
        ),
        "evidence_exact": not offenders["evidence_exact"],
        "evidence_tier": all(all(evidence_tier_ok(str(e.source_url)) for e in a.evidence) for a in analyses),
        "numeric_claims_cited": not offenders["numeric_claims_cited"],
        "no_investment_advice": not offenders["no_investment_advice"],
        "no_repeated_statement_published": all(
            a.novelty != "REPEATED_STATEMENT" for a in analyses if a.importance_score >= IMPORTANCE_THRESHOLD
        ),
        "region_basis_recorded": all(bool(a.region_basis.strip()) for a in analyses),
        "firsthand_tier_consistent": all(
            (a.sourcing == "FIRSTHAND_OFFICIAL")
            == all(classify_url(str(e.source_url), policy)[0] == ClassifiedTier.T1 for e in a.evidence)
            for a in analyses
        ),
        "secondhand_origin_noted": all(
            a.sourcing != "SECONDHAND_REPORT" or bool(a.original_source.strip()) for a in analyses
        ),
        "output_in_chinese": not offenders["output_in_chinese"],
    }
    return GlobalOpsResult(
        passed=all(checks.values()),
        unpublished_findings=unpublished_findings,
        checks=checks,
        errors=[name for name, passed in checks.items() if not passed],
        offenders={name: items for name, items in offenders.items() if items},
        stats={
            "analyses_accepted": len(analyses),
            "analyses_discarded_evidence_mismatch": discarded_evidence_failures,
        },
    )
