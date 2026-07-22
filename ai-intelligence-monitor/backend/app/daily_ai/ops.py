from app.daily_ai.contracts import AShareAnalysisContract, AShareOpsResult, TianApiPage
from app.daily_ai.discovery import official_source


PROHIBITED_ADVICE = ("买入", "卖出", "目标价", "建议增持", "建议减持")


def check_a_share_channel(*, tian: TianApiPage | None, analyses: list[AShareAnalysisContract], raw_text_by_url: dict[str, str], digest_items: list[dict]) -> AShareOpsResult:
    checks = {
        "tian_discovery_only": tian is None or all(not item.eligible_as_evidence and item.title_policy == "DISCOVERY_ONLY" for item in tian.items),
        "max_four_a_share": len(digest_items) <= 4,
        "official_primary_links": all(official_source(str(item.get("url", ""))) and item.get("source_tier") == "T1" for item in digest_items),
        "evidence_exact": all(all(e.quote in raw_text_by_url.get(str(e.source_url), "") for e in a.evidence) for a in analyses),
        "evidence_official": all(all(official_source(str(e.source_url)) for e in a.evidence) for a in analyses),
        "numeric_claims_cited": all(not a.contains_verifiable_numbers or any(any(ch.isdigit() for ch in e.quote) for e in a.evidence) for a in analyses),
        "no_investment_advice": all(not any(term in str(a.model_dump(mode="json")) for term in PROHIBITED_ADVICE) and not a.investment_advice for a in analyses),
        "tian_not_displayed": tian is None or all((not item.title or item.title not in str(digest_items)) and (not item.description or item.description not in str(digest_items)) for item in tian.items),
    }
    return AShareOpsResult(passed=all(checks.values()), checks=checks, errors=[name for name, passed in checks.items() if not passed])
