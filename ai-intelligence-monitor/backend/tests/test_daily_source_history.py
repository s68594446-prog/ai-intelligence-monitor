import json
from datetime import date

from app.daily_ai.source_history import (
    append_source_history,
    load_recent_source_history,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
URL = "https://example.com/news?a=1&utm_source=test"


def test_recent_history_skips_only_same_url_and_same_content(tmp_path):
    path = tmp_path / ".source-history.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "day": "2026-08-11",
                        "canonical_url": URL,
                        "content_sha256": HASH_A,
                    }
                ),
                json.dumps(
                    {
                        "day": "2026-08-08",
                        "canonical_url": URL,
                        "content_sha256": HASH_B,
                    }
                ),
                "not-json",
            ]
        ),
        encoding="utf-8",
    )

    history = load_recent_source_history(path, current_day=date(2026, 8, 12))

    assert history.has_same_content("https://example.com/news?a=1", HASH_A)
    assert not history.has_same_content("https://example.com/news?a=1", HASH_B)
    assert not history.has_same_content("https://example.com/news?a=1", "c" * 64)
    assert history.records_loaded == 1
    assert history.malformed_lines == 1


def test_source_history_append_is_idempotent_and_contains_no_raw_text(tmp_path):
    path = tmp_path / ".source-history.jsonl"
    records = [
        {
            "day": "2026-08-12",
            "canonical_url": URL,
            "content_sha256": HASH_A,
        }
    ]

    assert append_source_history(path, records) == 1
    assert append_source_history(path, records) == 0

    stored = path.read_text(encoding="utf-8")
    assert stored.count("\n") == 1
    assert "utm_source" not in stored
    assert "raw_text" not in stored
