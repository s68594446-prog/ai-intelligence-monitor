"""Deterministic Phase 3C review, not a general semantic fact checker.

Never repair prose or invent numbers. Unsupported/ambiguous monetary claims are
held, not entered as confirmed errors. Exact source spans stay in audit only.
"""
import hashlib
import html
import re
from collections import Counter
from html.parser import HTMLParser

from app.daily_ai.money_claims import facts
from app.daily_ai.article_integrity import source_integrity_issues

VERSION = "final-review-v2"
INTERNAL = re.compile(r"(?:raw_text|raw_response|system_instruction|provider-request-events|"
                      r"analysis-failures|Traceback|x-goog-api-key|SMTP_PASSWORD)|"
                      r"(?:ops|provider-usage)\.json", re.I)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def review_fields(fields: dict[str, str], source_url: str, raw_text: str) -> dict:
    source_facts = facts(raw_text, source_url)
    claims = [c for name, text in fields.items() for c in facts(text, source_url, field=name)]
    findings = []
    for code in source_integrity_issues(raw_text):
        findings.append({"error_type": code, "source_url": source_url, "status": "HOLD"})
    for name, text in fields.items():
        if leak_findings(text):
            findings.append({"error_type": "INTERNAL_FIELD_LEAK", "field": name,
                             "status": "HOLD", "tokens": leak_findings(text)})
    for claim in claims:
        matches = [f for f in source_facts if f["amount"] == claim["amount"] and f["currency"] == claim["currency"]]
        comparable = [f for f in matches if claim["period"] == "unspecified"
                      or claim["period"] == f["period"]]
        code = None
        if not matches:
            code = "AMOUNT_UNVERIFIED"
        elif not comparable:
            code = "PERIOD_UNVERIFIED"
        # One source record must support every tested dimension. Do not splice
        # a paid flag from one fact with a cash flag from another equal value.
        checks = [
            ("SCOPE_UNVERIFIED", lambda f: not claim["scope_tags"] or not f["scope_tags"]
             or set(claim["scope_tags"]) <= set(f["scope_tags"])),
            ("METRIC_UNVERIFIED", lambda f: not claim["metric_tags"] or not f["metric_tags"]
             or set(claim["metric_tags"]) <= set(f["metric_tags"])),
            ("BOUND_LOST", lambda f: claim["bound"] != "exact" or f["bound"] == "exact"),
            ("BOUND_UNVERIFIED", lambda f: claim["bound"] == "exact"
             or f["bound"] in {claim["bound"], "exact"}),
            ("PAYMENT_UNVERIFIED", lambda f: claim["fact_status"] != "paid" or f["fact_status"] == "paid"),
            ("ESTIMATE_AS_FACT", lambda f: claim["fact_status"] == "estimate" or f["fact_status"] != "estimate"),
            ("COMPUTE_AS_CASH", lambda f: claim["unit"] != "cash" or f["unit"] != "compute_access"),
        ]
        if code is None:
            for reason, supported in checks:
                comparable = [f for f in comparable if supported(f)]
                if not comparable:
                    code = reason
                    break
        claim["evidence_ids"] = [f["claim_id"] for f in comparable]
        claim["review_status"] = "HOLD" if code else "SUPPORTED_VALUE"
        if code:
            findings.append({"claim_id": claim["claim_id"], "field": claim["field"],
                             "error_type": code, "status": "HOLD", "evidence": matches})
    # A missing disclosure is not inferred merely because the quote omitted it.
    # Only narrow, explicit annual-amount denials are checked; exact payment dates
    # or unannounced partners are distinct and are not contradicted by an allocation.
    annual = [f for f in source_facts if f["period"] == "annual"]
    for name, text in fields.items():
        if re.search(r"未披露|未公布|未知", text) and re.search(
                r"(?:每年|年度).{0,8}(?:金额|额度|分配|拨付)|(?:金额|额度|分配).{0,8}(?:每年|年度)", text):
            findings.append({"claim_id": sha(source_url + name + text)[:24], "field": name,
                             "error_type": "DISCLOSURE_NEEDS_REVIEW", "status": "HOLD", "evidence": annual})
    return {"claims": claims, "source_facts": source_facts, "findings": findings,
            "passed": not findings}


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))

    def handle_data(self, data):
        self.parts.append(data)


def visible_text(markup: str) -> str:
    parser = _VisibleText()
    parser.feed(markup)
    return " ".join(parser.parts)


def leak_findings(text: str) -> list[str]:
    # Match decoded text as well as markup so entity encoding cannot hide keys.
    return sorted(set(m[0] for m in INTERNAL.finditer(html.unescape(text))))


def body_hashes(markdown: str, text: str, markup: str) -> dict:
    return {"markdown": sha(markdown.strip()), "text": sha(text.strip()), "html": sha(markup.strip())}


def unrendered_lines(body: str, items: list) -> list[str]:
    """Every non-header reader line must survive parsing into a displayed field.

    In particular, prose after a source URL used to disappear silently from HTML.
    This checks non-numeric prose too; a money counter alone cannot catch it.
    """
    from app.daily_ai.email_digest import SECTION_RE, IMPORTANCE_RE, METADATA_RE
    displayed = [getattr(item, name) for item in items for name in
                 ("headline", "what_happened", "analysis", "companies_and_path", "risks_and_uncertainties", "source_note")]
    leftovers = []
    for line in (line.strip() for line in body.splitlines() if line.strip()):
        if line in {"AI产业链每日重点", "───", "今日无达到标准的产业链重点新闻"} or SECTION_RE.fullmatch(line) or IMPORTANCE_RE.fullmatch(line) or METADATA_RE.fullmatch(line):
            continue
        if line.startswith("🔗 来源：") or line.startswith("其他来源："):
            # Validity and multiplicity of parsed links are checked separately.
            expected = [item.source_url + item.source_note for item in items]
            expected += ["、".join(item.other_sources) for item in items]
            if line.split("：", 1)[1].strip() in expected:
                continue
        value = re.sub(r"^(?:💡 分析：|📍 涉及：|⚠️ 风险与不确定性：)", "", line).strip()
        if value.startswith("【") and value.endswith("】"):
            value = value[1:-1]
        if not any(value in field for field in displayed):
            leftovers.append(line)
    return leftovers


def review_rendered(*, day: str, body: str, raw_by_url: dict[str, str],
                    expected_urls: set[str] | None = None) -> dict:
    # Local import avoids a renderer/reviewer module cycle.
    from app.daily_ai.email_digest import parse_digest_sections, render_bodies
    text, markup = render_bodies(day, body)
    findings, claims, source_facts = [], [], []
    items = [i for s in parse_digest_sections(body) for i in s.items]
    if not items and body.strip() and "今日无达到标准的产业链重点新闻" not in body:
        findings.append({"error_type": "UNPARSED_READER_BODY", "status": "HOLD"})
    if unrendered_lines(body, items):
        findings.append({"error_type": "UNRENDERED_READER_CONTENT", "status": "HOLD"})
    urls = {i.source_url for i in items}
    if expected_urls is not None and (urls != expected_urls or len(items) != len(expected_urls)):
        findings.append({"error_type": "RENDERED_ITEMS_MISMATCH", "status": "HOLD"})
    for item in items:
        if not item.headline or not item.what_happened or not item.analysis or not item.source_url:
            findings.append({"error_type": "INCOMPLETE_READER_ITEM", "status": "HOLD"})
        fields = {name: getattr(item, name) for name in
                  ("headline", "what_happened", "analysis", "companies_and_path", "risks_and_uncertainties", "source_note")}
        if item.source_url not in raw_by_url:
            findings.append({"error_type": "SOURCE_SNAPSHOT_MISSING", "status": "HOLD",
                             "source_url": item.source_url})
        review = review_fields(fields, item.source_url, raw_by_url.get(item.source_url, ""))
        claims.extend(review["claims"])
        source_facts.extend(review["source_facts"])
        findings.extend(review["findings"])
        visible = visible_text(markup)
        for name, value in fields.items():
            if value and value not in visible:
                findings.append({"error_type": "HTML_CONTENT_MISMATCH", "field": name, "status": "HOLD"})
    for name, value in {"markdown": body, "text": text, "html": markup}.items():
        if leak_findings(value):
            findings.append({"error_type": "INTERNAL_FIELD_LEAK", "surface": name,
                             "status": "HOLD", "tokens": leak_findings(value)})
    parser = _VisibleText()
    parser.feed(markup)
    expected_links = Counter(url for item in items for url in (item.source_url, *item.other_sources) if url)
    if Counter(parser.links) != expected_links:
        findings.append({"error_type": "RENDERED_LINK_MISMATCH", "status": "HOLD"})
    def values(value):
        return Counter((f["amount"], f["currency"]) for f in facts(value, ""))
    parsed_values = Counter((f["amount"], f["currency"]) for f in claims)
    if values(body) != parsed_values:
        findings.append({"error_type": "UNREVIEWED_RENDERED_NUMBER", "status": "HOLD"})
    if values(text) != values(body) or values(visible_text(markup)) != values(body):
        findings.append({"error_type": "RENDERED_NUMBER_MISMATCH", "status": "HOLD"})
    return {"version": VERSION, "day": day, "passed": not findings,
            "body_hashes": body_hashes(body, text, markup), "findings": findings,
            "claims": claims, "source_facts": source_facts,
            "coverage": "monetary-values-periods-status; not general semantic verification"}


def verify_review_seal(review: dict | None, *, day: str, body: str, text: str, markup: str) -> bool:
    return (isinstance(review, dict) and review.get("version") == VERSION
            and review.get("passed") is True and review.get("findings") == []
            and review.get("day") == day
            and review.get("body_hashes") == body_hashes(body, text, markup))


def review_seal(report: dict) -> dict:
    """Keep full evidence in final-review.json; archive only the delivery seal."""
    return {**{key: report[key] for key in ("version", "day", "passed", "body_hashes", "coverage")},
            "findings": [{key: value for key, value in finding.items() if key != "evidence"}
                         for finding in report["findings"]],
            "claim_count": len(report["claims"]), "audit_artifact": "final-review.json"}
