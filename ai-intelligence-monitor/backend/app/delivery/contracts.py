from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EmailDeliveryOutcome(StrEnum):
    READY = "READY"
    SENT = "SENT"
    ALREADY_SENT = "ALREADY_SENT"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class EmailDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    from_address: str
    to_addresses: tuple[str, ...]
    subject: str
    html_body: str
    text_body: str
    message_id: str


class EmailProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider_message_id: str


class EmailDeliveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str
    status: EmailDeliveryOutcome
    reasons: list[str] = Field(default_factory=list)
    attempt_id: int | None = None
    idempotency_key: str | None = None
    provider_message_id: str | None = None
    recipient_count: int = 0
    dry_run: bool = False
