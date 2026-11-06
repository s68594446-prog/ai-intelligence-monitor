"""Verify or replay sealed Phase 3C artifacts without external calls."""
import argparse
import json
from pathlib import Path

from app.daily_ai.replay_integrity import ReplayIntegrityError, replay_run, verify_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="New directory; omit for read-only verification")
    args = parser.parse_args()
    try:
        report = replay_run(args.run_dir, args.output) if args.output else verify_run(args.run_dir)
    except (ReplayIntegrityError, OSError) as error:
        code = str(error) if isinstance(error, ReplayIntegrityError) else "REPLAY_IO_FAILED"
        print(json.dumps({"integrity_passed": False, "error_code": code}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
