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
    monkeypatch.setattr(
        probe.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )

    result = probe.probe_gemini()

    assert result["error"] == "SKIPPED_NO_SECRET"
    assert not result["reachable"]
    assert result["auth"] == "NOT_CHECKED"
