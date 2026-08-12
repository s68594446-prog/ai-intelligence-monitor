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
from urllib.parse import urlsplit

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


@dataclass(frozen=True)
class EmailDigestItem:
    headline: str
    what_happened: str
    analysis: str
    companies_and_path: str
    source_url: str
    source_note: str
    other_sources: tuple[str, ...]


@dataclass(frozen=True)
class EmailDigestSection:
    label: str
    items: tuple[EmailDigestItem, ...]


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


FROM_DISPLAY_NAME = "AI Supply Chain Daily"


def build_subject(day: str) -> str:
    return f"AI Supply Chain Daily｜{day}"


SECTION_RE = re.compile(r"^═══\s*【(.+?)】\s*═══$")
HEADLINE_RE = re.compile(r"^【(.+?)】$")
SOURCE_RE = re.compile(r"^(https?://[^\s（]+)(.*)$")
EMAIL_SECTOR_LABELS = {"封装·设备·材料": "封装、设备与材料"}


def _parse_item(lines: list[str]) -> EmailDigestItem | None:
    values = [line.strip() for line in lines if line.strip()]
    if not values:
        return None
    headline_match = HEADLINE_RE.fullmatch(values[0])
    if not headline_match:
        return None

    what_happened: list[str] = []
    analysis: list[str] = []
    companies_and_path: list[str] = []
    source_url = ""
    source_note = ""
    other_sources: list[str] = []
    field = "what"
    for value in values[1:]:
        if value.startswith("💡 分析："):
            field = "analysis"
            analysis.append(value.removeprefix("💡 分析：").strip())
        elif value.startswith("📍 涉及："):
            field = "chain"
            companies_and_path.append(value.removeprefix("📍 涉及：").strip())
        elif value.startswith("🔗 来源："):
            field = "source"
            source = value.removeprefix("🔗 来源：").strip()
            match = SOURCE_RE.match(source)
            if match:
                source_url, source_note = match.group(1), match.group(2).strip()
            else:
                source_note = source
        elif value.startswith("其他来源：") or value.startswith("　　其他来源："):
            field = "other_sources"
            source_list = value.split("：", 1)[1]
            other_sources.extend(
                part.strip() for part in source_list.split("、") if part.strip()
            )
        elif field == "what":
            what_happened.append(value)
        elif field == "analysis":
            analysis.append(value)
        elif field == "chain":
            companies_and_path.append(value)

    return EmailDigestItem(
        headline=headline_match.group(1),
        what_happened=" ".join(what_happened),
        analysis=" ".join(analysis),
        companies_and_path=" ".join(companies_and_path),
        source_url=source_url,
        source_note=source_note,
        other_sources=tuple(other_sources),
    )


def parse_digest_sections(body: str) -> tuple[EmailDigestSection, ...]:
    """Parse the deterministic reader-facing digest format for HTML only."""
    sections: list[EmailDigestSection] = []
    label: str | None = None
    item_lines: list[str] = []
    items: list[EmailDigestItem] = []

    def flush_item() -> None:
        nonlocal item_lines
        item = _parse_item(item_lines)
        if item is not None:
            items.append(item)
        item_lines = []

    def flush_section() -> None:
        nonlocal items
        flush_item()
        if label is not None and items:
            sections.append(
                EmailDigestSection(
                    label=EMAIL_SECTOR_LABELS.get(label, label),
                    items=tuple(items),
                )
            )
        items = []

    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        section = SECTION_RE.fullmatch(stripped)
        if section:
            flush_section()
            label = section.group(1)
            continue
        if stripped == "───":
            flush_item()
            continue
        if label is not None:
            item_lines.append(raw_line)
    flush_section()
    return tuple(sections)


def _safe_link(url: str, *, label: str | None = None) -> str:
    parsed = urlsplit(url)
    escaped = html.escape(label or url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return escaped
    return (
        f'<a href="{html.escape(url, quote=True)}" '
        'style="color:#1769aa;text-decoration:underline;word-break:break-all">'
        f"{escaped}</a>"
    )


def _detail_row(label: str, value: str) -> str:
    return (
        '<tr><td style="padding:0 22px 16px 22px">'
        f'<div style="font-size:11px;line-height:16px;letter-spacing:.08em;'
        f'color:#64748b;font-weight:700;margin:0 0 5px 0">{html.escape(label)}</div>'
        f'<div style="font-size:14px;line-height:23px;color:#25364a;margin:0">'
        f'{html.escape(value or "未明确列出")}</div></td></tr>'
    )


def _render_card(item: EmailDigestItem) -> str:
    source = _safe_link(item.source_url) if item.source_url else html.escape(
        item.source_note or "未提供"
    )
    if item.source_note and item.source_url:
        source += (
            '<div style="font-size:12px;line-height:19px;color:#64748b;margin-top:4px">'
            f'{html.escape(item.source_note)}</div>'
        )
    if item.other_sources:
        supplemental = "<br>".join(
            _safe_link(url, label=f"补充来源 {index}")
            for index, url in enumerate(item.other_sources, 1)
        )
    else:
        supplemental = '<span style="color:#94a3b8">无</span>'

    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;border:1px solid #dbe4ee;border-collapse:separate;'
        'border-spacing:0;background-color:#ffffff;border-radius:8px;margin:0 0 14px 0">'
        '<tr><td style="padding:19px 22px 15px 22px;border-left:4px solid #2774a8">'
        f'<div style="font-size:17px;line-height:25px;color:#102a43;font-weight:700">'
        f'{html.escape(item.headline)}</div></td></tr>'
        + _detail_row("发生了什么", item.what_happened)
        + _detail_row("产业链解读", item.analysis)
        + _detail_row("涉及公司与传导路径", item.companies_and_path)
        + '<tr><td style="padding:0 22px 20px 22px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;background-color:#f6f9fc;border-collapse:collapse">'
        '<tr><td style="width:50%;vertical-align:top;padding:12px 14px;border-right:1px solid #dbe4ee">'
        '<div style="font-size:11px;line-height:16px;letter-spacing:.08em;color:#64748b;'
        'font-weight:700;margin-bottom:5px">原始来源</div>'
        f'<div style="font-size:12px;line-height:19px">{source}</div></td>'
        '<td style="width:50%;vertical-align:top;padding:12px 14px">'
        '<div style="font-size:11px;line-height:16px;letter-spacing:.08em;color:#64748b;'
        'font-weight:700;margin-bottom:5px">补充来源</div>'
        f'<div style="font-size:12px;line-height:19px">{supplemental}</div></td></tr>'
        '</table></td></tr></table>'
    )


def render_bodies(day: str, body: str) -> tuple[str, str]:
    """Plain-text and self-contained HTML bodies. No images, scripts, or tracking."""
    text_body = body if body.strip() else f"{build_subject(day)}\n\n今日无达到标准的产业链重点新闻"
    sections = parse_digest_sections(body)
    item_count = sum(len(section.items) for section in sections)
    section_count = len(sections)
    section_html = []
    for section in sections:
        cards = "".join(_render_card(item) for item in section.items)
        section_html.append(
            '<tr><td style="padding:8px 24px 2px 24px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'border="0" style="width:100%;border-collapse:collapse">'
            '<tr><td style="padding:10px 0 12px 0;border-bottom:2px solid #c8d8e8">'
            f'<span style="font-size:18px;line-height:26px;color:#153e5c;font-weight:700">'
            f'{html.escape(section.label)}</span>'
            f'<span style="font-size:12px;color:#64748b;margin-left:8px">'
            f'{len(section.items)} 条</span></td></tr>'
            f'<tr><td style="padding:14px 0 4px 0">{cards}</td></tr>'
            '</table></td></tr>'
        )
    if not section_html:
        section_html.append(
            '<tr><td style="padding:34px 28px;text-align:center;font-size:15px;'
            'line-height:24px;color:#52667a">今日无达到标准的产业链重点新闻</td></tr>'
        )
    html_body = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background-color:#eef3f8;'
        'font-family:Arial,Helvetica,sans-serif;color:#25364a">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;background-color:#eef3f8;border-collapse:collapse">'
        '<tr><td align="center" style="padding:22px 10px">'
        '<table role="presentation" width="660" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;max-width:660px;background-color:#ffffff;'
        'border-collapse:collapse;border-radius:10px;overflow:hidden">'
        '<tr><td style="padding:30px 28px 24px 28px;background-color:#0d2942;'
        'border-top:5px solid #36a3c8">'
        '<div style="font-size:12px;line-height:18px;letter-spacing:.14em;'
        'color:#8ed4eb;font-weight:700;margin-bottom:8px">AI SUPPLY CHAIN INTELLIGENCE</div>'
        '<div style="font-size:28px;line-height:36px;color:#ffffff;font-weight:700">'
        'AI 产业链日报</div>'
        f'<div style="font-size:14px;line-height:22px;color:#c7d7e6;margin-top:6px">'
        f'{html.escape(day)}</div></td></tr>'
        '<tr><td style="padding:18px 24px 10px 24px;background-color:#f8fafc">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;border-collapse:collapse">'
        '<tr><td style="width:50%;padding:10px 12px;border-right:1px solid #dbe4ee">'
        '<div style="font-size:11px;color:#64748b;letter-spacing:.08em">当日新闻</div>'
        f'<div style="font-size:23px;line-height:30px;color:#153e5c;font-weight:700">'
        f'{item_count} <span style="font-size:13px;font-weight:400">条</span></div></td>'
        '<td style="width:50%;padding:10px 12px">'
        '<div style="font-size:11px;color:#64748b;letter-spacing:.08em">覆盖产业链环节</div>'
        f'<div style="font-size:23px;line-height:30px;color:#153e5c;font-weight:700">'
        f'{section_count} <span style="font-size:13px;font-weight:400">个</span></div></td>'
        '</tr></table></td></tr>'
        + "".join(section_html)
        + '<tr><td style="padding:22px 28px;background-color:#f2f6fa;border-top:1px solid #dbe4ee;'
        'font-size:12px;line-height:20px;color:#64748b;text-align:center">'
        '内容仅用于产业研究与信息整理，不构成任何投资建议。'
        '</td></tr></table></td></tr></table></body></html>'
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
        from_display_name=FROM_DISPLAY_NAME,
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
    """Gate + (optional) send for the most recent prepared digest file."""
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

    tracking = {
        "message_id": request.message_id,
        "sender_domain": request.from_address.rsplit("@", 1)[1],
    }
    ledger.append({"idempotency_key": key, "day": gate.day, "status": "PENDING", "recipient_count": len(request.to_addresses), **tracking})
    try:
        provider_result = SMTPEmailDeliveryAdapter(settings).send(request)
    except Exception as exc:  # provider errors are bounded codes without credentials
        code = getattr(exc, "code", type(exc).__name__)
        unknown = getattr(exc, "outcome_unknown", False)
        status = "UNKNOWN" if unknown else "FAILED"
        ledger.append({"idempotency_key": key, "day": gate.day, "status": status, "reason": code, "recipient_count": len(request.to_addresses), **tracking})
        return {"status": status, "day": gate.day, "idempotency_key": key, "reason": code, "recipient_count": len(request.to_addresses)}
    accepted_at = datetime.now(timezone.utc).isoformat()
    ledger.append({"idempotency_key": key, "day": gate.day, "status": "SENT", "recipient_count": len(request.to_addresses), "smtp_accepted_at": accepted_at, "provider_message_id": provider_result.provider_message_id, **tracking})
    return {"status": "SENT", "day": gate.day, "idempotency_key": key, "recipient_count": len(request.to_addresses), "message_id": request.message_id, "smtp_accepted_at": accepted_at}
