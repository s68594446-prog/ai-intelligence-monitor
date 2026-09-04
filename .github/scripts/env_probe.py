"""Secret-safe Gemini connectivity/authentication/model probe.

The API key is read only from ``GEMINI_API_KEY`` and is sent only in the
``x-goog-api-key`` header.  The report contains no request headers or provider
response body and therefore cannot disclose the credential.
"""

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


TIMEOUT = 20
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash"
OFFICIAL_HOST = "generativelanguage.googleapis.com"


def _endpoint(base_url: str, model: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OFFICIAL_HOST
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GEMINI_BASE_URL must be an HTTPS Gemini API URL")
    if not model or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in model):
        raise ValueError("GEMINI_ANALYSIS_MODEL is invalid")
    return f"{base_url.rstrip('/')}/models/{urllib.parse.quote(model, safe='-._')}"


def _probe_model(*, key: str, model: str, base_url: str) -> dict:
    result = {
        "target_model": model,
        "reachable": False,
        "auth": "NOT_CHECKED",
        "model_exists": False,
    }
    try:
        url = _endpoint(base_url, model)
    except ValueError as exc:
        return {**result, "error": type(exc).__name__}

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ai-intelligence-monitor-env-probe/2.0",
            "x-goog-api-key": key,
        },
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, ssl.SSLError, socket.timeout, OSError) as exc:
        return {
            **result,
            "elapsed_ms": round((time.monotonic() - start) * 1000),
            "error": type(exc).__name__,
        }

    auth = "PASS" if status in {200, 404} else (
        "FAILED" if status in {401, 403} else "UNKNOWN"
    )
    return {
        **result,
        "reachable": True,
        "auth": auth,
        "model_exists": status == 200,
        "status": status,
        "elapsed_ms": round((time.monotonic() - start) * 1000),
    }


def probe_gemini() -> dict:
    key = os.environ.get("GEMINI_API_KEY", "")
    primary = os.environ.get("GEMINI_ANALYSIS_MODEL", DEFAULT_MODEL)
    fallback = os.environ.get("GEMINI_FALLBACK_MODEL", "").strip()
    fallback_enabled = os.environ.get(
        "GEMINI_QUOTA_FALLBACK_ENABLED", "false"
    ).strip().casefold() in {"1", "true", "yes", "on"}
    base_url = os.environ.get("GEMINI_BASE_URL", DEFAULT_BASE_URL)
    targets = [primary]
    if fallback_enabled:
        if not fallback:
            return {
                "target_models": targets,
                "reachable": False,
                "auth": "NOT_CHECKED",
                "model_exists": False,
                "error": "FALLBACK_MODEL_MISSING",
            }
        targets.append(fallback)
    if not key:
        return {
            "target_models": targets,
            "reachable": False,
            "auth": "NOT_CHECKED",
            "model_exists": False,
            "error": "SKIPPED_NO_SECRET",
        }
    models = {
        model: _probe_model(key=key, model=model, base_url=base_url)
        for model in targets
    }
    return {
        "target_model": primary,
        "target_models": targets,
        "fallback_enabled": fallback_enabled,
        "reachable": all(item["reachable"] for item in models.values()),
        "auth": (
            "PASS"
            if all(item["auth"] == "PASS" for item in models.values())
            else "FAILED"
            if any(item["auth"] == "FAILED" for item in models.values())
            else "UNKNOWN"
        ),
        "model_exists": all(item["model_exists"] for item in models.values()),
        "models": models,
    }


def main() -> None:
    gemini = probe_gemini()
    passed = bool(
        gemini["reachable"]
        and gemini["auth"] == "PASS"
        and gemini["model_exists"]
    )
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gemini": gemini,
        "passed": passed,
    }
    with open("env-probe-report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
