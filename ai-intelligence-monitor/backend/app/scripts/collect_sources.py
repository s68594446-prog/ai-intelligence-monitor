import argparse
import json

from app.core.database import SessionLocal
from app.source_collections.contracts import CollectionRequest
from app.source_collections.service import collect_sources


def main():
    parser = argparse.ArgumentParser(description="Collect approved DRAM/HBM official sources")
    parser.add_argument("--coverage", default="dram_hbm", choices=["dram_hbm"])
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--since-hours", type=int, default=168)
    parser.add_argument("--max-items-per-source", type=int, default=5)
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    request = CollectionRequest(
        coverage_id=args.coverage,
        source_ids=args.source,
        since_hours=args.since_hours,
        max_items_per_source=args.max_items_per_source,
        analyze=args.analyze,
        dry_run=args.dry_run,
    )
    with SessionLocal() as session:
        result = collect_sources(session, request)
        print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
