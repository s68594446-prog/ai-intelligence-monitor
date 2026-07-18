import argparse
import json

from app.core.database import SessionLocal
from app.harness.service import replay_run


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a deterministic run without overwriting it")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    with SessionLocal() as session:
        replay, diff = replay_run(session, args.run_id)
        print(json.dumps({"run_id": replay.run_id, "replay_of": replay.replay_of, **diff}, indent=2))
        if not diff["stable"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

