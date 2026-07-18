from app.source_collections.contracts import (
    ApprovedSource,
    CandidateStatus,
    DiscoveredItem,
    PrefilterResult,
)


def prefilter(item: DiscoveredItem, source: ApprovedSource, coverage: dict) -> PrefilterResult:
    searchable = f"{item.title} {item.summary}".casefold()
    coverage_terms = list(coverage.get("products", [])) + list(coverage.get("monitoring_variables", []))
    configured = source.relevance_terms + coverage_terms
    matched = sorted({term for term in configured if term.casefold() in searchable}, key=str.casefold)
    excluded = sorted({term for term in source.exclusion_terms if term.casefold() in searchable}, key=str.casefold)
    if matched and excluded:
        status = CandidateStatus.NEEDS_MANUAL_REVIEW
        reason = "both relevance and exclusion terms matched"
    elif matched:
        status = CandidateStatus.CANDIDATE
        reason = f"{source.role.value} source matched approved DRAM/HBM terms"
    else:
        status = CandidateStatus.FILTERED_OUT
        reason = "no explicit DRAM/HBM or dependency-sentinel term matched"
    return PrefilterResult(
        canonical_url=item.canonical_url,
        candidate_status=status,
        matched_terms=matched,
        excluded_terms=excluded,
        match_reason=reason,
    )
