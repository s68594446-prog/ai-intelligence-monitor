"""Read-only, zero-network replay of a saved Phase 3C final draft."""
import argparse
import json
from datetime import date
from pathlib import Path

from app.daily_ai.final_review import review_rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    bundle = json.loads((args.run_dir / "source-bundle.json").read_text())
    report = review_rendered(
        day=args.day.isoformat(), body=(args.run_dir / "04-digest.txt").read_text().strip(),
        raw_by_url={s["url"]: s["raw_text"] for s in bundle["sources"]},
    )
    print(json.dumps({"passed": report["passed"], "network_calls": 0, "llm_calls": 0,
                      "claims": len(report["claims"]), "findings": report["findings"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
