import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = REPO_ROOT / ".github/scripts/env_probe.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("gemini_env_probe", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_env_probe_sends_key_only_in_header(monkeypatch):
    probe = load_probe()
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["key"] = request.get_header("X-goog-api-key")
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("GEMINI_API_KEY", "probe-secret")
    monkeypatch.setenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash")
    monkeypatch.delenv("GEMINI_QUOTA_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("GEMINI_SERVER_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.setattr(probe.urllib.request, "urlopen", urlopen)

    result = probe.probe_gemini()

    assert result["reachable"]
    assert result["auth"] == "PASS"
    assert result["model_exists"]
    assert seen["key"] == "probe-secret"
    assert "probe-secret" not in seen["url"]
    assert seen["url"].endswith("/models/gemini-3.5-flash")


def test_env_probe_missing_key_is_explicit_and_network_free(monkeypatch):
    probe = load_probe()
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_QUOTA_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("GEMINI_SERVER_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.setattr(
        probe.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )

    result = probe.probe_gemini()

    assert result["error"] == "SKIPPED_NO_SECRET"
    assert not result["reachable"]
    assert result["auth"] == "NOT_CHECKED"


def test_env_probe_validates_primary_and_explicit_fallback(monkeypatch):
    probe = load_probe()
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("X-goog-api-key")))
        return FakeResponse()

    monkeypatch.setenv("GEMINI_API_KEY", "probe-secret")
    monkeypatch.setenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_QUOTA_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("GEMINI_SERVER_FALLBACK_ENABLED", "true")
    monkeypatch.setattr(probe.urllib.request, "urlopen", urlopen)

    result = probe.probe_gemini()

    assert result["model_exists"] is True
    assert result["server_fallback_enabled"] is True
    assert result["target_models"] == [
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ]
    assert [url.rsplit("/", 1)[-1] for url, _ in seen] == [
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ]
    assert all(key == "probe-secret" for _, key in seen)
    assert all("probe-secret" not in url for url, _ in seen)
