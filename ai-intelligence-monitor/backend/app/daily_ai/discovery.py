import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


OFFICIAL_DOMAINS = {
    "www.sse.com.cn", "static.sse.com.cn", "www.szse.cn", "disc.static.szse.cn",
    "www.bse.cn", "www.cninfo.com.cn", "static.cninfo.com.cn", "www.csrc.gov.cn",
}


def in_window(published_at: datetime, *, now: datetime | None = None, hours: int = 48) -> bool:
    now = now or datetime.now(timezone.utc)
    value = published_at.astimezone(timezone.utc)
    return now - timedelta(hours=hours) <= value <= now


def official_source(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in OFFICIAL_DOMAINS and not parsed.username and not parsed.password


def announcement_key(*, security_code: str, title: str, announced_at: str) -> str:
    normalized = "".join(title.split()).replace("股份有限公司", "")
    return hashlib.sha256(f"{security_code}|{normalized}|{announced_at[:10]}".encode()).hexdigest()


def deduplicate_announcements(candidates: list[dict]) -> list[dict]:
    priority = {"SSE": 0, "SZSE": 0, "BSE": 0, "CNINFO": 1, "COMPANY_IR": 2}
    selected: dict[str, dict] = {}
    for item in candidates:
        key = announcement_key(security_code=item["security_code"], title=item["title"], announced_at=item["announced_at"])
        current = selected.get(key)
        if current is None or priority.get(item.get("source"), 9) < priority.get(current.get("source"), 9):
            selected[key] = item
    return [selected[key] for key in sorted(selected)]


def keyword_prefilter(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)
