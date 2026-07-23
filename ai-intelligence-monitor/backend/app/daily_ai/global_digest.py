from app.daily_ai.global_contracts import GlobalAiAnalysisContract


IMPORTANCE_THRESHOLD = 60
GLOBAL_LIMIT = 6
CHINA_LIMIT = 6
TOTAL_LIMIT = 12


def select_global_digest(analyses: list[GlobalAiAnalysisContract]) -> list[dict]:
    eligible = [
        item for item in analyses
        if item.importance_score >= IMPORTANCE_THRESHOLD
        and item.novelty != "REPEATED_STATEMENT"
        and item.evidence
    ]
    eligible.sort(key=lambda item: (-item.importance_score, item.headline))
    selected: list[dict] = []
    counts = {"GLOBAL": 0, "CHINA": 0}
    limits = {"GLOBAL": GLOBAL_LIMIT, "CHINA": CHINA_LIMIT}
    for item in eligible:
        if len(selected) >= TOTAL_LIMIT or counts[item.region_section] >= limits[item.region_section]:
            continue
        counts[item.region_section] += 1
        selected.append({
            "section": item.region_section,
            "topic": item.topic,
            "headline": item.headline,
            "what_happened": item.what_happened,
            "url": str(item.evidence[0].source_url),
            "sourcing": item.sourcing,
            "original_source": item.original_source,
        })
    return selected


def _render_item(item: dict) -> str:
    if item["sourcing"] == "FIRSTHAND_OFFICIAL":
        return f"- {item['headline']}：{item['what_happened']}\n  官方原文：{item['url']}"
    return (
        f"- {item['headline']}：{item['what_happened']}\n"
        f"  转述报道：{item['url']}（原始出处：{item['original_source']}）"
    )


def render_global_digest(items: list[dict]) -> str:
    if not items:
        return "AI每日摘要\n今日无重大更新"
    lines = ["AI每日摘要"]
    for section, label in (("CHINA", "中国"), ("GLOBAL", "全球")):
        rows = [item for item in items if item["section"] == section]
        if not rows:
            continue
        lines.append(f"\n【{label}】")
        lines.extend(_render_item(item) for item in rows)
    return "\n".join(lines)
