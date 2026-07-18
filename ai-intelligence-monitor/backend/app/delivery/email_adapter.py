import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from pydantic import SecretStr

from app.core.settings import Settings
from app.delivery.contracts import EmailDeliveryRequest, EmailProviderResult


class EmailProviderError(RuntimeError):
    def __init__(self, code: str, *, outcome_unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


class EmailDeliveryAdapter(Protocol):
    def send(self, request: EmailDeliveryRequest) -> EmailProviderResult:
        ...


@dataclass(frozen=True)
class SMTPConfiguration:
    host: str
    port: int
    username: str
    password: SecretStr
    from_address: str
    recipients: tuple[str, ...]
    security: str
    timeout_seconds: float
    message_id_domain: str


_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


def _has_header_break(value: str) -> bool:
    return "\r" in value or "\n" in value


def normalize_address(value: str) -> str:
    address = value.strip()
    if not address or _has_header_break(address) or address.count("@") != 1:
        raise ValueError("invalid email address")
    local, domain = address.rsplit("@", 1)
    if not local or not _DOMAIN_RE.fullmatch(domain) or " " in local:
        raise ValueError("invalid email address")
    if any(character in local for character in '<>()[],:;\\"'):
        raise ValueError("invalid email address")
    return f"{local.lower()}@{domain.lower()}"


def normalize_recipients(value: str) -> tuple[str, ...]:
    if not value.strip():
        raise ValueError("recipient list is empty")
    parts = value.split(",")
    if any(not part.strip() for part in parts):
        raise ValueError("recipient list contains an empty address")
    return tuple(sorted(set(normalize_address(part) for part in parts)))


def resolve_smtp_configuration(settings: Settings) -> tuple[SMTPConfiguration | None, list[str]]:
    errors: list[str] = []
    host = (settings.smtp_host or "").strip()
    username = (settings.smtp_username or "").strip()
    password = settings.smtp_password
    from_value = (settings.smtp_from_address or "").strip()
    if not host or _has_header_break(host) or not _HOST_RE.fullmatch(host):
        errors.append("SMTP_HOST_INVALID")
    if not username or _has_header_break(username):
        errors.append("SMTP_USERNAME_MISSING")
    if password is None:
        errors.append("SMTP_PASSWORD_MISSING")
    try:
        from_address = normalize_address(from_value)
    except ValueError:
        from_address = ""
        errors.append("SMTP_FROM_ADDRESS_INVALID")
    try:
        recipients = normalize_recipients(settings.smtp_to_addresses)
    except ValueError:
        recipients = ()
        errors.append("SMTP_TO_ADDRESSES_INVALID")
    domain = (settings.smtp_message_id_domain or "").strip().lower()
    if not domain and from_address:
        domain = from_address.rsplit("@", 1)[1]
    if not domain or _has_header_break(domain) or not _DOMAIN_RE.fullmatch(domain):
        errors.append("SMTP_MESSAGE_ID_DOMAIN_INVALID")
    if errors:
        return None, errors
    return SMTPConfiguration(
        host=host,
        port=settings.smtp_port,
        username=username,
        password=password,
        from_address=from_address,
        recipients=recipients,
        security=settings.smtp_security,
        timeout_seconds=settings.smtp_timeout_seconds,
        message_id_domain=domain,
    ), []


class SMTPEmailDeliveryAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def build_message(request: EmailDeliveryRequest) -> EmailMessage:
        for value in (request.subject, request.from_address, request.message_id, *request.to_addresses):
            if _has_header_break(value):
                raise EmailProviderError("EMAIL_HEADER_INVALID")
        message = EmailMessage()
        message["Subject"] = request.subject
        message["From"] = request.from_address
        message["To"] = "undisclosed-recipients:;"
        message["Message-ID"] = request.message_id
        message.set_content(request.text_body)
        message.add_alternative(request.html_body, subtype="html")
        return message

    def send(self, request: EmailDeliveryRequest) -> EmailProviderResult:
        configuration, errors = resolve_smtp_configuration(self.settings)
        if configuration is None:
            raise EmailProviderError(errors[0] if errors else "SMTP_CONFIGURATION_INVALID")
        if (
            request.from_address != configuration.from_address
            or request.to_addresses != configuration.recipients
        ):
            raise EmailProviderError("SMTP_REQUEST_CONFIGURATION_MISMATCH")
        message = self.build_message(request)
        smtp = None
        send_started = False
        try:
            if configuration.security == "SSL":
                smtp = smtplib.SMTP_SSL(
                    configuration.host,
                    configuration.port,
                    timeout=configuration.timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                smtp = smtplib.SMTP(
                    configuration.host,
                    configuration.port,
                    timeout=configuration.timeout_seconds,
                )
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(configuration.username, configuration.password.get_secret_value())
            send_started = True
            refused = smtp.send_message(
                message,
                from_addr=configuration.from_address,
                to_addrs=list(configuration.recipients),
            )
            if refused:
                raise EmailProviderError(
                    "SMTP_PARTIAL_RECIPIENT_ACCEPTANCE",
                    outcome_unknown=True,
                )
        except EmailProviderError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailProviderError("SMTP_AUTHENTICATION_FAILED") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise EmailProviderError("SMTP_RECIPIENT_REJECTED") from exc
        except (TimeoutError, OSError, smtplib.SMTPServerDisconnected) as exc:
            raise EmailProviderError(
                "SMTP_DELIVERY_UNKNOWN" if send_started else "SMTP_NETWORK_ERROR",
                outcome_unknown=send_started,
            ) from exc
        except smtplib.SMTPException as exc:
            raise EmailProviderError(
                "SMTP_DELIVERY_UNKNOWN" if send_started else "SMTP_PROVIDER_ERROR",
                outcome_unknown=send_started,
            ) from exc
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    pass
        return EmailProviderResult(provider_message_id=request.message_id)
