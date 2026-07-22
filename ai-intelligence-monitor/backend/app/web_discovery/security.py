import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


Resolver = Callable[[str], list[str]]


class WebSecurityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.username is not None or parts.password is not None:
        raise WebSecurityError("URL_CREDENTIALS_FORBIDDEN")
    try:
        host = (parts.hostname or "").casefold().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebSecurityError("INVALID_HOSTNAME") from exc
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    path = parts.path or "/"
    tracking_keys = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in tracking_keys
        ),
        doseq=True,
    )
    return urlunsplit((parts.scheme.casefold(), host + port, path, query, ""))


def system_resolver(hostname: str) -> list[str]:
    return sorted({item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)})


def _validate_addresses(addresses: list[str]):
    if not addresses:
        raise WebSecurityError("DNS_EMPTY")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise WebSecurityError("DNS_INVALID_ADDRESS") from exc
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise WebSecurityError("NON_PUBLIC_ADDRESS")


def validate_public_url(url: str, resolver: Resolver = system_resolver) -> tuple[str, list[str]]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise WebSecurityError("INVALID_URL") from exc
    if parts.scheme.casefold() != "https":
        raise WebSecurityError("HTTPS_REQUIRED")
    if parts.username is not None or parts.password is not None:
        raise WebSecurityError("URL_CREDENTIALS_FORBIDDEN")
    if port not in {None, 443}:
        raise WebSecurityError("NON_DEFAULT_PORT")
    try:
        host = (parts.hostname or "").casefold().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebSecurityError("INVALID_HOSTNAME") from exc
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise WebSecurityError("LOCALHOST_FORBIDDEN")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise WebSecurityError("IP_LITERAL_FORBIDDEN")
    try:
        addresses = resolver(host)
    except (OSError, UnicodeError, ValueError) as exc:
        raise WebSecurityError("DNS_RESOLUTION_FAILED") from exc
    _validate_addresses(addresses)
    return canonicalize_url(url), addresses
