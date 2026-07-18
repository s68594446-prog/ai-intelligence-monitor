import argparse
import json

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.settings import settings
from app.harness.ops import OpsChecker
from app.models import RunAudit


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic operations checks")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--run-id")
    args = parser.parse_args()
    with SessionLocal() as session:
        if args.latest:
            run = session.scalar(select(RunAudit).order_by(RunAudit.started_at.desc()).limit(1))
            if run is None:
                raise SystemExit("no runs found")
            run_id = run.run_id
        else:
            run_id = args.run_id
        result = OpsChecker(session, settings.runs_dir).check(run_id)
        print(json.dumps({"run_id": run_id, **result}, indent=2))
        if not result["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

