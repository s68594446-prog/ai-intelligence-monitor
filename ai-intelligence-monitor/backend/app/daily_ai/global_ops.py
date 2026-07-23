from urllib.parse import urlsplit

from app.daily_ai.global_contracts import GlobalAiAnalysisContract, GlobalOpsResult, quote_matches
from app.daily_ai.global_digest import IMPORTANCE_THRESHOLD, SECTOR_CAP, TOTAL_LIMIT
from app.web_discovery.contracts import ClassifiedTier
from app.web_discovery.source_policy import WebSourcePolicy, classify_url


def _contains_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


PROHIBITED_ADVICE = (
    "买入", "卖出", "目标价", "建议增持", "建议减持", "建仓", "涨停",
    "price target", "buy rating", "sell rating", "should buy", "should sell",
)


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

    sector_counts: dict[str, int] = {}
    for item in digest_items:
        sector_counts[item.get("sector", "?")] = sector_counts.get(item.get("sector", "?"), 0) + 1
    checks = {
        "total_limit": len(digest_items) <= TOTAL_LIMIT,
        "sector_cap": all(count <= SECTOR_CAP for count in sector_counts.values()),
        # Papers have little chain-tracking value: at most one arXiv item.
        "arxiv_quota": sum((urlsplit(item.get("url", "")).hostname or "").endswith("arxiv.org") for item in digest_items) <= 1,
        # Every published item names a concrete materiality criterion.
        "important_has_criterion": all(
            a.importance_criterion != "NONE" for a in analyses if a.importance_score >= IMPORTANCE_THRESHOLD
        ),
        "evidence_exact": all(
            all(quote_matches(e.quote, raw_text_by_url.get(str(e.source_url), "")) for e in a.evidence) for a in analyses
        ),
        "evidence_tier": all(all(evidence_tier_ok(str(e.source_url)) for e in a.evidence) for a in analyses),
        "numeric_claims_cited": all(
            not a.contains_verifiable_numbers or any(any(ch.isdigit() for ch in e.quote) for e in a.evidence)
            for a in analyses
        ),
        "no_investment_advice": all(
            not a.investment_advice
            and not any(term in str(a.model_dump(mode="json")).casefold() for term in (t.casefold() for t in PROHIBITED_ADVICE))
            for a in analyses
        ),
        "no_repeated_statement_published": all(a.novelty != "REPEATED_STATEMENT" for a in analyses if a.importance_score >= IMPORTANCE_THRESHOLD),
        "region_basis_recorded": all(bool(a.region_basis.strip()) for a in analyses),
        # FIRSTHAND_OFFICIAL is legal only when every evidence page is itself a
        # T1 official domain; anything reached through media is SECONDHAND.
        "firsthand_tier_consistent": all(
            (a.sourcing == "FIRSTHAND_OFFICIAL")
            == all(classify_url(str(e.source_url), policy)[0] == ClassifiedTier.T1 for e in a.evidence)
            for a in analyses
        ),
        "secondhand_origin_noted": all(
            a.sourcing != "SECONDHAND_REPORT" or bool(a.original_source.strip()) for a in analyses
        ),
        # The reader-facing narrative must be Chinese (headline may be bilingual).
        "output_in_chinese": all(_contains_cjk(a.what_happened) and _contains_cjk(a.why_it_matters) for a in analyses),
    }
    return GlobalOpsResult(
        passed=all(checks.values()),
        checks=checks,
        errors=[name for name, passed in checks.items() if not passed],
        stats={
            "analyses_accepted": len(analyses),
            "analyses_discarded_evidence_mismatch": discarded_evidence_failures,
        },
    )
