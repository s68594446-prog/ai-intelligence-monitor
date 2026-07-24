from app.daily_ai.dedup import cluster_texts
from app.daily_ai.global_contracts import IMPORTANCE_THRESHOLD, GlobalAiAnalysisContract


# 覆盖面要广，输出要精。The bar stays a real bar: a day with few (or zero)
# qualifying items is a legal, honest digest — never padded to fill space.
SECTOR_CAP = 5
TOTAL_LIMIT = 20

SECTOR_ORDER = [
    ("CHIPS_COMPUTE", "芯片与算力"),
    ("MEMORY_STORAGE", "存储"),
    ("PACKAGING_EQUIPMENT", "封装·设备·材料"),
    ("NETWORKING_OPTICS", "网络与光"),
    ("INFRASTRUCTURE", "基础设施"),
    ("AI_DEMAND", "应用与需求端"),
    ("MACRO_POLICY", "宏观政策"),
]
SECTOR_LABELS = dict(SECTOR_ORDER)


def _richness(item: GlobalAiAnalysisContract) -> tuple:
    """How informative an item is, for picking the survivor of a duplicate event."""
    return (
        item.importance_score,
        len(item.why_it_matters),
        sum(len(entry.companies) for entry in item.chain_mapping),
        len(item.transmission_path),
    )


def select_global_digest(
    analyses: list[GlobalAiAnalysisContract],
    *,
    cluster_by_url: dict[str, str] | None = None,
) -> list[dict]:
    eligible = [
        item for item in analyses
        if item.importance_score >= IMPORTANCE_THRESHOLD
        and item.importance_criterion != "NONE"
        and item.novelty != "REPEATED_STATEMENT"
        and item.evidence
    ]
    # Same event reported by several sources: keep the most informative write-up
    # and carry the other outlets as supplementary sources, so one event never
    # consumes two slots. Discovery-stage clusters are merged with CJK-aware
    # similarity over the model's own headlines, because the shared clustering
    # cannot separate reworded Chinese titles.
    cluster_by_url = cluster_by_url or {}
    headline_clusters = cluster_texts(
        {str(item.evidence[0].source_url): f"{item.headline} {item.what_happened}" for item in eligible}
    )
    survivors: dict[str, GlobalAiAnalysisContract] = {}
    extra_sources: dict[str, list[str]] = {}
    seen_keys: dict[str, str] = {}
    for item in eligible:
        url = str(item.evidence[0].source_url)
        # Either signal merging two items is enough; the first key wins so both
        # signals converge on one bucket.
        candidate_keys = [cluster_by_url.get(url), headline_clusters.get(url)]
        key = next((seen_keys[k] for k in candidate_keys if k and k in seen_keys), None)
        if key is None:
            key = next((k for k in candidate_keys if k), f"url:{url}")
        for signal in candidate_keys:
            if signal:
                seen_keys[signal] = key
        current = survivors.get(key)
        if current is None:
            survivors[key] = item
            extra_sources.setdefault(key, [])
            continue
        loser_url = url if _richness(item) <= _richness(current) else str(current.evidence[0].source_url)
        if _richness(item) > _richness(current):
            survivors[key] = item
        extra_sources[key].append(loser_url)

    ordered = sorted(survivors.items(), key=lambda pair: (-pair[1].importance_score, pair[1].headline))
    selected: list[dict] = []
    sector_counts: dict[str, int] = {}
    for key, item in ordered:
        if len(selected) >= TOTAL_LIMIT or sector_counts.get(item.sector, 0) >= SECTOR_CAP:
            continue
        sector_counts[item.sector] = sector_counts.get(item.sector, 0) + 1
        selected.append({
            "other_sources": extra_sources.get(key, []),
            "sector": item.sector,
            "importance_criterion": item.importance_criterion,
            "headline": item.headline,
            "what_happened": item.what_happened,
            "analysis": item.why_it_matters,
            "chain_mapping": [entry.model_dump(mode="json") for entry in item.chain_mapping],
            "transmission_path": item.transmission_path,
            "url": str(item.evidence[0].source_url),
            "sourcing": item.sourcing,
            "original_source": item.original_source,
        })
    return selected


def _render_chain(item: dict) -> str:
    groups = [
        f"【{SECTOR_LABELS.get(entry['sector'], entry['sector'])}】"
        + ("、".join(entry["companies"]) if entry.get("companies") else "无明确对应上市公司")
        for entry in item.get("chain_mapping", [])
    ]
    parts = []
    if groups:
        parts.append(" | ".join(groups))
    if item.get("transmission_path"):
        parts.append(f"传导路径：{item['transmission_path']}")
    return f"📍 涉及：{'；'.join(parts)}" if parts else ""


def _render_item(item: dict) -> str:
    if item["sourcing"] == "FIRSTHAND_OFFICIAL":
        source_line = f"🔗 来源：{item['url']}"
    else:
        source_line = f"🔗 来源：{item['url']}（转述报道，原始出处：{item['original_source']}）"
    blocks = [
        f"【{item['headline']}】",
        item["what_happened"],
        f"💡 分析：{item['analysis']}",
    ]
    chain = _render_chain(item)
    if chain:
        blocks.append(chain)
    if item.get("other_sources"):
        source_line += "\n　　其他来源：" + "、".join(item["other_sources"])
    blocks.append(source_line)
    return "\n\n".join(blocks)


def render_global_digest(items: list[dict]) -> str:
    if not items:
        return "AI产业链每日重点\n\n今日无达到标准的产业链重点新闻"
    lines = ["AI产业链每日重点"]
    for sector, label in SECTOR_ORDER:
        rows = [item for item in items if item["sector"] == sector]
        if not rows:  # empty sectors are hidden, never padded
            continue
        lines.append(f"\n═══ 【{label}】 ═══")
        lines.append("\n\n───\n\n".join(_render_item(item) for item in rows))
    return "\n\n".join(lines)
