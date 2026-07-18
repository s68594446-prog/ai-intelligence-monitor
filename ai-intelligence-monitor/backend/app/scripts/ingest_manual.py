import argparse
import json
from pathlib import Path

from app.core.database import SessionLocal
from app.manual.contracts import ManualDocumentCreate
from app.manual.service import analyze_manual_document, submit_manual_document


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest one manually supplied DRAM/HBM source")
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--source-tier", required=True, choices=["T1", "T2", "T3"])
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--url")
    parser.add_argument("--language", default="en")
    parser.add_argument("--notes")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--analyze", action="store_true")
    args = parser.parse_args()

    payload = ManualDocumentCreate(
        title=args.title,
        url=args.url,
        source_name=args.source_name,
        source_tier=args.source_tier,
        published_at=args.published_at,
        language=args.language,
        raw_text=args.file.read_text(encoding="utf-8"),
        optional_notes=args.notes,
    )
    with SessionLocal() as session:
        created = submit_manual_document(session, payload)
        result: dict = {"ingestion": created.model_dump(mode="json")}
        if args.analyze:
            analysis = analyze_manual_document(session, created.document_id)
            result["analysis"] = analysis.model_dump(mode="json")
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

