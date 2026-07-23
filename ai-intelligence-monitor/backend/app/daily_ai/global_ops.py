from app.daily_ai.global_contracts import GlobalAiAnalysisContract, GlobalOpsResult
from app.daily_ai.global_digest import CHINA_LIMIT, GLOBAL_LIMIT, TOTAL_LIMIT
from app.web_discovery.contracts import ClassifiedTier
from app.web_discovery.source_policy import WebSourcePolicy, classify_url


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
) -> GlobalOpsResult:
    def evidence_tier_ok(url: str) -> bool:
        return classify_url(url, policy)[0] in {ClassifiedTier.T1, ClassifiedTier.T2}

    checks = {
        "global_limit": sum(item["section"] == "GLOBAL" for item in digest_items) <= GLOBAL_LIMIT,
        "china_limit": sum(item["section"] == "CHINA" for item in digest_items) <= CHINA_LIMIT,
        "total_limit": len(digest_items) <= TOTAL_LIMIT,
        "evidence_exact": all(
            all(e.quote in raw_text_by_url.get(str(e.source_url), "") for e in a.evidence) for a in analyses
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
        "no_repeated_statement_published": all(a.novelty != "REPEATED_STATEMENT" for a in analyses if a.importance_score >= 60),
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
    }
    return GlobalOpsResult(
        passed=all(checks.values()),
        checks=checks,
        errors=[name for name, passed in checks.items() if not passed],
    )
