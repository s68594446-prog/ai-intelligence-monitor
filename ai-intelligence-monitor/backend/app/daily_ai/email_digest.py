"""Controlled email delivery for the daily supply-chain digest.

Reuses the audited SMTPEmailDeliveryAdapter (STARTTLS on 587, envelope-only
recipients, header-injection guards). This path is separate from the Phase 3A
Signal-alert Gate, which is bound to a DRAM/HBM Analysis Run and does not apply
to the digest. Idempotency is enforced through a committed JSONL ledger because
GitHub Actions has no persistent database.

Discipline preserved: sends only a digest that passed Ops, never twice for the
same day+content+recipients, records PENDING -> SENT/FAILED, and adds no
tracking pixels, external images, or attachments.
"""
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.settings import Settings
from app.delivery.contracts import EmailDeliveryRequest
from app.delivery.email_adapter import (
    SMTPEmailDeliveryAdapter,
    resolve_smtp_configuration,
)

AUDIT_MARKER = "## 审计"
EMPTY_DIGEST_MARKER = "今日无达到标准的产业链重点新闻"
MAX_DIGEST_BYTES = 1_000_000


@dataclass(frozen=True)
class DigestGateResult:
    ok: bool
    reasons: list[str]
    day: str
    body: str
    ops_passed: bool


def split_digest_body(markdown: str) -> str:
    """Return the reader-facing digest, dropping the committed audit appendix."""
    head = markdown.split(AUDIT_MARKER, 1)[0]
    return head.rstrip().removesuffix("---").rstrip()


def latest_digest_path(digests_dir: Path) -> Path | None:
    files = sorted(p for p in digests_dir.glob("*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    return files[-1] if files else None


def _ops_passed(markdown: str) -> bool | None:
    match = re.search(r"### ops\.json\s*```json\s*(\{.*?\})\s*```", markdown, re.S)
    if not match:
        return None
    try:
        return bool(json.loads(match.group(1)).get("passed"))
    except ValueError:
        return None


def evaluate_digest(path: Path) -> DigestGateResult:
    reasons: list[str] = []
    if not path.is_file() or path.is_symlink():
        return DigestGateResult(False, ["DIGEST_NOT_A_REGULAR_FILE"], path.stem, "", False)
    raw = path.read_bytes()
    if len(raw) > MAX_DIGEST_BYTES:
        reasons.append("DIGEST_TOO_LARGE")
    markdown = raw.decode("utf-8", "strict")
    body = split_digest_body(markdown)
    ops = _ops_passed(markdown)
    if ops is None:
        reasons.append("DIGEST_OPS_RESULT_MISSING")
    elif not ops:
        reasons.append("DIGEST_OPS_NOT_PASSED")
    if not body.strip():
        reasons.append("DIGEST_BODY_EMPTY")
    return DigestGateResult(not reasons, reasons, path.stem, body, bool(ops))


def build_subject(day: str) -> str:
    return f"AI产业链每日重点｜{day}"


def render_bodies(day: str, body: str) -> tuple[str, str]:
    """Plain-text and self-contained HTML bodies. No images, scripts, or tracking."""
    text_body = body if body.strip() else f"{build_subject(day)}\n\n今日无达到标准的产业链重点新闻"
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("<br>")
        elif stripped.startswith("═══") or stripped.endswith("═══"):
            label = stripped.strip("═ ")
            lines.append(f'<h2 style="border-bottom:2px solid #888;padding-bottom:4px">{html.escape(label)}</h2>')
        elif stripped.startswith("【") and stripped.endswith("】"):
            lines.append(f'<h3 style="margin:18px 0 4px">{html.escape(stripped)}</h3>')
        elif stripped == "───":
            lines.append("<hr>")
        else:
            lines.append(f'<p style="margin:4px 0">{html.escape(stripped)}</p>')
    html_body = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:720px;margin:0 auto;padding:16px;color:#1a1a1a;line-height:1.6">'
        + "\n".join(lines)
        + "</body></html>"
    )
    return text_body, html_body


def recipient_fingerprint(recipients: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(recipients).encode("utf-8")).hexdigest()


def idempotency_key(day: str, body: str, recipients: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "channel": "DAILY_DIGEST_EMAIL",
            "day": day,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "recipient_fingerprint": recipient_fingerprint(recipients),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EmailLedger:
    """Append-only committed record of send attempts; the idempotency store."""

    def __init__(self, path: Path):
        self.path = path

    def _entries(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def already_sent(self, key: str) -> bool:
        return any(entry.get("idempotency_key") == key and entry.get("status") == "SENT" for entry in self._entries())

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**entry, "recorded_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")


def build_request(settings: Settings, day: str, body: str) -> tuple[EmailDeliveryRequest | None, list[str], str | None]:
    configuration, errors = resolve_smtp_configuration(settings)
    if configuration is None:
        return None, errors, None
    key = idempotency_key(day, body, configuration.recipients)
    text_body, html_body = render_bodies(day, body)
    request = EmailDeliveryRequest(
        run_id=f"digest-{day}",
        from_address=configuration.from_address,
        to_addresses=configuration.recipients,
        subject=build_subject(day),
        html_body=html_body,
        text_body=text_body,
        message_id=f"<aim-digest-{key[:32]}@{configuration.message_id_domain}>",
    )
    return request, [], key


def deliver_latest(
    settings: Settings,
    *,
    digests_dir: Path,
    ledger_path: Path,
    send: bool,
) -> dict:
    """Gate + (optional) send for the most recent committed digest."""
    path = latest_digest_path(digests_dir)
    if path is None:
        return {"status": "BLOCKED", "reasons": ["NO_DIGEST_FOUND"]}
    gate = evaluate_digest(path)
    if not settings.email_delivery_enabled:
        gate.reasons.append("EMAIL_DELIVERY_DISABLED")
    if gate.reasons:
        return {"status": "BLOCKED", "day": gate.day, "reasons": gate.reasons}

    request, config_errors, key = build_request(settings, gate.day, gate.body)
    if request is None:
        return {"status": "BLOCKED", "day": gate.day, "reasons": config_errors}
    ledger = EmailLedger(ledger_path)
    if ledger.already_sent(key):
        return {"status": "ALREADY_SENT", "day": gate.day, "idempotency_key": key, "recipient_count": len(request.to_addresses)}
    if not send:
        return {"status": "READY", "day": gate.day, "idempotency_key": key, "recipient_count": len(request.to_addresses), "dry_run": True}

    ledger.append({"idempotency_key": key, "day": gate.day, "status": "PENDING", "recipient_count": len(request.to_addresses)})
    try:
        SMTPEmailDeliveryAdapter(settings).send(request)
    except Exception as exc:  # provider errors are bounded codes without credentials
        code = getattr(exc, "code", type(exc).__name__)
        unknown = getattr(exc, "outcome_unknown", False)
        status = "UNKNOWN" if unknown else "FAILED"
        ledger.append({"idempotency_key": key, "day": gate.day, "status": status, "reason": code, "recipient_count": len(request.to_addresses)})
        return {"status": status, "day": gate.day, "idempotency_key": key, "reason": code, "recipient_count": len(request.to_addresses)}
    ledger.append({"idempotency_key": key, "day": gate.day, "status": "SENT", "recipient_count": len(request.to_addresses)})
    return {"status": "SENT", "day": gate.day, "idempotency_key": key, "recipient_count": len(request.to_addresses)}
