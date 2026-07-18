from sqlalchemy.orm import Session

from app.core.settings import Settings, settings
from app.delivery.contracts import EmailDeliveryOutcome, EmailDeliveryResult
from app.delivery.email_adapter import EmailDeliveryAdapter, SMTPEmailDeliveryAdapter
from app.delivery.service import deliver_run_email


def coordinate_post_ops_email_delivery(
    session: Session,
    run_id: str,
    *,
    app_settings: Settings = settings,
    adapter: EmailDeliveryAdapter | None = None,
) -> EmailDeliveryResult | None:
    if not app_settings.email_auto_send:
        return None
    try:
        selected_adapter = adapter or SMTPEmailDeliveryAdapter(app_settings)
        return deliver_run_email(
            session,
            run_id,
            selected_adapter,
            app_settings=app_settings,
        )
    except Exception:
        return EmailDeliveryResult(
            run_id=run_id,
            status=EmailDeliveryOutcome.UNKNOWN,
            reasons=["POST_OPS_DELIVERY_COORDINATOR_ERROR"],
        )
