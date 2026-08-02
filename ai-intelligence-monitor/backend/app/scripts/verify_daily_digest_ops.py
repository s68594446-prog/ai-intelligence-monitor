import argparse
import json
from pathlib import Path

from app.core.settings import Settings


def verify_latest_ops(runs_dir: Path) -> dict:
    candidates = sorted(Path(runs_dir).glob("*-daily-ai/ops.json"))
    if not candidates:
        return {
            "passed": False,
            "errors": ["OPS_RESULT_MISSING"],
            "offenders": {},
            "ops_path": None,
        }

    ops_path = candidates[-1]
    try:
        ops = json.loads(ops_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "passed": False,
            "errors": ["OPS_RESULT_INVALID"],
            "offenders": {},
            "ops_path": str(ops_path),
        }

    return {
        "passed": ops.get("passed") is True,
        "errors": list(ops.get("errors", [])),
        "offenders": dict(ops.get("offenders", {})),
        "ops_path": str(ops_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fail closed unless the latest daily AI run passed Digest Ops"
    )
    parser.add_argument("--runs-dir", type=Path)
    args = parser.parse_args()
    runs_dir = args.runs_dir or Settings().runs_dir
    result = verify_latest_ops(runs_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result["passed"]:
        print("::error::Digest Ops Gate failed; artifact retained, publish and email blocked")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
