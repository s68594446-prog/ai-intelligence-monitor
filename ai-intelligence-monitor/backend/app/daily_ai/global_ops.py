import re
from urllib.parse import urlsplit

from app.daily_ai.global_contracts import (
    IMPORTANCE_THRESHOLD,
    GlobalAiAnalysisContract,
    GlobalOpsResult,
    quote_matches,
)
from app.daily_ai.global_digest import SECTOR_CAP, TOTAL_LIMIT
from app.daily_ai.quality import check_text_quality
from app.web_discovery.contracts import ClassifiedTier
from app.web_discovery.source_policy import WebSourcePolicy, classify_url


def _contains_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def narrative_fields(item: GlobalAiAnalysisContract) -> dict[str, str]:
    """Model prose only: never translate or language-check exact source quotes.

    Company/source names and enum values may legally remain in English.
    Optional empty transmission/risk fields are honest, not missing news.
    """
    fields = {name: getattr(item, name) for name in (
        "headline", "what_happened", "why_it_matters", "importance_reason",
        "transmission_path",
    )}
    for name in ("risks_and_counterevidence", "uncertainties"):
        fields.update({f"{name}[{index}]": value for index, value in enumerate(getattr(item, name))})
    return fields


def output_language_violations(item: GlobalAiAnalysisContract) -> list[str]:
    """Same deterministic Chinese-prose screen at admission and final Ops.

    This catches missing Chinese, not every mixed-language or stylistic defect.
    It changes no text and does not consume an extra model request.
    """
    return [name for name, value in narrative_fields(item).items()
            if not (name == "transmission_path" and not value.strip())
            and not _contains_cjk(value)]


def output_quality_violations(item: GlobalAiAnalysisContract) -> list[str]:
    return check_text_quality(narrative_fields(item))


CHINESE_NUMERALS = set("〇零一二两三四五六七八九十百千万亿兆")
# Quantities are not always ASCII digits: Chinese numerals ("数千万种子轮") and
# English number words ("up to two gigawatts") carry them too. A quote using
# any of these forms does cite its number.
ENGLISH_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "dozen", "twenty", "thirty", "forty",
    "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
    "million", "billion", "trillion", "half", "double", "triple", "quarter",
    # Multiplicative adverbs/adjectives carry a quantity just as "double" does.
    # Their absence made "delivery times ... to as much as twice their previous
    # levels" read as an uncited number and blocked a whole digest (2026-07-28).
    "twice", "thrice", "twofold", "threefold", "fourfold", "fivefold", "tenfold",
)
# \b on both sides: "twicelike"/"tenfoldish" must not match.
_ENGLISH_NUMBER_RE = re.compile(r"\b(" + "|".join(ENGLISH_NUMBER_WORDS) + r")\b", re.IGNORECASE)

# Numbers as they appear in narrative prose, for diagnostics ONLY (never for
# pass/fail). CJK has no word boundaries, so a bare numeral class also matches
# the 三 in 三星 (Samsung) and the 一 in 进一步 — which made the diagnostic
# claim a digest asserted numbers it never asserted. Strip those first.
NUMBER_IN_PROSE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|percent|[A-Za-z]{1,3}\b)?|[〇零一二两三四五六七八九十百千万亿兆]+"
)
# Proper nouns and fixed expressions whose CJK numeral is not a quantity.
# Deliberately a short, literal list — this only cleans up a human-facing
# string, so it needs to stop the common noise, not to be exhaustive.
_CJK_NUMERAL_NOISE = (
    "三星", "统一", "第一", "一系列", "进一步", "一体", "一致", "一定", "一般",
    "一直", "一些", "一样", "一起", "一方面", "唯一", "十分", "百度", "万科",
)


def numbers_claimed_in_prose(text: str) -> list[str]:
    """Numeric claims a reader would see, with proper-noun/idiom noise removed."""
    for phrase in _CJK_NUMERAL_NOISE:
        text = text.replace(phrase, "　")
    return NUMBER_IN_PROSE.findall(text)


def quote_carries_number(quote: str) -> bool:
    if any(ch.isdigit() or ch in CHINESE_NUMERALS for ch in quote):
        return True
    return bool(_ENGLISH_NUMBER_RE.search(quote))


def numeric_claim_cited(item: GlobalAiAnalysisContract) -> bool:
    """An item claiming verifiable numbers must have a quote that carries one.

    THE single definition of this rule. The analysis stage calls it to discard
    the offending item fail-soft (discipline: one bad article is dropped with an
    audit record, it never takes the run or the digest down with it); ops calls
    it again as a backstop. Keep it here so the two can never drift apart.
    """
    return not item.contains_verifiable_numbers or any(
        quote_carries_number(e.quote) for e in item.evidence
    )


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

# Investor-style promotional framing can make a factually accurate digest read
# like a bullish note even without literal 利好/看好 language. Keep this list
# deliberately narrow: ordinary words such as 增长 or 强劲 remain legal when
# they report source facts, while explicit benefit/catalyst rhetoric is not.
NON_NEUTRAL_FRAMING = (
    "直接受益", "显著受益", "有望受益", "将受益于", "受益标的",
    "极高确定性", "强确定性", "增长红利", "需求红利", "注入动力",
    "行业背书", "构成催化", "催化剂", "打开增长空间", "释放增长潜力",
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


def neutrality_violations(item: GlobalAiAnalysisContract) -> list[str]:
    """Return narrow promotional phrases found in model-authored prose."""

    lowered = narrative_text(item).casefold()
    return [term for term in NON_NEUTRAL_FRAMING if term.casefold() in lowered]


def sourcing_tier_diagnostics(
    item: GlobalAiAnalysisContract,
    policy: WebSourcePolicy,
) -> dict[str, object]:
    """Return deterministic diagnostics for the model's sourcing label.

    FIRSTHAND_OFFICIAL is valid only when every cited evidence page is T1.
    Any T2 evidence makes the item SECONDHAND_REPORT.  The model may describe
    the original announcer, but it may not promote a media retelling to T1.
    """
    evidence = [
        {
            "url": str(entry.source_url),
            "tier": classify_url(str(entry.source_url), policy)[0].value,
        }
        for entry in item.evidence
    ]
    expected = (
        "FIRSTHAND_OFFICIAL"
        if evidence and all(entry["tier"] == ClassifiedTier.T1.value for entry in evidence)
        else "SECONDHAND_REPORT"
    )
    return {
        "headline": item.headline[:120],
        "actual_sourcing": item.sourcing,
        "expected_sourcing": expected,
        "evidence": evidence,
        "consistent": item.sourcing == expected,
    }


def sourcing_matches_tier(
    item: GlobalAiAnalysisContract,
    policy: WebSourcePolicy,
) -> bool:
    return bool(sourcing_tier_diagnostics(item, policy)["consistent"])


def check_global_channel(
    *,
    analyses: list[GlobalAiAnalysisContract],
    raw_text_by_url: dict[str, str],
    digest_items: list[dict],
    policy: WebSourcePolicy,
    discarded_evidence_failures: int = 0,
    analysis_provider_required: bool = False,
    analysis_attempts: int = 0,
    provider_validated_outputs: int = 0,
    fetched_count: int = 0,
    analysis_candidate_count: int = 0,
    provider_halted: bool = False,
    provider_failure_codes: list[str] | None = None,
) -> GlobalOpsResult:
    def evidence_tier_ok(url: str) -> bool:
        return classify_url(url, policy)[0] in {ClassifiedTier.T1, ClassifiedTier.T2}

    # Backstop only: the analysis stage already discards offenders one by one.
    # Shared definition — see numeric_claim_cited.
    numeric_cited = numeric_claim_cited

    def numeric_diagnosis(item: GlobalAiAnalysisContract) -> str:
        """Name the claimed numbers and the quotes, so the cause is visible."""
        claimed = numbers_claimed_in_prose(f"{item.what_happened} {item.why_it_matters}")
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

    # Presentation-quality checks (Chinese output, numeric citation) gate only
    # on what the reader sees; findings on unpublished items are recorded but
    # do not block. no_investment_advice is a COMPLIANCE red line — the system
    # must not PRODUCE advice at all — so it gates on every analysis, published
    # or not, with no backdoor. evidence_exact likewise applies to all.
    def presentation_findings(items: list[GlobalAiAnalysisContract]) -> dict[str, list[str]]:
        return {
            "numeric_claims_cited": [numeric_diagnosis(a) for a in items if not numeric_cited(a)],
            "output_in_chinese": [
                f"{a.headline[:80]} → {','.join(output_language_violations(a))}"
                for a in items if output_language_violations(a)
            ],
            "output_text_quality": [
                f"{a.headline[:80]} → {','.join(output_quality_violations(a))}"
                for a in items if output_quality_violations(a)
            ],
        }

    offenders = presentation_findings(published)
    offenders["no_investment_advice"] = [
        f"{a.headline[:70]} → 命中：{'/'.join(advice_violations(a))}"
        for a in analyses if advice_violations(a)
    ]
    offenders["neutral_language"] = [
        f"{a.headline[:70]} → 命中：{'/'.join(neutrality_violations(a))}"
        for a in analyses if neutrality_violations(a)
    ]
    offenders["evidence_exact"] = [
        a.headline[:80] for a in analyses
        if not all(quote_matches(e.quote, raw_text_by_url.get(str(e.source_url), "")) for e in a.evidence)
    ]
    offenders["firsthand_tier_consistent"] = [
        (
            f"{diagnostics['headline']} → actual={diagnostics['actual_sourcing']}, "
            f"expected={diagnostics['expected_sourcing']}, "
            f"tiers={','.join(str(entry['tier']) for entry in diagnostics['evidence'])}"
        )
        for a in analyses
        for diagnostics in [sourcing_tier_diagnostics(a, policy)]
        if not diagnostics["consistent"]
    ]
    unpublished_findings = {name: items for name, items in presentation_findings(unpublished).items() if items}

    sector_counts: dict[str, int] = {}
    for item in digest_items:
        sector_counts[item.get("sector", "?")] = sector_counts.get(item.get("sector", "?"), 0) + 1
    unprocessed_candidate_count = max(0, analysis_candidate_count - analysis_attempts)
    checks = {
        # A truly empty news day remains legal.  But once fetched sources were
        # sent for analysis, zero schema-validated provider outputs means the
        # provider path was unusable; that must not masquerade as an empty day.
        "analysis_provider_usable": (
            not analysis_provider_required
            or analysis_attempts == 0
            or provider_validated_outputs > 0
        ),
        # A low-yield but fully processed day is honest. A low-yield day caused
        # by a halted provider with candidates left untouched is incomplete and
        # must fail closed before publication/email.
        "analysis_coverage_complete": unprocessed_candidate_count == 0,
        # Dropping every schema-valid response for language failure is not an
        # honest empty day. A single bad article is still isolated fail-soft.
        "analysis_language_usable": not (
            analysis_provider_required and provider_validated_outputs > 0
            and (provider_failure_codes or []).count("OUTPUT_LANGUAGE_INVALID") >= provider_validated_outputs
        ),
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
        "neutral_language": not offenders["neutral_language"],
        "no_repeated_statement_published": all(
            a.novelty != "REPEATED_STATEMENT" for a in analyses if a.importance_score >= IMPORTANCE_THRESHOLD
        ),
        "region_basis_recorded": all(bool(a.region_basis.strip()) for a in analyses),
        "firsthand_tier_consistent": not offenders["firsthand_tier_consistent"],
        "secondhand_origin_noted": all(
            a.sourcing != "SECONDHAND_REPORT" or bool(a.original_source.strip()) for a in analyses
        ),
        "output_in_chinese": not offenders["output_in_chinese"],
        "output_text_quality": not offenders["output_text_quality"],
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
            "analysis_attempts": analysis_attempts,
            "provider_validated_outputs": provider_validated_outputs,
        },
        audit={
            "fetched_count": fetched_count,
            "analysis_candidate_count": analysis_candidate_count,
            "analysis_attempts": analysis_attempts,
            "validated_outputs": provider_validated_outputs,
            "provider_halted": provider_halted,
            "provider_failure_codes": sorted(set(provider_failure_codes or [])),
            "unprocessed_candidate_count": unprocessed_candidate_count,
        },
    )
