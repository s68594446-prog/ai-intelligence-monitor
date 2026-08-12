"""Cross-day source history for the isolated Phase 3C daily digest.

Only canonical URLs and content hashes are persisted. Raw article text, model
output, credentials, and recipient data never enter this ledger.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from app.web_discovery.security import WebSecurityError, canonicalize_url


HISTORY_WINDOW_DAYS = 3
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SourceHistoryIndex:
    hashes_by_url: dict[str, frozenset[str]]
    records_loaded: int
    malformed_lines: int

    def has_same_content(self, url: str, content_sha256: str | None) -> bool:
        if not content_sha256 or not SHA256_RE.fullmatch(content_sha256):
            return False
        try:
            canonical = canonicalize_url(url)
        except WebSecurityError:
            return False
        return content_sha256 in self.hashes_by_url.get(canonical, frozenset())


def load_recent_source_history(
    path: Path,
    *,
    current_day: date,
    window_days: int = HISTORY_WINDOW_DAYS,
) -> SourceHistoryIndex:
    """Load same-content records from today and the preceding bounded window.

    Malformed records fail open for freshness: they are counted for audit but
    never cause a source to be skipped.
    """

    if window_days < 1:
        raise ValueError("window_days must be positive")
    if not path.is_file():
        return SourceHistoryIndex({}, 0, 0)

    hashes: dict[str, set[str]] = {}
    loaded = malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            entry_day = date.fromisoformat(entry["day"])
            delta = (current_day - entry_day).days
            content_sha256 = entry["content_sha256"]
            canonical = canonicalize_url(entry["canonical_url"])
            if not isinstance(content_sha256, str) or not SHA256_RE.fullmatch(
                content_sha256
            ):
                raise ValueError("invalid content hash")
        except (KeyError, TypeError, ValueError, WebSecurityError):
            malformed += 1
            continue
        if not 0 <= delta <= window_days:
            continue
        hashes.setdefault(canonical, set()).add(content_sha256)
        loaded += 1
    return SourceHistoryIndex(
        {url: frozenset(values) for url, values in hashes.items()},
        loaded,
        malformed,
    )


def append_source_history(path: Path, records: list[dict]) -> int:
    """Append new valid records idempotently and return the appended count."""

    existing: set[tuple[str, str, str]] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                existing.add(
                    (
                        str(entry["day"]),
                        canonicalize_url(entry["canonical_url"]),
                        str(entry["content_sha256"]),
                    )
                )
            except (KeyError, TypeError, ValueError, WebSecurityError):
                continue

    prepared: list[dict] = []
    for entry in records:
        day = date.fromisoformat(str(entry["day"])).isoformat()
        canonical = canonicalize_url(str(entry["canonical_url"]))
        content_sha256 = str(entry["content_sha256"])
        if not SHA256_RE.fullmatch(content_sha256):
            raise ValueError("invalid content hash")
        key = (day, canonical, content_sha256)
        if key in existing:
            continue
        existing.add(key)
        prepared.append(
            {
                "day": day,
                "canonical_url": canonical,
                "content_sha256": content_sha256,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    if prepared:
        with path.open("a", encoding="utf-8") as handle:
            for entry in prepared:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return len(prepared)
