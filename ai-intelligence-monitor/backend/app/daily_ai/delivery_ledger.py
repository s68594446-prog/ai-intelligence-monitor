"""Stdlib-only, fail-closed Phase 3C send ledger, shared by all entry points."""
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path


class LedgerError(ValueError):
    pass


def audit_hash(report: dict) -> str:
    return hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class EmailLedger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError
            entries = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
            for row in entries:
                if (not isinstance(row, dict) or row.get("status") not in {"PENDING", "UNKNOWN", "FAILED", "SENT"}
                        or not isinstance(row.get("idempotency_key"), str) or not row["idempotency_key"]
                        or date.fromisoformat(row["day"]).isoformat() != row["day"]):
                    raise ValueError
            return entries
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            raise LedgerError("DELIVERY_LEDGER_INVALID") from None

    def already_sent(self, key: str, day: str | None = None) -> bool:
        return any(row["status"] == "SENT" and (row["idempotency_key"] == key or row["day"] == day)
                   for row in self._entries())

    def unresolved_attempt(self, key: str, day: str) -> bool:
        latest = {}
        for row in self._entries():
            if row["idempotency_key"] == key or row["day"] == day:
                latest[row["idempotency_key"]] = row["status"]
        return any(status in {"PENDING", "UNKNOWN"} for status in latest.values())

    def day_state(self, day: str) -> str:
        if self.unresolved_attempt("", day):
            return "UNRESOLVED"
        return "SENT" if self.already_sent("", day) else "NOT_SENT"

    @contextmanager
    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize local callers as well as the workflows' shared concurrency.
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LedgerError("DELIVERY_IN_PROGRESS") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**entry, "recorded_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
