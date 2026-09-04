from pydantic import SecretStr

from app.core.settings import Settings
from app.delivery.email_adapter import resolve_smtp_configuration
from app.scripts.validate_smtp_config import validate


def test_smtp_preflight_uses_existing_validator_and_never_serializes_password():
    settings = Settings(
        _env_file=None,
        EMAIL_DELIVERY_ENABLED=True,
        SMTP_HOST="smtp.example.com",
        SMTP_PORT=587,
        SMTP_USERNAME="sender@example.com",
        SMTP_PASSWORD=SecretStr("top-secret"),
        SMTP_FROM="sender@example.com",
        SMTP_TO="reader@example.net",
    )

    configuration, errors = resolve_smtp_configuration(settings)

    assert errors == []
    assert configuration is not None
    assert "top-secret" not in repr(configuration)


def test_workflow_smtp_preflight_returns_only_bounded_codes(monkeypatch):
    monkeypatch.setenv("EMAIL_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "top-secret")
    monkeypatch.setenv("SMTP_FROM", "sender@example.com")
    monkeypatch.setenv("SMTP_TO", "reader@example.net")

    result = validate()

    assert result == {"valid": True, "error_codes": [], "network_calls": 0}
    assert "top-secret" not in str(result)
