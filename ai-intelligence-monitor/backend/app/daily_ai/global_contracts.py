import difflib
import re
from typing import Literal

from pydantic import Field, model_validator

from app.daily_ai.contracts import EvidenceQuote, StrictModel


def normalize_ws(text: str) -> str:
    """Collapse runs of layout whitespace to one space; substantive characters untouched."""
    return re.sub(r"\s+", " ", text).strip()


def quote_matches(quote: str, raw_text: str) -> bool:
    """Verbatim containment after whitespace normalization on both sides.

    This keeps the verbatim-evidence discipline: any changed, added, or
    invented non-whitespace character still fails; only layout differences
    (newlines from HTML extraction, mixed-width spacing) stop causing false
    negatives.
    """
    return normalize_ws(quote) in normalize_ws(raw_text)


def closest_snippet(quote: str, raw_text: str, context: int = 60) -> dict:
    """Diagnostics for a failed quote match: the longest common run and its raw context."""
    raw_n, quote_n = normalize_ws(raw_text), normalize_ws(quote)
    match = difflib.SequenceMatcher(None, raw_n, quote_n, autojunk=False).find_longest_match(0, len(raw_n), 0, len(quote_n))
    start, end = max(0, match.a - context), min(len(raw_n), match.a + match.size + context)
    return {
        "quote_chars": len(quote_n),
        "longest_common_chars": match.size,
        "raw_closest_context": raw_n[start:end],
    }


GlobalTopic = Literal[
    "foundation_models", "ai_agents", "chips_compute", "cloud_platforms",
    "robotics_autonomy", "enterprise_ai", "consumer_ai", "open_source_research",
    "funding_ma", "regulation_governance", "china_ai", "global_competition",
]


NewsCategory = Literal[
    "COMPANY_RELEASE",       # major model/product/feature launches
    "BUSINESS_FUNDING_MA",   # funding, M&A, commercial moves
    "POLICY_REGULATION",     # regulation and policy originals
    "TECH_BREAKTHROUGH",     # significant deployed technical advances
    "RESEARCH_PAPER",        # academic papers (hard digest quota: max 1)
    "OTHER",
]


class GlobalAiAnalysisContract(StrictModel):
    headline: str = Field(min_length=1, max_length=300)
    topic: GlobalTopic
    news_category: NewsCategory
    # Section is decided by the actors and jurisdiction of the event itself,
    # never by the language of the article reporting it.
    region_section: Literal["GLOBAL", "CHINA"]
    region_basis: str = Field(min_length=1)
    what_happened: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    contains_verifiable_numbers: bool
    # FIRSTHAND_OFFICIAL only when the fetched evidence page itself is the
    # official announcer (a T1 domain). A domestic media retelling of a global
    # AI event is SECONDHAND_REPORT and must name the original source; it never
    # impersonates T1.
    sourcing: Literal["FIRSTHAND_OFFICIAL", "SECONDHAND_REPORT"]
    original_source: str = ""
    novelty: Literal["NEW_FACT", "REPEATED_STATEMENT", "UNKNOWN"]
    announced_vs_delivered: Literal["DELIVERED", "ANNOUNCED_ONLY", "UNKNOWN"]
    risks_and_counterevidence: list[str]
    evidence: list[EvidenceQuote] = Field(min_length=1)
    uncertainties: list[str]
    importance_score: int = Field(ge=0, le=100)
    importance_reason: str = Field(min_length=1)
    investment_advice: Literal[False] = False

    @model_validator(mode="after")
    def repeated_statement_not_important(self):
        if self.novelty == "REPEATED_STATEMENT" and self.importance_score >= 60:
            raise ValueError("a repeated statement cannot pass the importance threshold")
        return self

    @model_validator(mode="after")
    def secondhand_requires_original_source(self):
        if self.sourcing == "SECONDHAND_REPORT" and not self.original_source.strip():
            raise ValueError("a secondhand report must name its original source")
        return self


class GlobalOpsResult(StrictModel):
    version: Literal["global-ai-ops-v1"] = "global-ai-ops-v1"
    passed: bool
    checks: dict[str, bool]
    errors: list[str]
    stats: dict[str, int] = Field(default_factory=dict)
