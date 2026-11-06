"""Confirmed corrections only; reviewer/model opinions are never auto-promoted."""
import hashlib
import json
from pathlib import Path

REGISTRY = Path(__file__).resolve().parents[3] / "config/daily_ai/confirmed_corrections.json"


def load_corrections(path: Path = REGISTRY) -> list[dict]:
    records = json.loads(path.read_text())
    if not isinstance(records, list):
        raise ValueError("correction registry must be a list")
    ids = set()
    for record in records:
        required = ("claim_id", "error_type", "source_url", "evidence", "wrong_text",
                    "resolution", "corrected_text", "confirmed_on")
        if (not isinstance(record, dict) or any(not isinstance(record.get(k), str) or not record[k] for k in required)
                or record.get("status") != "CONFIRMED"
                or record.get("confirmation_basis") != "preserved_source"
                or record["claim_id"] in ids):
            raise ValueError("only unique source-confirmed corrections are allowed")
        ids.add(record["claim_id"])
    return records


def registry_digest() -> str:
    return hashlib.sha256(json.dumps(load_corrections(), sort_keys=True).encode()).hexdigest()


def recurrence_findings(fields: dict[str, str], source_url: str, raw_text: str) -> list[dict]:
    from app.daily_ai.money_claims import facts
    found = []
    normalized_raw = " ".join(raw_text.split())
    for entry in load_corrections():
        if entry["source_url"] != source_url:
            continue
        if " ".join(entry["evidence"].split()) not in normalized_raw:
            continue  # changed/missing evidence is not proof of a recurrence
        # A newer source may legitimately report the previously wrong value.
        if entry.get("wrong_amount") and any(
            f["amount"] == entry["wrong_amount"] and f["currency"] == entry.get("currency")
            for f in facts(raw_text, source_url)
        ):
            continue
        for name, text in fields.items():
            repeats_amount = entry.get("wrong_amount") and any(
                f["amount"] == entry["wrong_amount"] and f["currency"] == entry.get("currency")
                for f in facts(text, source_url))
            if entry["wrong_text"] in text or repeats_amount:
                found.append({"claim_id": entry["claim_id"], "field": name,
                              "error_type": "CONFIRMED_ERROR_RECURRENCE", "status": "HOLD",
                              "evidence": entry["evidence"], "source_url": source_url,
                              "resolution": entry["resolution"], "corrected_text": entry["corrected_text"]})
    return found
