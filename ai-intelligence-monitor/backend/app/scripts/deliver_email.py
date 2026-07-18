import argparse
import json

from app.core.database import SessionLocal
from app.core.settings import settings
from app.delivery.contracts import EmailDeliveryOutcome, EmailDeliveryResult
from app.delivery.email_adapter import SMTPEmailDeliveryAdapter
from app.delivery.service import deliver_run_email


def result_exit_code(result: EmailDeliveryResult) -> int:
    if result.status in {
        EmailDeliveryOutcome.READY,
        EmailDeliveryOutcome.SENT,
        EmailDeliveryOutcome.ALREADY_SENT,
    }:
        return 0
    if result.status == EmailDeliveryOutcome.BLOCKED:
        return 2
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check or execute controlled email delivery for one completed run"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Perform SMTP delivery; without this flag only deterministic gates are checked",
    )
    args = parser.parse_args()
    adapter = SMTPEmailDeliveryAdapter(settings) if args.send else None
    with SessionLocal() as session:
        result = deliver_run_email(
            session,
            args.run_id,
            adapter,
            app_settings=settings,
            dry_run=not args.send,
        )
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    exit_code = result_exit_code(result)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
