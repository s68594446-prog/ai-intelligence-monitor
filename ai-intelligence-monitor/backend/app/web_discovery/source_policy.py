from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict

from app.web_discovery.contracts import ClassifiedTier


class WebSourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str
    coverage_id: str
    t1_domains: list[str]
    t2_domains: list[str]
    t3_domains: list[str]
    rules: dict


def load_source_policy(path: Path) -> WebSourcePolicy:
    policy = WebSourcePolicy.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    normalized = [domain.casefold().rstrip(".") for domains in (policy.t1_domains, policy.t2_domains, policy.t3_domains) for domain in domains]
    if len(normalized) != len(set(normalized)):
        raise ValueError("source policy contains duplicate domains")
    return policy


def _domain_matches(host: str, approved: str) -> bool:
    return host == approved or host.endswith("." + approved)


def classify_url(url: str, policy: WebSourcePolicy) -> tuple[ClassifiedTier, str]:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    for tier, domains in (
        (ClassifiedTier.T1, policy.t1_domains),
        (ClassifiedTier.T2, policy.t2_domains),
        (ClassifiedTier.T3, policy.t3_domains),
    ):
        if any(_domain_matches(host, domain.casefold().rstrip(".")) for domain in domains):
            return tier, host
    return ClassifiedTier.UNVERIFIED, host
