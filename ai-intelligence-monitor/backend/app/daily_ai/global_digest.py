from app.daily_ai.global_contracts import GlobalAiAnalysisContract


IMPORTANCE_THRESHOLD = 60
GLOBAL_LIMIT = 8
CHINA_LIMIT = 6
TOTAL_LIMIT = 14

# News first, papers last. RESEARCH_PAPER additionally carries a hard quota:
# at most one paper per digest, and only when genuinely important.
CATEGORY_PRIORITY = {
    "COMPANY_RELEASE": 0,
    "BUSINESS_FUNDING_MA": 1,
    "POLICY_REGULATION": 2,
    "TECH_BREAKTHROUGH": 3,
    "RESEARCH_PAPER": 4,
    "OTHER": 5,
}
CATEGORY_QUOTA = {"RESEARCH_PAPER": 1}


def select_global_digest(analyses: list[GlobalAiAnalysisContract]) -> list[dict]:
    eligible = [
        item for item in analyses
        if item.importance_score >= IMPORTANCE_THRESHOLD
        and item.novelty != "REPEATED_STATEMENT"
        and item.evidence
    ]
    eligible.sort(key=lambda item: (CATEGORY_PRIORITY.get(item.news_category, 9), -item.importance_score, item.headline))
    selected: list[dict] = []
    counts = {"GLOBAL": 0, "CHINA": 0}
    limits = {"GLOBAL": GLOBAL_LIMIT, "CHINA": CHINA_LIMIT}
    category_counts: dict[str, int] = {}
    for item in eligible:
        if len(selected) >= TOTAL_LIMIT or counts[item.region_section] >= limits[item.region_section]:
            continue
        quota = CATEGORY_QUOTA.get(item.news_category)
        if quota is not None and category_counts.get(item.news_category, 0) >= quota:
            continue
        counts[item.region_section] += 1
        category_counts[item.news_category] = category_counts.get(item.news_category, 0) + 1
        selected.append({
            "section": item.region_section,
            "topic": item.topic,
            "news_category": item.news_category,
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
        return "AI每日摘要\n今日无重大更新"
    lines = ["AI每日摘要"]
    for section, label in (("GLOBAL", "全球"), ("CHINA", "中国")):
        rows = [item for item in items if item["section"] == section]
        if not rows:
            continue
        lines.append(f"\n═══ 【{label}】 ═══\n")
        lines.append("\n\n".join(_render_item(item) for item in rows))
    return "\n".join(lines)
