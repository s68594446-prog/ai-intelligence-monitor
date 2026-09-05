import argparse
import json
from pathlib import Path

from app.core.settings import Settings
from app.daily_ai.email_digest import deliver_latest


PROJECT_DIR = Path(__file__).resolve().parents[3]
DIGESTS_DIR = PROJECT_DIR.parent / "digests"
LEDGER_PATH = DIGESTS_DIR / ".email-log.jsonl"


def main():
    parser = argparse.ArgumentParser(description="Email the latest prepared daily digest; default is a Gate dry-run with no SMTP")
    parser.add_argument("--send", action="store_true", help="actually connect to SMTP and send (otherwise Gate check only)")
    parser.add_argument("--digests-dir", type=Path, default=DIGESTS_DIR)
    parser.add_argument("--day", help="exact YYYY-MM-DD digest; missing file blocks instead of choosing another day")
    args = parser.parse_args()

    result = deliver_latest(
        Settings(),
        digests_dir=args.digests_dir,
        ledger_path=args.digests_dir / ".email-log.jsonl",
        send=args.send,
        day=args.day,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # Exit codes: 0 = READY/SENT/ALREADY_SENT, 2 = Gate blocked, 1 = failed/unknown
    if result["status"] in {"READY", "SENT", "ALREADY_SENT"}:
        raise SystemExit(0)
    if result["status"] == "BLOCKED":
        raise SystemExit(2)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
