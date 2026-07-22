import hashlib
import re
from urllib.parse import urlsplit

from app.web_discovery.contracts import NormalizedSearchResult, ProviderSearchPage
from app.web_discovery.security import canonicalize_url
from app.web_discovery.source_policy import WebSourcePolicy, classify_url


def normalize_search_pages(
    pages: list[ProviderSearchPage], policy: WebSourcePolicy
) -> list[NormalizedSearchResult]:
    merged: dict[str, dict] = {}
    for page in pages:
        for hit in page.results:
            try:
                url = canonicalize_url(hit.url)
            except (ValueError, TypeError):
                continue
            current = merged.setdefault(
                url,
                {
                    "title": hit.title,
                    "snippet": hit.snippet,
                    "query_ids": [],
                    "best_rank": hit.rank,
                    "provider_provenance": [],
                },
            )
            if page.query_id not in current["query_ids"]:
                current["query_ids"].append(page.query_id)
            if hit.rank < current["best_rank"]:
                current.update(title=hit.title, snippet=hit.snippet, best_rank=hit.rank)
            provenance = {
                "provider": page.provider,
                "provider_version": page.adapter_version,
                "provider_request_id": page.provider_request_id,
                "query_id": page.query_id,
                "query_text_hash": page.query_text_hash,
                "rank": hit.rank,
                "published_at": hit.published_at,
                "discovered_at": hit.discovered_at,
                "provider_score": hit.provider_score,
                "source_metadata": hit.source_metadata,
                "raw_response_sha256": page.raw_response_sha256,
                "retrieval_status": hit.retrieval_status,
                "fallback_from": page.fallback_from,
                "snippet_policy": "DISCOVERY_ONLY",
            }
            if provenance not in current["provider_provenance"]:
                current["provider_provenance"].append(provenance)
    output = []
    for url, value in sorted(merged.items()):
        tier, domain = classify_url(url, policy)
        output.append(
            NormalizedSearchResult(
                candidate_id="web-" + hashlib.sha256(url.encode()).hexdigest()[:16],
                canonical_url=url,
                title=value["title"],
                snippet=value["snippet"],
                query_ids=sorted(value["query_ids"]),
                best_rank=value["best_rank"],
                source_tier=tier,
                source_policy_version=policy.policy_version,
                source_domain=domain,
                provider_provenance=sorted(
                    value["provider_provenance"],
                    key=lambda item: (item["provider"], item["query_id"], item["rank"]),
                ),
            )
        )
    return sorted(output, key=lambda item: (item.best_rank, item.canonical_url))


def event_cluster_key(title: str) -> str:
    tokens = re.findall(r"[a-z0-9\u4e00-\u9fff]+", title.casefold())
    normalized = " ".join(sorted(set(tokens))[:20])
    return "web-event-" + hashlib.sha256(normalized.encode()).hexdigest()[:20]


def cluster_event_keys(results: list[NormalizedSearchResult]) -> dict[str, str]:
    stopwords = {
        "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "the", "to", "with",
        "announces", "official", "update", "news", "says",
    }
    tokens = {
        item.candidate_id: {
            token for token in re.findall(r"[a-z0-9\u4e00-\u9fff]+", item.title.casefold())
            if len(token) > 1 and token not in stopwords
        }
        for item in results
    }
    parent = {item.candidate_id: item.candidate_id for item in results}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    ordered = sorted(tokens)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            overlap = tokens[left] & tokens[right]
            union_size = len(tokens[left] | tokens[right])
            if len(overlap) >= 3 and union_size and len(overlap) / union_size >= 0.35:
                union(left, right)
    groups: dict[str, list[str]] = {}
    for candidate_id in ordered:
        groups.setdefault(find(candidate_id), []).append(candidate_id)
    output = {}
    for members in groups.values():
        digest = hashlib.sha256("|".join(sorted(members)).encode()).hexdigest()[:20]
        for candidate_id in members:
            output[candidate_id] = "web-event-" + digest
    return output
