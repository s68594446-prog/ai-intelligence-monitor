"""GitHub Actions environment probe for the daily AI digest migration.

Three feasibility tests, report-only, stdlib only:
1. Domestic target sites: DNS + HTTPS GET (status, latency, geo-blocks).
2. DeepSeek API: reachability + authentication (key from GitHub Secrets).
3. Domestic AI media RSS feeds: fetch + minimal parse.

The DeepSeek key is read from the environment and never printed; GitHub
masks secret values in logs. The report is written to env-probe-report.json.
"""
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request

TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; ai-intelligence-monitor-env-probe/1.0)"

DOMESTIC_SITES = [
    "https://www.sse.com.cn/",
    "https://www.cninfo.com.cn/",
    "https://www.jiqizhixin.com/",
    "https://bigmodel.cn/",
]
RSS_FEEDS = [
    ("jiqizhixin", "https://www.jiqizhixin.com/rss"),
    ("qbitai", "https://www.qbitai.com/feed"),
    ("36kr", "https://36kr.com/feed"),
]


def http_get(url: str, headers: dict | None = None, max_bytes: int = 200_000) -> dict:
    start = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(max_bytes)
            return {
                "ok": True, "status": response.status,
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "bytes": len(body), "content_type": response.headers.get("content-type", ""),
                "body_head": body[:400].decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "elapsed_ms": round((time.monotonic() - start) * 1000),
                "error": f"HTTP_{exc.code}", "body_head": (exc.read(400) or b"").decode("utf-8", "replace")}
    except (urllib.error.URLError, ssl.SSLError, socket.timeout, OSError) as exc:
        return {"ok": False, "status": None, "elapsed_ms": round((time.monotonic() - start) * 1000),
                "error": f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"}


def probe_domestic() -> list[dict]:
    results = []
    for url in DOMESTIC_SITES:
        host = url.split("/")[2]
        entry = {"url": url}
        start = time.monotonic()
        try:
            addrs = sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
            entry["dns"] = {"ok": True, "addresses": addrs, "elapsed_ms": round((time.monotonic() - start) * 1000)}
        except OSError as exc:
            entry["dns"] = {"ok": False, "error": str(exc), "elapsed_ms": round((time.monotonic() - start) * 1000)}
            entry["get"] = {"ok": False, "error": "SKIPPED_DNS_FAILED"}
            results.append(entry)
            continue
        entry["get"] = http_get(url)
        entry["get"].pop("body_head", None)
        results.append(entry)
    return results


def probe_deepseek() -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return {"ok": False, "error": "SKIPPED_NO_SECRET", "note": "DEEPSEEK_API_KEY secret is not configured"}
    result = http_get("https://api.deepseek.com/models", headers={"Authorization": f"Bearer {key}"})
    result.pop("body_head", None)
    if result.get("status") == 200:
        result["auth"] = "PASS"
    elif result.get("status") in {401, 403}:
        result["auth"] = "REACHABLE_BUT_AUTH_FAILED"
    else:
        result["auth"] = "UNREACHABLE_OR_ERROR"
    return result


def probe_rss() -> list[dict]:
    results = []
    for name, url in RSS_FEEDS:
        result = http_get(url, max_bytes=500_000)
        head = result.pop("body_head", "")
        looks_like_feed = "<rss" in head or "<feed" in head or "<?xml" in head
        results.append({
            "name": name, "url": url, **result,
            "looks_like_feed": bool(result.get("ok")) and looks_like_feed,
        })
    return results


def main():
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runner": {"ip_note": "GitHub-hosted runner, egress region visible to targets"},
        "domestic_sites": probe_domestic(),
        "deepseek": probe_deepseek(),
        "rss_feeds": probe_rss(),
    }
    verdicts = {
        "domestic_sites": all(item.get("get", {}).get("status") == 200 for item in report["domestic_sites"]),
        "domestic_sites_partial": [item["url"] for item in report["domestic_sites"] if item.get("get", {}).get("status") == 200],
        "deepseek": report["deepseek"].get("auth") == "PASS",
        "rss": [item["name"] for item in report["rss_feeds"] if item.get("looks_like_feed")],
    }
    report["verdicts"] = verdicts
    with open("env-probe-report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n=== SUMMARY ===")
    print(f"1) domestic sites 200 OK: {verdicts['domestic_sites_partial']} (all pass: {verdicts['domestic_sites']})")
    print(f"2) deepseek: {report['deepseek'].get('auth', 'UNKNOWN')}")
    print(f"3) rss feeds usable: {verdicts['rss']}")


if __name__ == "__main__":
    main()
