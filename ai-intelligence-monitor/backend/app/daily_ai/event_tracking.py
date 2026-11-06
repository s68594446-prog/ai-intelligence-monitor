"""Conservative cross-day memory for Phase 3C; zero model/network calls.

No fuzzy company-name merges. Only identical canonical URLs or explicitly
reviewed URL aliases share a thread. Publication requires a matching SENT body
hash, not a fetch log, dry-run output, or an arbitrary file in runs/.
"""
import json
import re
from datetime import date
from pathlib import Path

from app.daily_ai.final_review import sha
from app.daily_ai.ops_report import report_passed
from app.web_discovery.security import canonicalize_url
from app.daily_ai.delivery_ledger import EmailLedger, LedgerError, audit_hash

CONFIG = Path(__file__).resolve().parents[3] / "config/daily_ai/event_tracking.json"


def load_tracking_config(path: Path = CONFIG) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data.get("aliases"), dict) or not isinstance(data.get("judgments"), list):
        raise ValueError("invalid event tracking config")
    if len(data["judgments"]) > 5:
        raise ValueError("keep at most five explicit reviewable judgments")
    data["aliases"] = {canonicalize_url(url): thread for url, thread in data["aliases"].items()}
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", value) for value in data["aliases"].values()):
        raise ValueError("invalid reviewed thread ID")
    return data


def thread_id(url: str, aliases: dict[str, str]) -> str:
    canonical = canonicalize_url(url)
    return aliases.get(canonical, "event-" + sha(canonical)[:20])


def load_published_threads(digests_dir: Path, *, current_day: date) -> tuple[dict, list[str]]:
    from app.daily_ai.email_digest import split_digest_body
    sent = set()
    warnings = []
    ledger = digests_dir / ".email-log.jsonl"
    if not ledger.is_file():
        return {}, []
    try:
        entries = EmailLedger(ledger)._entries()
    except LedgerError:
        return {}, ["MALFORMED_PUBLICATION_LEDGER_ENTRY"]
    for entry in entries:
        if entry["status"] == "SENT" and re.fullmatch(r"[0-9a-f]{64}", entry.get("body_sha256", "")):
            sent.add((entry["day"], entry["body_sha256"], entry.get("audit_sha256")))
    latest = {}
    for path in sorted(digests_dir.glob("????-??-??.md")):
        try:
            day = date.fromisoformat(path.stem)
            if not 0 < (current_day - day).days <= 90:
                continue
            content = path.read_text()
            body_hash = sha(split_digest_body(content).strip())
            if not any(row[:2] == (path.stem, body_hash) for row in sent):
                continue
            appendix = content.partition("## 审计")[2]
            blocks = re.findall(r"### ops\.json\s*```json\s*(.*?)\s*```", appendix, re.S)
            if len(blocks) != 1:
                raise ValueError("ambiguous audit")
            report = json.loads(blocks[0])
            if (path.stem, body_hash, audit_hash(report)) not in sent:
                warnings.append("THREAD_AUDIT_NOT_BOUND_TO_SEND:" + path.stem)
                continue
            if not report_passed(report):
                continue
            from app.daily_ai.email_digest import parse_digest_sections
            published_urls = {item.source_url for section in parse_digest_sections(split_digest_body(content)) for item in section.items}
            for record in report.get("audit", {}).get("event_threads", []):
                if record["day"] != path.stem or record["source_url"] not in published_urls or record["change"] in {"UNCHANGED", "STATUS_REVIEW_REQUIRED"}:
                    continue
                latest[record["thread_id"]] = {**record, "publication_status": "PUBLISHED"}
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            warnings.append("INVALID_THREAD_ARCHIVE:" + path.stem)
    return latest, sorted(set(warnings))


def track_events(items: list[dict], *, analyses: list, previous: dict, day: date,
                 aliases: dict[str, str]) -> tuple[list[dict], list[dict]]:
    by_url = {str(a.evidence[0].source_url): a for a in analyses}
    kept, records = [], []
    for item in items:
        url = item["url"]
        analysis = by_url[url]
        tid = thread_id(url, aliases)
        old = previous.get(tid)
        evidence = [{"day": day.isoformat(), "source_url": str(e.source_url), "quote": e.quote,
                     "evidence_id": sha(str(e.source_url) + "|" + " ".join(e.quote.split()))}
                    for e in analysis.evidence]
        prior_chain = old.get("evidence_chain", []) if old else []
        old_ids = {e["evidence_id"] for e in prior_chain}
        added = [e for e in evidence if e["evidence_id"] not in old_ids]
        state = item["announced_vs_delivered"]
        change = ("NEW" if old is None else "STATUS_CHANGED" if state != old["state"]
                  else "NEW_EVIDENCE" if added else "UNCHANGED")
        if old and state != old["state"] and not added:
            change = "STATUS_REVIEW_REQUIRED"
        chain = [*prior_chain, *added]
        record = {"thread_id": tid, "day": day.isoformat(), "source_url": url,
                  "last_reported_conclusion": old["conclusion"] if old else None,
                  "conclusion": item["what_happened"], "previous_state": old["state"] if old else None,
                  "state": state, "change": change, "new_evidence": added,
                  "evidence_chain": chain[-100:], "evidence_chain_truncated": len(chain) > 100 or bool(old and old.get("evidence_chain_truncated")),
                  "trend_candidate": len({e["day"] for e in chain}) >= 3,
                  "publication_status": "CANDIDATE"}
        records.append(record)
        if change not in {"UNCHANGED", "STATUS_REVIEW_REQUIRED"}:
            # Reader sees today's existing evidence-backed account. Full prior
            # conclusions and evidence stay in the audit, not in email internals.
            kept.append({**item, "thread_id": tid,
                         "thread_note": f"跨日跟踪：较{old['day']}报道新增证据" + ("，公告状态发生变化。" if change == "STATUS_CHANGED" else "。") if old else ""})
    return kept, records


def review_judgments(judgments: list[dict], *, today: date, raw_by_url: dict[str, str]) -> list[dict]:
    """Due-date checklist; only human-confirmed, exact-source decisions resolve it.

    No news/absence/model score is treated as fulfillment. Human confirmation is
    a reviewed configuration input, not something inferred or written here.
    """
    results = []
    for item in judgments:
        review_date = date.fromisoformat(item["review_date"])
        if not item.get("verification_condition") or not item.get("thread_id") or not item.get("claim_id"):
            raise ValueError("judgment needs explicit identity, condition and date")
        result = {"claim_id": item["claim_id"], "thread_id": item["thread_id"],
                  "verification_condition": item["verification_condition"],
                  "review_date": review_date.isoformat(), "due": today >= review_date,
                  "status": "PENDING", "evidence": []}
        decision = item.get("confirmed_review", {})
        if (today >= review_date and decision.get("confirmed_by") == "human"
                and decision.get("status") in {"FULFILLED", "NOT_FULFILLED", "PARTIAL"}
                and review_date <= date.fromisoformat(decision["date"]) <= today
                and decision.get("quote") and decision["quote"] in raw_by_url.get(decision.get("source_url"), "")):
            result.update(status=decision["status"], evidence=[decision])
        results.append(result)
    return results
