from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS_DIR = REPO_ROOT / ".github/workflows"


def load_steps(name: str) -> list[dict]:
    workflow = yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))
    return workflow["jobs"]["digest"]["steps"]


def test_daily_workflow_gates_before_publish_and_delivery():
    steps = load_steps("daily-digest.yml")
    names = [step.get("name") for step in steps]

    upload = names.index("Upload full run artifacts (audit trail)")
    gate = names.index("Verify digest Ops Gate")
    prepare = names.index("Prepare audited digest for delivery")
    history = names.index("Update published source history (URL and content hash only)")
    email = names.index("Email the digest (Gate-checked; sends only if Ops passed)")
    commit = names.index("Commit delivered digest and email ledger")

    assert upload < gate < prepare < history < email < commit

    assert steps[gate]["run"].strip() == "python -m app.scripts.verify_daily_digest_ops"
    assert "git commit" not in steps[prepare]["run"]
    assert "steps.guard.outputs.send_email" in steps[email]["run"]
    assert 'python -m app.scripts.send_digest_email --day "$DIGEST_DAY" --send' in steps[email]["run"]
    assert 'python -m app.scripts.send_digest_email --day "$DIGEST_DAY"\n' in steps[email]["run"]
    assert "||" not in steps[email]["run"]
    assert "steps.guard.outputs.send_email == 'true'" in steps[commit]["if"]
    assert "digests/.source-history.jsonl" in steps[commit]["run"]
    assert "git add" in steps[commit]["run"]
    assert "git push origin main" in steps[commit]["run"]


def test_workflows_use_node24_action_majors():
    for name in ("daily-digest.yml", "email-digest.yml", "env-probe.yml"):
        text = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
        assert "actions/checkout@v4" not in text
        assert "actions/setup-python@v5" not in text
        assert "actions/upload-artifact@v4" not in text

    daily = (WORKFLOWS_DIR / "daily-digest.yml").read_text(encoding="utf-8")
    assert "actions/checkout@v5" in daily
    assert "actions/setup-python@v6" in daily
    assert "actions/upload-artifact@v6" in daily


def test_daily_workflow_uses_gemini_without_deepseek_fallback():
    steps = load_steps("daily-digest.yml")
    analysis = next(step for step in steps if step.get("name", "").startswith("Run daily digest"))

    assert "Gemini" in analysis["name"]
    assert analysis["env"]["GEMINI_API_KEY"] == "${{ secrets.GEMINI_API_KEY }}"
    assert "GEMINI_ANALYSIS_MODEL" in analysis["env"]
    assert "GEMINI_FALLBACK_MODEL" in analysis["env"]
    assert "GEMINI_QUOTA_FALLBACK_ENABLED" in analysis["env"]
    assert "GEMINI_BASE_URL" in analysis["env"]
    assert "DEEPSEEK_API_KEY" not in analysis["env"]
    assert analysis["run"].strip().startswith("python -m app.scripts.daily_global_ai")


def test_daily_workflow_can_cover_all_fetches_and_has_bounded_retry_reserve():
    steps = load_steps("daily-digest.yml")
    analysis = next(step for step in steps if step.get("name", "").startswith("Run daily digest"))
    command = analysis["run"].split()
    fetch_cap = int(command[command.index("--max-fetch") + 1])
    request_cap = int(command[command.index("--max-provider-requests") + 1])
    assert 0 < fetch_cap < request_cap <= 80


def test_env_probe_only_receives_gemini_configuration():
    workflow = yaml.safe_load((WORKFLOWS_DIR / "env-probe.yml").read_text(encoding="utf-8"))
    step = next(
        step
        for step in workflow["jobs"]["probe"]["steps"]
        if step.get("name") == "Run environment probe"
    )

    assert set(step["env"]) == {
        "GEMINI_API_KEY",
        "GEMINI_ANALYSIS_MODEL",
        "GEMINI_FALLBACK_MODEL",
        "GEMINI_QUOTA_FALLBACK_ENABLED",
        "GEMINI_BASE_URL",
    }
    assert step["env"]["GEMINI_API_KEY"] == "${{ secrets.GEMINI_API_KEY }}"


def test_smtp_preflight_is_secret_scoped_and_analysis_gets_only_boolean():
    steps = load_steps("daily-digest.yml")
    preflight = next(step for step in steps if step.get("id") == "smtp-config")
    analysis = next(step for step in steps if step.get("name", "").startswith("Run daily digest"))

    assert preflight["run"] == "python -m app.scripts.validate_smtp_config"
    assert "SMTP_PASSWORD" in preflight["env"]
    assert "SMTP_PASSWORD" not in analysis["env"]
    assert analysis["env"]["SMTP_CONFIGURATION_PREVALIDATED"] == (
        "${{ steps.smtp-config.outputs.valid }}"
    )


def test_manual_daily_workflow_defaults_to_full_no_send_dry_run():
    text = (WORKFLOWS_DIR / "daily-digest.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]
    steps = load_steps("daily-digest.yml")
    guard = next(step for step in steps if step.get("id") == "guard")

    assert dispatch["send_email"]["default"] is False
    assert "github.event_name == 'schedule'" in guard["run"]
    assert 'if [ "$SEND_EMAIL" != "true" ]' in guard["run"]


def test_senders_share_a_lock_and_manual_sender_is_date_scoped():
    daily = yaml.safe_load((WORKFLOWS_DIR / "daily-digest.yml").read_text())
    manual = yaml.safe_load((WORKFLOWS_DIR / "email-digest.yml").read_text())
    assert daily["concurrency"] == manual["concurrency"]
    step = next(s for s in manual["jobs"]["email"]["steps"] if s.get("name", "").startswith("Send (or Gate-check)"))
    assert step["env"]["DIGEST_DAY"] == "${{ inputs.day }}"
    assert '--day "$DAY"' in step["run"]
    assert "TZ=Asia/Shanghai" in step["run"]


def test_failed_send_retains_only_ledger_without_publishing_failed_digest():
    for name, job in (("daily-digest.yml", "digest"), ("email-digest.yml", "email")):
        workflow = yaml.safe_load((WORKFLOWS_DIR / name).read_text())
        steps = workflow["jobs"][job]["steps"]
        step = next(s for s in steps if s.get("name") == "Preserve delivery attempt ledger on failure")
        assert "failure()" in step["if"]
        assert "git add digests/.email-log.jsonl\n" in step["run"]
        assert ".source-history.jsonl" not in step["run"]
        assert "--autostash" in step["run"]
        assert "|| true" not in step["run"]


def test_backend_ci_runs_offline_tests_on_push_and_pull_request():
    text = (WORKFLOWS_DIR / "backend-ci.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    steps = workflow["jobs"]["test"]["steps"]
    test_step = next(step for step in steps if step.get("name") == "Run offline backend tests")

    assert "push:" in text
    assert "pull_request:" in text
    assert workflow["permissions"] == {"contents": "read"}
    assert test_step["run"] == "python -m pytest -q"
    assert test_step["env"] == {
        "GEMINI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "OPENAI_API_KEY": "",
    }


def test_scheduled_sender_pins_one_date_and_missing_render_is_fatal():
    workflow = yaml.safe_load((WORKFLOWS_DIR / "daily-digest.yml").read_text())
    steps = {s.get("name"): s for s in workflow["jobs"]["digest"]["steps"]}
    for name in ("Prepare audited digest for delivery",
                 "Email the digest (Gate-checked; sends only if Ops passed)",
                 "Commit delivered digest and email ledger"):
        assert steps[name]["env"]["DIGEST_DAY"] == "${{ steps.guard.outputs.day }}"
        assert "date +%F" not in steps[name]["run"]
    prepare = steps["Prepare audited digest for delivery"]["run"]
    missing = prepare.split('if [ -z "$DIGEST" ]; then', 1)[1].split("fi", 1)[0]
    assert "exit 1" in missing
    assert "exit 0" not in missing
