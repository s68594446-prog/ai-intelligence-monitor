"""Strict interpretation of saved Phase 3C reports at publication boundaries.

This validates the serialized report, not source evidence or frozen core Ops.
Historical reports with only a boolean ``passed`` remain readable.
"""


def report_passed(report: object) -> bool:
    if not isinstance(report, dict) or report.get("passed") is not True:
        return False
    checks = report.get("checks", {})
    errors = report.get("errors", [])
    return (
        isinstance(checks, dict)
        and all(value is True for value in checks.values())
        and isinstance(errors, list)
        and not errors
    )
