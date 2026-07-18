import argparse
import json

from app.core.database import SessionLocal
from app.harness.service import build_harness


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a deterministic Phase 0 fixture")
    parser.add_argument("--scenario", default="verified_s3")
    args = parser.parse_args()
    with SessionLocal() as session:
        run = build_harness(session).run(args.scenario)
        print(json.dumps({
            "run_id": run.run_id,
            "status": run.status.value,
            "documents": run.documents_processed,
            "events": run.events_created,
            "signals": run.signals_created,
            "alerts": run.alerts_selected,
            "ops_check": run.ops_check,
        }, indent=2))


if __name__ == "__main__":
    main()

