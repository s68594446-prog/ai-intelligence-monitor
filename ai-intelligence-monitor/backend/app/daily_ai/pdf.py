import hashlib
from io import BytesIO
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pypdf import PdfReader

from app.daily_ai.contracts import PdfExtraction
from app.daily_ai.discovery import official_source
from app.web_discovery.security import WebSecurityError, system_resolver, validate_public_url


def extract_pdf_text(body: bytes, *, source_url: str, content_type: str, max_bytes: int = 8_000_000, max_pages: int = 80, max_chars: int = 200_000) -> PdfExtraction:
    digest = hashlib.sha256(body).hexdigest()
    blocked = lambda code, pages=0: PdfExtraction(status="BLOCKED_PDF", source_url=source_url, sha256=digest, page_count=pages, error_code=code)
    if len(body) > max_bytes:
        return blocked("PDF_TOO_LARGE")
    if not body.startswith(b"%PDF-") or "pdf" not in content_type.lower():
        return blocked("INVALID_PDF_TYPE")
    try:
        reader = PdfReader(BytesIO(body), strict=True)
        if reader.is_encrypted:
            return blocked("ENCRYPTED_PDF")
        if len(reader.pages) > max_pages:
            return blocked("PDF_TOO_MANY_PAGES", len(reader.pages))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return blocked("DAMAGED_PDF")
    text = text[:max_chars].strip()
    if not text:
        return blocked("NO_TEXT_LAYER", len(reader.pages))
    return PdfExtraction(status="SUCCESS", source_url=source_url, sha256=digest, page_count=len(reader.pages), text=text)


class SafeOfficialPdfFetcher:
    """One-shot T1 PDF fetch with DNS pinning and redirect revalidation."""

    def __init__(self, *, client: httpx.Client, resolver=system_resolver, max_bytes=8_000_000, max_redirects=3):
        self.client = client
        self.resolver = resolver
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    def fetch(self, url: str) -> PdfExtraction:
        empty = b""
        if not official_source(url):
            return extract_pdf_text(empty, source_url=url, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": "NON_OFFICIAL_SOURCE"})
        current = url
        seen = set()
        for _ in range(self.max_redirects + 1):
            try:
                canonical, before = validate_public_url(current, self.resolver)
            except WebSecurityError as exc:
                return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": exc.code})
            if canonical in seen:
                return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": "REDIRECT_LOOP"})
            seen.add(canonical)
            parts = urlsplit(canonical)
            pinned = sorted(before)[0]
            netloc = f"[{pinned}]" if ":" in pinned else pinned
            request = self.client.build_request("GET", urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), headers={"Host": parts.hostname, "Accept": "application/pdf", "Accept-Encoding": "identity"}, extensions={"sni_hostname": parts.hostname})
            try:
                response = self.client.send(request, stream=True, follow_redirects=False)
            except (httpx.TimeoutException, httpx.NetworkError):
                return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": "NETWORK_ERROR"})
            try:
                _, after = validate_public_url(canonical, self.resolver)
            except WebSecurityError as exc:
                response.close()
                return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": exc.code})
            if set(before) != set(after):
                response.close()
                return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": "DNS_REBINDING"})
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                response.close()
                if not location:
                    return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": "REDIRECT_WITHOUT_LOCATION"})
                current = urljoin(current, location)
                if not official_source(current):
                    return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": "REDIRECT_NON_OFFICIAL"})
                continue
            if response.status_code != 200:
                response.close()
                return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": f"HTTP_{response.status_code}"})
            if response.headers.get("content-encoding", "").lower() not in {"", "identity"}:
                response.close()
                return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": "COMPRESSED_RESPONSE_FORBIDDEN"})
            body = bytearray()
            try:
                for chunk in response.iter_raw():
                    if len(body) + len(chunk) > self.max_bytes:
                        response.close()
                        return extract_pdf_text(bytes(body), source_url=current, content_type="application/pdf", max_bytes=len(body) - 1).model_copy(update={"error_code": "PDF_TOO_LARGE"})
                    body.extend(chunk)
            finally:
                response.close()
            return extract_pdf_text(bytes(body), source_url=current, content_type=response.headers.get("content-type", ""), max_bytes=self.max_bytes)
        return extract_pdf_text(empty, source_url=current, content_type="", max_bytes=self.max_bytes).model_copy(update={"error_code": "TOO_MANY_REDIRECTS"})
