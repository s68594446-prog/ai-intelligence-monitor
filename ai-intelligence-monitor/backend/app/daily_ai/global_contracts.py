from typing import Literal

from pydantic import Field, model_validator

from app.daily_ai.contracts import EvidenceQuote, StrictModel


GlobalTopic = Literal[
    "foundation_models", "ai_agents", "chips_compute", "cloud_platforms",
    "robotics_autonomy", "enterprise_ai", "consumer_ai", "open_source_research",
    "funding_ma", "regulation_governance", "china_ai", "global_competition",
]


class GlobalAiAnalysisContract(StrictModel):
    headline: str = Field(min_length=1, max_length=300)
    topic: GlobalTopic
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
