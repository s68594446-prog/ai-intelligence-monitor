from app.daily_ai.contracts import AShareAnalysisContract


def select_a_share_digest(analyses: list[AShareAnalysisContract], *, china_existing_count: int = 0) -> list[dict]:
    limit = min(4, max(0, 6 - china_existing_count), 12)
    selected = [item for item in analyses if item.importance_score >= 60 and not item.concept_only and item.ai_relevance in {"DIRECT", "INDIRECT"}]
    selected.sort(key=lambda item: (-item.importance_score, item.security_code, item.announced_at))
    return [{"company_name": item.company_name, "security_code": item.security_code, "announcement_type": item.announcement_type, "what_happened": item.what_happened, "url": str(item.evidence[0].source_url), "source_tier": "T1"} for item in selected[:limit] if item.evidence]


def render_a_share_section(items: list[dict]) -> str:
    if not items:
        return "A股AI重要公告\n今日无达到标准的A股AI重大公告"
    lines = ["A股AI重要公告"]
    lines.extend(f"- {item['company_name']}（{item['security_code']}）：{item['what_happened']}\n  官方原文：{item['url']}" for item in items)
    return "\n".join(lines)
