"""Secret-safe SMTP configuration preflight for the digest workflow.

This performs no network operation and prints only bounded validation codes.
The email adapter repeats the same validation immediately before any send.
"""

import json
import os
from pathlib import Path

from app.core.settings import Settings
from app.delivery.email_adapter import resolve_smtp_configuration


def validate() -> dict:
    configuration, errors = resolve_smtp_configuration(Settings())
    return {
        "valid": configuration is not None and not errors,
        "error_codes": sorted(set(errors)),
        "network_calls": 0,
    }


def main() -> None:
    result = validate()
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            handle.write(f"valid={'true' if result['valid'] else 'false'}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
