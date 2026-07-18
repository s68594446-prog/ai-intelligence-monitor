import argparse

from app.core.database import SessionLocal
from app.fixed_sources.service import fetch_fixed_source


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch one approved official fixed source")
    parser.add_argument("--source", required=True, help="Registered source key")
    parser.add_argument("--limit", type=int, default=None, help="Maximum new candidates, 1-10")
    args = parser.parse_args()
    with SessionLocal() as session:
        result = fetch_fixed_source(session, args.source, limit=args.limit)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
