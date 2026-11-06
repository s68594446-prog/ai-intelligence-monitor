"""Bounded GitHub API reads and a sanitized health summary; never retries sends."""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-intelligence-monitor/backend"))
from app.daily_ai.delivery_ledger import EmailLedger, LedgerError
from app.daily_ai.watchdog import diagnose, LOCAL


def api(path):
    request = Request("https://api.github.com/" + path, headers={
        "Accept": "application/vnd.github+json", "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "User-Agent": "ai-monitor-read-only-watchdog"})
    with urlopen(request, timeout=20) as response:
        raw = response.read(5_000_001)
    if len(raw) > 5_000_000:
        raise ValueError("response limit")
    return json.loads(raw)


def main():
    now = datetime.now(timezone.utc)
    day = now.astimezone(LOCAL).date().isoformat()
    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError("repository")
        # At most three API calls; no arbitrary URL or response-body logging.
        runs = api(f"repos/{repo}/actions/workflows/daily-digest.yml/runs?branch=main&per_page=100")["workflow_runs"]
        runs += api(f"repos/{repo}/actions/workflows/email-digest.yml/runs?branch=main&per_page=100")["workflow_runs"]
        today = [r for r in runs if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(LOCAL).date().isoformat() == day]
        latest = max(today, key=lambda r: r["created_at"]) if today else None
        jobs = api(f"repos/{repo}/actions/runs/{int(latest['id'])}/jobs?per_page=100")["jobs"] if latest else []
        if len(jobs) >= 100:
            raise ValueError("incomplete job observation")
        ledger = EmailLedger(Path("digests/.email-log.jsonl"))
        try:
            state = ledger.day_state(day)
        except LedgerError:
            state = "INVALID"
        archive = Path("digests") / f"{day}.md"
        matches = True
        if state == "SENT" and archive.is_file():
            body = archive.read_text().split("## 审计", 1)[0].rstrip().removesuffix("---").rstrip()
            recorded = {r.get("body_sha256") for r in ledger._entries() if r["day"] == day and r["status"] == "SENT"}
            matches = None in recorded or hashlib.sha256(body.encode()).hexdigest() in recorded
        report = diagnose(now=now, runs=runs, jobs=jobs, ledger_state=state,
                          archive_exists=archive.is_file(), archive_matches=matches)
    except Exception:
        report = {"day": day, "status": "OBSERVATION_FAILED", "passed": False,
                  "inbox_delivery_verified": False, "model_calls": 0, "smtp_calls": 0}
    Path("watchdog-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(f"Daily digest {day}: **{report['status']}**\n\nRead-only inspection; SMTP acceptance is not inbox delivery. No automatic resend.\n")
    if not report["passed"]:
        print("::error::Daily digest watchdog: " + report["status"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
