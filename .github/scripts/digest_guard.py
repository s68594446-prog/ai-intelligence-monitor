"""No-network generation preflight; the sender repeats this day-level gate."""
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-intelligence-monitor/backend"))
from app.daily_ai.delivery_ledger import EmailLedger, LedgerError


def main():
    day = date.fromisoformat(os.environ["DIGEST_DAY"]).isoformat()
    try:
        state = EmailLedger(Path("digests/.email-log.jsonl")).day_state(day)
    except LedgerError:
        state = "INVALID"
    print(json.dumps({"day": day, "delivery_state": state}))
    if state in {"UNRESOLVED", "INVALID"}:
        print("::error::Delivery ledger requires manual reconciliation; no regeneration or resend")
        return 1
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"skip={'true' if state == 'SENT' else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
