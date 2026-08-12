"""Persist the published Phase 3C URL/content-hash history after Ops passes."""

import argparse
import json
from pathlib import Path

from app.daily_ai.source_history import append_source_history


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    args = parser.parse_args()

    records = json.loads(args.artifact.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit("published source history artifact must be a list")
    appended = append_source_history(args.history, records)
    print(json.dumps({"status": "UPDATED", "appended": appended}, sort_keys=True))


if __name__ == "__main__":
    main()
