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
from app.daily_ai.delivery_ledger import EmailLedger, LedgerError, audit_hash
from contextlib import nullcontext
from app.daily_ai.final_review import leak_findings, verify_review_seal
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
    review: dict | None = None
    audit_sha256: str | None = None


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
    fact_status: str = ""


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
    appendix = markdown.partition(AUDIT_MARKER)[2]
    matches = re.findall(r"### ops\.json\s*```json\s*(.*?)\s*```", appendix, re.S)
    review = None
    report = None
    if len(matches) == 1:
        try:
            report = json.loads(matches[0])
            audit = report.get("audit", {}) if isinstance(report, dict) else {}
            review = audit.get("final_review") if isinstance(audit, dict) else None
        except ValueError:
            pass
    text, markup = render_bodies(path.stem, body)
    if not verify_review_seal(review, day=path.stem, body=body, text=text, markup=markup):
        reasons.append("FINAL_REVIEW_MISSING_OR_STALE")
    if any(leak_findings(value) for value in (body, text, markup)):
        reasons.append("INTERNAL_FIELD_LEAK")
    return DigestGateResult(not reasons, reasons, path.stem, body, bool(ops), review,
                            audit_hash(report) if isinstance(report, dict) else None)


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
    fact_status = ""
    field = "what"
    for value in values[1:]:
        importance = IMPORTANCE_RE.fullmatch(value)
        if importance:
            importance_score = min(100, max(0, int(importance.group(1))))
        elif value.startswith("事实状态："):
            fact_status = value.removeprefix("事实状态：").strip()
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
        fact_status=fact_status,
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
        'style="color:#80dcff;text-decoration:underline;word-break:break-all">'
        f"{escaped}</a>"
    )


def _source_label(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


# Installed fonts only: CJK prose stays proportional; dates and metrics use fixed-width digits.
DIGEST_SANS = "Arial,'PingFang SC','Microsoft YaHei',sans-serif"
DIGEST_MONO = "Menlo,Consolas,'Courier New',monospace"


def _detail_row(label: str, value: str, *, caution: bool = False) -> str:
    content = html.escape(value or "未明确列出")
    label_style = "font-size:12px;line-height:18px;font-weight:bold;color:#a4ccd7;letter-spacing:.06em"
    if caution:
        return f"""
        <tr><td style="padding:0 0 16px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr><td bgcolor="#29262a" style="padding:12px 15px;border-left:2px solid #f3bb73">
              <div style="font-size:12px;line-height:18px;font-weight:bold;color:#ffd197">{html.escape(label)}</div>
              <p style="margin:5px 0 0;font-size:15px;line-height:25px;color:#e8dcd0">{content}</p>
            </td></tr>
          </table>
        </td></tr>"""
    return f"""
        <tr><td style="padding:0 0 18px">
          <div style="{label_style}">{html.escape(label)}</div>
          <p style="margin:5px 0 0;font-size:16px;line-height:27px;color:#dce7ee">{content}</p>
        </td></tr>"""


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
    source = _safe_link(item.source_url, label=_source_label(item.source_url)) if item.source_url else html.escape(
        item.source_note or "未提供"
    )
    if item.source_note and item.source_url:
        source += (
            '<div style="font-size:12px;line-height:19px;color:#adbfce;margin-top:4px">'
            f'{html.escape(item.source_note)}</div>'
        )
    if item.other_sources:
        supplemental = "<br>".join(
            _safe_link(url, label=_source_label(url))
            for url in item.other_sources
        )
    else:
        supplemental = '<span style="color:#9db2c3">无</span>'
    status = (
        '<span style="display:inline-block;margin:2px 0;padding:2px 9px;background-color:#163942;'
        'font-size:12px;line-height:18px;color:#a4ccd7">'
        f'事实状态：{html.escape(item.fact_status)}</span>'
    ) if item.fact_status else ""

    return f"""
      <table class="digest-card" role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="width:100%;table-layout:fixed;border-collapse:collapse;word-wrap:break-word">
        <tr><td style="padding:16px 0 12px">
          <h3 style="margin:0;font-family:{DIGEST_SANS};font-size:24px;line-height:34px;color:#f1f7fb;font-weight:bold;text-wrap:balance">{html.escape(item.headline)}</h3>
        </td></tr>
        <tr><td style="padding:0 0 18px;line-height:24px">
          <span style="display:inline-block;margin:0 12px 2px 0;white-space:nowrap"><span style="font-size:15px;color:#80ecdb">{"★" * stars}</span><span style="font-size:15px;color:#7993a6">{"☆" * (5 - stars)}</span>
          <span style="font-size:12px;color:#aec1ce">{score_label}</span></span>{status}
        </td></tr>
        {_detail_row("发生了什么", item.what_happened)}
        {_detail_row("产业链解读", item.analysis)}
        {_detail_row("涉及公司与传导路径", item.companies_and_path)}
        {_detail_row("风险与不确定性", item.risks_and_uncertainties, caution=True)}
        <tr><td style="padding:0 0 20px;border-bottom:1px solid #2b4258;color:#aec1ce;font-size:13px;line-height:22px">
          <div><strong>原始来源</strong>&nbsp; {source}</div>
          <div><strong>补充来源</strong>&nbsp; {supplemental}</div>
        </td></tr>
      </table>"""


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
    runtime_accent = "border-left:2px solid #f3bb73;padding-left:8px;" if " → " in model_label else ""
    section_html = []
    for section in sections:
        cards = "".join(_render_card(item) for item in section.items)
        section_html.append(f"""
          <tr><td style="padding:24px 5% 0">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
              <tr>
                <td style="white-space:nowrap;padding:0 12px 0 9px;border-left:3px solid #67ecd9">
                  <h2 style="margin:0;font-size:15px;line-height:23px;letter-spacing:.06em;color:#82eada">{html.escape(section.label)}</h2>
                </td>
                <td width="100%" style="width:100%"><div style="border-top:1px solid #36546b;font-size:1px;line-height:1px">&nbsp;</div></td>
                <td style="white-space:nowrap;padding-left:12px;font:12px {DIGEST_MONO};color:#a9c4d3">{len(section.items)} 条</td>
              </tr>
            </table>
            {cards}
          </td></tr>""")
    if not section_html:
        section_html.append(
            '<tr><td style="padding:34px 20px;text-align:center;font-size:16px;'
            'line-height:26px;color:#a4ccd7">今日无达到标准的产业链重点新闻</td></tr>'
        )
    metric_style = f"font-family:{DIGEST_MONO};font-size:25px;line-height:33px;font-weight:bold;color:#e6fbff"
    unit_style = "font:12px Arial,sans-serif;color:#a9c4d3"
    html_body = f"""<!doctype html>
    <html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="margin:0;padding:0;background-color:#09121f;color:#dce7ee;font-family:{DIGEST_SANS}">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#09121f">
        <tr><td align="center" style="padding:14px 6px">
          <!--[if mso]><table role="presentation" width="660" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
          <table role="presentation" width="660" cellpadding="0" cellspacing="0" bgcolor="#101e30"
                 style="width:100%;max-width:660px;table-layout:fixed;border-collapse:collapse;text-align:left;background-color:#101e30">
            <tr><td bgcolor="#0b1a2b" style="padding:10px 5%;border-top:3px solid #67ecd9;border-bottom:1px solid #294459">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
                <td style="font-family:{DIGEST_MONO};font-size:10px;line-height:15px;font-weight:bold;letter-spacing:.08em;color:#8ff1e0">AI SUPPLY CHAIN INTELLIGENCE</td>
                <td align="right" style="padding-left:6px;white-space:nowrap;font:12px {DIGEST_MONO};color:#b9cdda">{html.escape(day)}</td>
              </tr></table>
            </td></tr>
            <tr><td bgcolor="#122b40" style="padding:23px 5%;background-color:#122b40;background-image:linear-gradient(90deg,rgba(98,209,218,.08) 1px,transparent 1px),linear-gradient(rgba(98,209,218,.08) 1px,transparent 1px);background-size:18px 18px">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
                <td style="vertical-align:middle">
                  <h1 style="margin:0;font-size:34px;line-height:43px;font-weight:bold;letter-spacing:-.02em;color:#f0faff">AI 产业链日报</h1>
                  <p style="margin:5px 0 0;font-size:13px;line-height:20px;color:#bcd0de">每日产业链关键变化与证据摘要</p>
                </td>
                <td width="53" align="right" style="width:53px;padding-left:6px">
                  <table role="presentation" aria-hidden="true" width="48" cellpadding="0" cellspacing="0"
                         style="width:48px;border:4px dashed #6bc9c6;border-collapse:separate;border-spacing:0">
                    <tr><td height="39" align="center" bgcolor="#163e50" style="border:2px solid #82eada;font-size:1px;line-height:1px">
                      <div style="width:16px;height:16px;border:3px double #1a626a;background-color:#81e6d5;font-size:1px;line-height:1px">&nbsp;</div>
                    </td></tr>
                  </table>
                </td>
              </tr></table>
            </td></tr>
            <tr><td bgcolor="#142638" style="padding:0 5%;border-top:1px solid #304e63;border-bottom:1px solid #304e63">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="width:100%;table-layout:fixed;text-align:left">
                <tr style="font-size:12px;line-height:18px;color:#abc2d0">
                  <td style="padding:10px 3px 11px 0">新闻条数<div style="{metric_style}">{item_count} <span style="{unit_style}">条</span></div></td>
                  <td style="padding:10px 3px 11px 9px;border-left:1px solid #304e63">覆盖产业链环节<div style="{metric_style}">{section_count} <span style="{unit_style}">个</span></div></td>
                  <td style="padding:10px 0 11px 9px;border-left:1px solid #304e63">最高重要程度<div style="{metric_style}">{highest_stars}/5</div></td>
                </tr>
              </table>
            </td></tr>
            <tr><td style="padding:12px 5% 0">
              <div style="{runtime_accent}font-family:{DIGEST_MONO};font-size:12px;line-height:18px;color:#afc8d8;word-wrap:break-word">{html.escape(runtime_label)}</div>
            </td></tr>
            {"".join(section_html)}
            <tr><td style="padding:20px 5%;font-size:12px;line-height:20px;color:#afc8d8;text-align:center">
              内容仅用于产业研究与信息整理，不构成任何投资建议。
            </td></tr>
          </table>
          <!--[if mso]></td></tr></table><![endif]-->
        </td></tr>
      </table>
    </body></html>"""
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


def _deliver_latest(
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
    # Review the actual MIME inputs, not just an earlier renderer invocation.
    if (not verify_review_seal(gate.review, day=gate.day, body=gate.body,
                               text=request.text_body, markup=request.html_body)
            or leak_findings(request.text_body) or leak_findings(request.html_body)):
        return {"status": "BLOCKED", "day": gate.day, "reasons": ["FINAL_BODY_CHANGED"]}
    ledger = EmailLedger(ledger_path)
    if ledger.already_sent(key, gate.day):
        return {"status": "ALREADY_SENT", "day": gate.day, "idempotency_key": key, "recipient_count": len(request.to_addresses)}
    if ledger.unresolved_attempt(key, gate.day):
        return {"status": "BLOCKED", "day": gate.day, "reasons": ["PRIOR_DELIVERY_OUTCOME_UNRESOLVED"]}
    if not send:
        return {"status": "READY", "day": gate.day, "idempotency_key": key, "recipient_count": len(request.to_addresses), "dry_run": True}

    tracking = {
        "audit_sha256": gate.audit_sha256,
        "body_sha256": hashlib.sha256(gate.body.strip().encode()).hexdigest(),
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


def deliver_latest(settings: Settings, *, digests_dir: Path, ledger_path: Path, send: bool, day: str | None = None) -> dict:
    try:
        with EmailLedger(ledger_path).lock() if send else nullcontext():
            return _deliver_latest(settings, digests_dir=digests_dir, ledger_path=ledger_path, send=send, day=day)
    except LedgerError as exc:
        return {"status": "BLOCKED", "day": day, "reasons": [str(exc)]}
