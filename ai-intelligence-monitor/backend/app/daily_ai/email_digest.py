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
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.core.settings import Settings
from app.daily_ai.ops_report import report_passed
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
    risks_and_uncertainties: str
    source_url: str
    source_note: str
    other_sources: tuple[str, ...]
    importance_score: int


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
    appendix = markdown.partition(AUDIT_MARKER)[2]
    matches = re.findall(r"### ops\.json\s*```json\s*(.*?)\s*```", appendix, re.S)
    if len(matches) != 1:
        return None
    try:
        return report_passed(json.loads(matches[0]))
    except ValueError:
        return None


def evaluate_digest(path: Path) -> DigestGateResult:
    reasons: list[str] = []
    if not path.is_file() or path.is_symlink():
        return DigestGateResult(False, ["DIGEST_NOT_A_REGULAR_FILE"], path.stem, "", False)
    try:
        if date.fromisoformat(path.stem).isoformat() != path.stem:
            raise ValueError
    except ValueError:
        return DigestGateResult(False, ["DIGEST_DATE_INVALID"], path.stem, "", False)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_DIGEST_BYTES + 1)
    except OSError:
        return DigestGateResult(False, ["DIGEST_READ_FAILED"], path.stem, "", False)
    if len(raw) > MAX_DIGEST_BYTES:
        return DigestGateResult(False, ["DIGEST_TOO_LARGE"], path.stem, "", False)
    try:
        markdown = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return DigestGateResult(False, ["DIGEST_ENCODING_INVALID"], path.stem, "", False)
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
IMPORTANCE_RE = re.compile(r"^重要程度：(\d{1,3})/100$")
METADATA_RE = re.compile(r"^分析元数据：(.+?)\s*·\s*(Prompt\s+v\d+)$", re.I | re.M)
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
    risks_and_uncertainties: list[str] = []
    source_url = ""
    source_note = ""
    other_sources: list[str] = []
    importance_score = -1
    field = "what"
    for value in values[1:]:
        importance = IMPORTANCE_RE.fullmatch(value)
        if importance:
            importance_score = min(100, max(0, int(importance.group(1))))
        elif value.startswith("💡 分析："):
            field = "analysis"
            analysis.append(value.removeprefix("💡 分析：").strip())
        elif value.startswith("📍 涉及："):
            field = "chain"
            companies_and_path.append(value.removeprefix("📍 涉及：").strip())
        elif value.startswith("⚠️ 风险与不确定性："):
            field = "risks"
            risks_and_uncertainties.append(
                value.removeprefix("⚠️ 风险与不确定性：").strip()
            )
        elif value.startswith("🔗 来源："):
            field = "source"
            source = value.removeprefix("🔗 来源：").strip()
            match = SOURCE_RE.match(source)
            if match:
                source_url, source_note = match.group(1), match.group(2).strip()
            else:
                source_note = "无效来源地址"
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
        elif field == "risks":
            risks_and_uncertainties.append(value)

    return EmailDigestItem(
        headline=headline_match.group(1),
        what_happened=" ".join(what_happened),
        analysis=" ".join(analysis),
        companies_and_path=" ".join(companies_and_path),
        risks_and_uncertainties=" ".join(risks_and_uncertainties),
        source_url=source_url,
        source_note=source_note,
        other_sources=tuple(other_sources),
        importance_score=importance_score,
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
        'style="color:#8f1d2c;text-decoration:underline;word-break:break-all">'
        f"{escaped}</a>"
    )


def _detail_row(label: str, value: str) -> str:
    return (
        '<tr><td style="padding:0 22px 16px 22px">'
        f'<div style="font-size:11px;line-height:16px;letter-spacing:.08em;'
        f'color:#8f1d2c;font-weight:700;margin:0 0 5px 0">{html.escape(label)}</div>'
        f'<div style="font-size:14px;line-height:23px;color:#342b2d;margin:0">'
        f'{html.escape(value or "未明确列出")}</div></td></tr>'
    )


def importance_stars(score: int) -> int:
    if score < 0:  # legacy digests did not carry the numeric score
        return 0
    if score >= 90:
        return 5
    if score >= 80:
        return 4
    if score >= 70:
        return 3
    if score >= 60:
        return 2
    return 1


def _render_card(item: EmailDigestItem) -> str:
    stars = importance_stars(item.importance_score)
    score_label = f"{stars}/5" if item.importance_score >= 0 else "未记录"
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
        'border="0" style="width:100%;border:1px solid #ead7da;border-collapse:separate;'
        'border-spacing:0;background-color:#ffffff;border-radius:8px;margin:0 0 14px 0">'
        '<tr><td style="padding:19px 22px 15px 18px;border-left:4px solid #9a2637">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse"><tr>'
        f'<td style="font-size:17px;line-height:25px;color:#3b1820;font-weight:700;vertical-align:top">{html.escape(item.headline)}</td>'
        '<td align="right" style="width:112px;padding-left:10px;white-space:nowrap;vertical-align:top">'
        f'<span style="color:#d9972b;font-size:16px;letter-spacing:1px">{"★" * stars}</span>'
        f'<span style="color:#d8d1d2;font-size:16px;letter-spacing:1px">{"☆" * (5 - stars)}</span>'
        f'<div style="font-size:11px;line-height:16px;color:#7b6870">{score_label}</div>'
        '</td></tr></table></td></tr>'
        + _detail_row("发生了什么", item.what_happened)
        + _detail_row("产业链解读", item.analysis)
        + _detail_row("涉及公司与传导路径", item.companies_and_path)
        + _detail_row("风险与不确定性", item.risks_and_uncertainties)
        + '<tr><td style="padding:0 22px 20px 22px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;background-color:#fbf6f7;border-collapse:collapse">'
        '<tr><td style="width:50%;vertical-align:top;padding:12px 14px;border-right:1px solid #ead7da">'
        '<div style="font-size:11px;line-height:16px;letter-spacing:.08em;color:#8f1d2c;'
        'font-weight:700;margin-bottom:5px">原始来源</div>'
        f'<div style="font-size:12px;line-height:19px">{source}</div></td>'
        '<td style="width:50%;vertical-align:top;padding:12px 14px">'
        '<div style="font-size:11px;line-height:16px;letter-spacing:.08em;color:#8f1d2c;'
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
    all_items = [item for section in sections for item in section.items]
    highest_stars = max(
        (importance_stars(item.importance_score) for item in all_items),
        default=0,
    )
    metadata_match = METADATA_RE.search(body)
    model_label = metadata_match.group(1).strip() if metadata_match else "模型信息未记录"
    prompt_label = metadata_match.group(2).strip() if metadata_match else "Prompt 未记录"
    runtime_label = f"分析模型 {model_label} · {prompt_label}"
    section_html = []
    for section in sections:
        cards = "".join(_render_card(item) for item in section.items)
        section_html.append(
            '<tr><td style="padding:8px 24px 2px 24px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'border="0" style="width:100%;border-collapse:collapse">'
            '<tr><td style="padding:10px 0 12px 0;border-bottom:2px solid #c98d96">'
            f'<span style="font-size:18px;line-height:26px;color:#7d1c2a;font-weight:700">'
            f'{html.escape(section.label)}</span>'
            f'<span style="font-size:12px;color:#7b6870;margin-left:8px">'
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
        '<body style="margin:0;padding:0;background-color:#f3eeee;'
        'font-family:Arial,Helvetica,sans-serif;color:#342b2d">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;background-color:#f3eeee;border-collapse:collapse">'
        '<tr><td align="center" style="padding:22px 10px">'
        '<table role="presentation" width="660" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;max-width:660px;background-color:#ffffff;'
        'border-collapse:collapse;border-radius:10px;overflow:hidden">'
        '<tr><td style="padding:28px 28px 24px 28px;background-color:#7a1724;'
        'border-top:5px solid #b65361">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse"><tr><td style="vertical-align:top">'
        '<div style="font-size:12px;line-height:18px;letter-spacing:.14em;'
        'color:#f1c7cd;font-weight:700;margin-bottom:8px">AI SUPPLY CHAIN INTELLIGENCE</div>'
        '<div style="font-size:28px;line-height:36px;color:#ffffff;font-weight:700">'
        'AI 产业链日报</div>'
        '<div style="font-size:13px;line-height:20px;color:#f3d9dd;margin-top:4px">'
        '每日产业链关键变化与证据摘要</div>'
        f'<div style="font-size:14px;line-height:22px;color:#f3d9dd;margin-top:8px">{html.escape(day)}</div>'
        '</td><td align="right" style="width:220px;padding-left:14px;vertical-align:top">'
        '<span style="display:inline-block;padding:7px 10px;background-color:#65121d;border:1px solid #a84d5a;'
        'font-size:11px;line-height:17px;color:#ffffff;border-radius:4px">'
        f'{html.escape(runtime_label)}</span></td></tr></table></td></tr>'
        '<tr><td style="padding:18px 24px 10px 24px;background-color:#fbf7f7">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;border-collapse:collapse">'
        '<tr><td style="width:33.33%;padding:10px 12px;border-right:1px solid #ead7da">'
        '<div style="font-size:11px;color:#7b6870;letter-spacing:.08em">新闻条数</div>'
        f'<div style="font-size:23px;line-height:30px;color:#7d1c2a;font-weight:700">'
        f'{item_count} <span style="font-size:13px;font-weight:400">条</span></div></td>'
        '<td style="width:33.33%;padding:10px 12px;border-right:1px solid #ead7da">'
        '<div style="font-size:11px;color:#7b6870;letter-spacing:.08em">覆盖产业链环节</div>'
        f'<div style="font-size:23px;line-height:30px;color:#7d1c2a;font-weight:700">{section_count} '
        '<span style="font-size:13px;font-weight:400">个</span></div></td>'
        '<td style="width:33.33%;padding:10px 12px">'
        '<div style="font-size:11px;color:#7b6870;letter-spacing:.08em">最高重要程度</div>'
        f'<div style="font-size:19px;line-height:30px;color:#d9972b;font-weight:700">{"★" * highest_stars}'
        f'<span style="font-size:12px;color:#7b6870;font-weight:400"> {highest_stars}/5</span></div></td>'
        '</tr></table></td></tr>'
        + "".join(section_html)
        + '<tr><td style="padding:22px 28px;background-color:#f8f2f3;border-top:1px solid #ead7da;'
        'font-size:12px;line-height:20px;color:#7b6870;text-align:center">'
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

    def unresolved_attempt(self, key: str, day: str) -> bool:
        latest = {}
        for entry in self._entries():
            if entry.get("idempotency_key") == key or entry.get("day") == day:
                latest[entry.get("idempotency_key")] = entry.get("status")
        return any(status in {"PENDING", "UNKNOWN"} for status in latest.values())

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
    day: str | None = None,
) -> dict:
    """Gate + optional send for an exact day, or latest for legacy CLI callers."""
    if day is not None:
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError:
            return {"status": "BLOCKED", "reasons": ["DIGEST_DATE_INVALID"]}
    path = digests_dir / f"{day}.md" if day else latest_digest_path(digests_dir)
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
    if ledger.unresolved_attempt(key, gate.day):
        return {"status": "BLOCKED", "day": gate.day, "reasons": ["PRIOR_DELIVERY_OUTCOME_UNRESOLVED"]}
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
