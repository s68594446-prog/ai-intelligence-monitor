import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import DeliveryStatus, PipelineState, RunStatus
from app.models import DeliveryFeedback, Event, RunAudit, Signal, StateTransitionAudit
from app.pipeline.state_machine import is_allowed, transition


REQUIRED_ARTIFACTS = [
    "01-collected.json",
    "02-triage.json",
    "03-analysis.json",
    "04-red-team.json",
    "05-brief.html",
]


class OpsChecker:
    def __init__(self, session: Session, runs_dir: Path):
        self.session = session
        self.runs_dir = Path(runs_dir)

    def check(self, run_id: str) -> dict:
        run = self.session.scalar(select(RunAudit).where(RunAudit.run_id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        run_dir = self.runs_dir / run_id
        errors: list[str] = []
        for name in REQUIRED_ARTIFACTS:
            if not (run_dir / name).is_file():
                errors.append(f"missing artifact: {name}")
        if errors:
            return self._finish(run, errors)

        collected = json.loads((run_dir / "01-collected.json").read_text(encoding="utf-8"))
        triage = json.loads((run_dir / "02-triage.json").read_text(encoding="utf-8"))
        analysis = json.loads((run_dir / "03-analysis.json").read_text(encoding="utf-8"))
        red_team = json.loads((run_dir / "04-red-team.json").read_text(encoding="utf-8"))
        brief = (run_dir / "05-brief.html").read_text(encoding="utf-8")
        docs = {item["fixture_id"]: item for item in collected["documents"]}
        reviews = {item["signal_id"]: item for item in red_team["reviews"]}

        if run.run_kind == "manual":
            snapshot = run.manual_input_snapshot or {}
            if len(docs) != 1:
                errors.append("manual run must preserve exactly one input document")
            else:
                manual_doc = next(iter(docs.values()))
                if manual_doc.get("raw_text") != snapshot.get("raw_text"):
                    errors.append("manual raw_text was not preserved exactly")
                if manual_doc.get("source", {}).get("trust_tier") not in {"T1", "T2", "T3"}:
                    errors.append("manual source tier is invalid")
                if manual_doc.get("source", {}).get("name") != snapshot.get("source_name"):
                    errors.append("manual source name was not preserved")
                if (manual_doc.get("url") or "") != (snapshot.get("url") or ""):
                    errors.append("manual URL was not preserved")
                try:
                    collected_date = datetime.fromisoformat(
                        manual_doc["published_at"].replace("Z", "+00:00")
                    )
                    snapshot_date = datetime.fromisoformat(
                        snapshot["published_at"].replace("Z", "+00:00")
                    )
                    if collected_date != snapshot_date:
                        errors.append("manual published_at was not preserved")
                except (KeyError, TypeError, ValueError):
                    errors.append("manual published_at is missing or invalid")
            if not run.llm_schema_validated:
                errors.append("LLM output was not schema validated")
            if not (
                run.analysis_prompt_version
                and run.red_team_prompt_version
                and run.model_name
                and run.adapter_versions
            ):
                errors.append("manual prompt/model/adapter versions are incomplete")
            for output in analysis.get("adapter_outputs", []):
                result = output.get("analysis", {})
                if not result.get("schema_validated"):
                    errors.append("analysis adapter output is not schema validated")
                contract = result.get("research_contract")
                if contract is None:
                    errors.append("manual analysis contract is missing")
                    continue
                for claim in contract.get("extracted_claims", []):
                    evidence = claim.get("evidence_references", [])
                    if not evidence:
                        errors.append("manual extracted claim has no evidence reference")
                    for reference in evidence:
                        source_doc = docs.get(reference.get("reference_id"))
                        if source_doc is None:
                            errors.append("manual evidence references an unknown document")
                        elif reference.get("quote") not in source_doc.get("raw_text", ""):
                            errors.append("manual evidence quote is absent from raw_text")

        event_keys = [item["event_key"] for item in triage["events"]]
        if len(event_keys) != len(set(event_keys)):
            errors.append("duplicate Event records detected")
        if not run.coverage_version or not analysis.get("coverage_version"):
            errors.append("coverage version not recorded")
        if not run.prompt_version or not run.adapter_versions or not analysis.get("adapter_versions"):
            errors.append("prompt/adapter version not recorded")

        expected_published: list[int] = []
        for item in analysis["signals"]:
            level = item["signal_level"]
            facts = item["analysis"]["facts"]
            if level in {"S2", "S3"}:
                for fact in facts:
                    if not fact["source_refs"] and not fact.get("missing_marker"):
                        errors.append(f"S2/S3 fact has no source: signal {item['id']}")
            for fact in facts:
                unknown_refs = sorted(set(fact["source_refs"]) - docs.keys())
                if unknown_refs:
                    errors.append(
                        f"fact references unknown source documents {unknown_refs}: signal {item['id']}"
                    )
                contains_number = bool(
                    re.search(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?%?", fact["statement"])
                    or (
                        fact.get("value")
                        and re.search(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?%?", fact["value"])
                    )
                )
                if (fact["value_kind"] == "NUMBER" or contains_number) and not fact["source_refs"] and not fact.get("missing_marker"):
                    errors.append(f"number has no source or missing marker: signal {item['id']}")
            if level in {"S2", "S3"} and item["id"] not in reviews:
                errors.append(f"S2/S3 did not pass Red Team: signal {item['id']}")
            if level == "S3":
                tiers = [docs[ref]["source"]["trust_tier"] for fact in facts for ref in fact["source_refs"] if ref in docs]
                if tiers and set(tiers) == {"T3"}:
                    errors.append(f"T3-only evidence triggered S3: signal {item['id']}")
            if item.get("adjudication") == "SEND_ALERT":
                tiers = [source["tier"] for source in item.get("sources", [])]
                if tiers and set(tiers) == {"T3"}:
                    errors.append(f"T3-only evidence triggered SEND_ALERT: signal {item['id']}")
            if item["pipeline_state"] in {"CONFLICTED", "INSUFFICIENT_EVIDENCE"} and item["published"]:
                errors.append(f"held content marked publishable: signal {item['id']}")
            if item["published"]:
                expected_published.append(item["id"])

        transitions = self.session.scalars(
            select(StateTransitionAudit).where(StateTransitionAudit.run_id == run_id)
        ).all()
        for audit in transitions:
            if not is_allowed(audit.from_state, audit.to_state):
                errors.append(
                    f"illegal state transition audited: {audit.from_state.value}->{audit.to_state.value}"
                )

        out_of_scope_events = self.session.scalars(
            select(Event).where(
                Event.run_id == run_id,
                Event.pipeline_state == PipelineState.OUT_OF_SCOPE,
            )
        ).all()
        if out_of_scope_events and expected_published:
            errors.append("OUT_OF_SCOPE content was published")

        marker = re.search(r"published-signal-ids:(\[[^\n]*\])", brief)
        brief_ids = json.loads(marker.group(1)) if marker else None
        if brief_ids is None:
            errors.append("brief publication marker missing")
        elif sorted(brief_ids) != sorted(expected_published):
            errors.append("brief is inconsistent with adjudication")
        material_expected = any(
            item["published"] and item["signal_level"] in {"S2", "S3"}
            for item in analysis["signals"]
        )
        expected_summary = "MATERIAL_CHANGE" if material_expected else "NO_MATERIAL_CHANGE"
        if f"brief-summary:{expected_summary}" not in brief:
            errors.append("brief summary is inconsistent with adjudication")
        return self._finish(run, errors)

    def _finish(self, run: RunAudit, errors: list[str]) -> dict:
        passed = not errors
        result = {"passed": passed, "errors": errors, "checks": 12 if run.run_kind == "manual" else 11}
        run.ops_check = result
        if passed:
            run.status = RunStatus.PASSED
            rendered = self.session.scalars(
                select(Signal).where(
                    Signal.run_id == run.run_id,
                    Signal.pipeline_state == PipelineState.RENDERED,
                )
            ).all()
            for signal in rendered:
                transition(
                    self.session,
                    run_id=run.run_id,
                    entity=signal,
                    to_state=PipelineState.OPS_CHECKED,
                    reason="deterministic Ops Check passed",
                )
        else:
            run.status = RunStatus.FAILED_OPS_CHECK
            run.errors = list(run.errors or []) + errors
            deliveries = self.session.scalars(
                select(DeliveryFeedback).where(DeliveryFeedback.run_id == run.run_id)
            ).all()
            for delivery in deliveries:
                delivery.delivery_status = DeliveryStatus.BLOCKED
                delivery.feedback = "Blocked by deterministic Ops Check"
        brief_path = self.runs_dir / run.run_id / "05-brief.html"
        if brief_path.is_file():
            brief = brief_path.read_text(encoding="utf-8")
            status = "PASSED" if passed else "FAILED"
            brief = re.sub(r"ops-check-status:[A-Z]+", f"ops-check-status:{status}", brief)
            brief = re.sub(
                r"(<code data-ops-check-status>)[A-Z]+(</code>)",
                rf"\1{status}\2",
                brief,
            )
            brief_path.write_text(brief, encoding="utf-8")
        self.session.commit()
        return result
