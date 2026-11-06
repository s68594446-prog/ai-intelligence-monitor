import argparse
import json
import os
from pathlib import Path

from app.core.settings import Settings
from app.daily_ai.ops_report import report_passed
from app.daily_ai.email_digest import render_bodies
from app.daily_ai.final_review import verify_review_seal


def verify_latest_ops(runs_dir: Path, *, run_dir: Path | None = None, day: str | None = None) -> dict:
    candidates = [run_dir / "ops.json"] if run_dir else sorted(Path(runs_dir).glob("*-daily-ai/ops.json"))
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

    if not isinstance(ops, dict) or not isinstance(ops.get("errors", []), list) or not isinstance(ops.get("offenders", {}), dict):
        return {"passed": False, "errors": ["OPS_RESULT_INVALID"], "offenders": {}, "ops_path": str(ops_path)}
    passed = report_passed(ops)
    errors = list(ops.get("errors", []))
    if run_dir or day:
        try:
            review = ops["audit"]["final_review"]
            body = (ops_path.parent / "04-digest.txt").read_text().strip()
            expected_day = day or review["day"]
            text, markup = render_bodies(expected_day, body)
            if not verify_review_seal(review, day=expected_day, body=body, text=text, markup=markup):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            passed = False
            errors.append("FINAL_REVIEW_MISSING_OR_STALE")
    if not passed and not errors:
        errors = ["OPS_RESULT_NOT_PASSED"]
    return {
        "passed": passed,
        "errors": errors,
        "offenders": dict(ops.get("offenders", {})),
        "ops_path": str(ops_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fail closed unless the latest daily AI run passed Digest Ops"
    )
    parser.add_argument("--runs-dir", type=Path)
    parser.add_argument("--run-dir", type=Path, default=os.environ.get("DAILY_RUN_DIR"))
    parser.add_argument("--day", default=os.environ.get("DIGEST_DAY"))
    args = parser.parse_args()
    runs_dir = args.runs_dir or Settings().runs_dir
    result = verify_latest_ops(runs_dir, run_dir=args.run_dir, day=args.day)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result["passed"]:
        print("::error::Digest Ops Gate failed; artifact retained, publish and email blocked")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
