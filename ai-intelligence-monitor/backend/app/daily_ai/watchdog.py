"""Read-only diagnosis: no model, SMTP, automatic repair, or inbox assertion."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo("Asia/Shanghai")


def diagnose(*, now: datetime, runs: list[dict], jobs: list[dict], ledger_state: str,
             archive_exists: bool, archive_matches: bool = True) -> dict:
    day = now.astimezone(LOCAL).date()
    def result(status, passed=False):
        return {"day": day.isoformat(), "status": status, "passed": passed,
                "inbox_delivery_verified": False, "model_calls": 0, "smtp_calls": 0}
    if ledger_state in {"INVALID", "UNRESOLVED"}:
        return result("LEDGER_INVALID" if ledger_state == "INVALID" else "DELIVERY_UNCERTAIN")
    if ledger_state == "SENT":
        return result("SMTP_ACCEPTED_ARCHIVED", True) if archive_exists and archive_matches else result("ARCHIVE_FAILED")
    today_runs = [r for r in runs if r.get("head_branch") == "main"
                  and datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(LOCAL).date() == day]
    if not today_runs:
        due = datetime.combine(day, time(9, 30), LOCAL)
        return result("NOT_STARTED" if now >= due else "WAITING_FOR_SCHEDULE", now < due)
    run = max(today_runs, key=lambda r: r["created_at"])
    if run["status"] != "completed":
        start = datetime.fromisoformat(run.get("run_started_at", run["created_at"]).replace("Z", "+00:00"))
        late = now - start > timedelta(minutes=60)
        return result("RUN_STALLED" if late else "RUNNING", not late)
    steps = [step for job in jobs for step in job.get("steps", []) if step.get("conclusion") in {"failure", "cancelled", "timed_out"}]
    names = " ".join(step["name"] for step in steps)
    if "Persist accepted delivery ledger" in names:
        return result("DELIVERY_UNCERTAIN")
    if "Commit delivered digest" in names or "Commit send ledger" in names:
        return result("ARCHIVE_FAILED")
    if "Email the digest" in names or "Send (or Gate-check)" in names:
        return result("DELIVERY_FAILED")
    if "Ops Gate" in names:
        return result("REVIEW_BLOCKED")
    if "SMTP configuration" in names:
        return result("DELIVERY_CONFIGURATION_FAILED")
    if run.get("conclusion") != "success":
        return result("GENERATION_FAILED")
    return result("DELIVERY_UNCONFIRMED")
