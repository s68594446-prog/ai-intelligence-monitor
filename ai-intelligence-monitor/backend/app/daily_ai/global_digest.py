from app.daily_ai.global_contracts import GlobalAiAnalysisContract


# 覆盖面要广，输出要精：threshold 70 means "genuinely material to the chain".
# A day with only a few (or zero) qualifying items is a legal, honest digest.
IMPORTANCE_THRESHOLD = 70
SECTOR_CAP = 3
TOTAL_LIMIT = 12

SECTOR_ORDER = [
    ("CHIPS_COMPUTE", "芯片与算力"),
    ("MEMORY_STORAGE", "存储"),
    ("PACKAGING_EQUIPMENT", "封装·设备·材料"),
    ("NETWORKING_OPTICS", "网络与光"),
    ("INFRASTRUCTURE", "基础设施"),
    ("AI_DEMAND", "应用与需求端"),
    ("MACRO_POLICY", "宏观政策"),
]


def select_global_digest(analyses: list[GlobalAiAnalysisContract]) -> list[dict]:
    eligible = [
        item for item in analyses
        if item.importance_score >= IMPORTANCE_THRESHOLD
        and item.importance_criterion != "NONE"
        and item.novelty != "REPEATED_STATEMENT"
        and item.evidence
    ]
    eligible.sort(key=lambda item: (-item.importance_score, item.headline))
    selected: list[dict] = []
    sector_counts: dict[str, int] = {}
    for item in eligible:
        if len(selected) >= TOTAL_LIMIT or sector_counts.get(item.sector, 0) >= SECTOR_CAP:
            continue
        sector_counts[item.sector] = sector_counts.get(item.sector, 0) + 1
        selected.append({
            "sector": item.sector,
            "importance_criterion": item.importance_criterion,
            "headline": item.headline,
            "what_happened": item.what_happened,
            "analysis": item.why_it_matters,
            "url": str(item.evidence[0].source_url),
            "sourcing": item.sourcing,
            "original_source": item.original_source,
        })
    return selected


def _render_item(item: dict) -> str:
    if item["sourcing"] == "FIRSTHAND_OFFICIAL":
        source_line = f"🔗 来源：{item['url']}"
    else:
        source_line = f"🔗 转述报道：{item['url']}（原始出处：{item['original_source']}）"
    return (
        f"【{item['headline']}】\n"
        f"{item['what_happened']}\n"
        f"💡 分析：{item['analysis']}\n"
        f"{source_line}"
    )


def render_global_digest(items: list[dict]) -> str:
    if not items:
        return "AI产业链每日重点\n今日无达到标准的产业链重点新闻"
    lines = ["AI产业链每日重点"]
    for sector, label in SECTOR_ORDER:
        rows = [item for item in items if item["sector"] == sector]
        if not rows:  # empty sectors are hidden, never padded
            continue
        lines.append(f"\n═══ 【{label}】 ═══\n")
        lines.append("\n\n".join(_render_item(item) for item in rows))
    return "\n".join(lines)
