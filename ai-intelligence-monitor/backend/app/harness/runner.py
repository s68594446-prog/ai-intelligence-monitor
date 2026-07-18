import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    Adjudication,
    DeliveryStatus,
    PipelineState,
    RedTeamAction,
    RunStatus,
    SignalLevel,
    TrustTier,
)
from app.coverage.service import ensure_coverage, ensure_coverage_config
from app.delivery.render import render_brief
from app.harness.ops import OpsChecker
from app.models import DeliveryFeedback, Document, Event, RunAudit, Signal, Source
from app.pipeline.adapters import AnalysisAdapter, CollectorAdapter, RedTeamAdapter
from app.pipeline.state_machine import transition


def _json_dump(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


class Harness:
    def __init__(
        self,
        *,
        session: Session,
        runs_dir: Path,
        coverage_path: Path,
        collector: CollectorAdapter,
        analysis: AnalysisAdapter,
        red_team: RedTeamAdapter,
        prompt_version: str = "phase0-prompt-v1",
        analysis_prompt_version: str | None = None,
        red_team_prompt_version: str | None = None,
        model_name: str | None = None,
        red_team_model_name: str | None = None,
    ):
        self.session = session
        self.runs_dir = Path(runs_dir)
        self.coverage_path = Path(coverage_path)
        self.collector = collector
        self.analysis = analysis
        self.red_team = red_team
        self.prompt_version = prompt_version
        self.analysis_prompt_version = analysis_prompt_version
        self.red_team_prompt_version = red_team_prompt_version
        self.model_name = model_name
        self.red_team_model_name = red_team_model_name

    def run(
        self,
        scenario: str,
        *,
        replay_of: str | None = None,
        run_ops: bool = True,
        coverage_snapshot: dict | None = None,
        run_kind: str = "fixture",
        replay_mode: str = "DETERMINISTIC",
        manual_input_snapshot: dict | None = None,
    ) -> RunAudit:
        topic, thesis = (
            ensure_coverage_config(self.session, coverage_snapshot)
            if coverage_snapshot is not None
            else ensure_coverage(self.session, self.coverage_path)
        )
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10]
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        audit = RunAudit(
            run_id=run_id,
            replay_of=replay_of,
            scenario=scenario,
            status=RunStatus.RUNNING,
            coverage_id=topic.coverage_id,
            coverage_version=topic.version,
            coverage_snapshot=topic.config_snapshot,
            thesis_version=thesis.version,
            prompt_version=self.prompt_version,
            model_adapter=self.analysis.name,
            adapter_versions={
                "collector": self.collector.version,
                "analysis": self.analysis.version,
                "red_team": self.red_team.version,
            },
            errors=[],
            artifact_paths=[],
            run_kind=run_kind,
            replay_mode=replay_mode,
            manual_input_snapshot=manual_input_snapshot,
            analysis_prompt_version=self.analysis_prompt_version,
            red_team_prompt_version=self.red_team_prompt_version,
            model_name=self.model_name,
            red_team_model_name=self.red_team_model_name,
            llm_schema_validated=False,
        )
        self.session.add(audit)
        self.session.flush()
        self.session.commit()

        try:
            batch = self.collector.collect(scenario)
            collected_payload = batch.model_dump(mode="json")
            _json_dump(run_dir / "01-collected.json", collected_payload)
            audit.artifact_paths = list(audit.artifact_paths) + [str(run_dir / "01-collected.json")]

            fixture_to_document: dict[str, Document] = {}
            for item in batch.documents:
                persisted_id = batch.persisted_document_ids.get(item.fixture_id)
                if persisted_id is not None:
                    document = self.session.get(Document, persisted_id)
                    if document is None:
                        raise ValueError(f"persisted manual document not found: {persisted_id}")
                    if document.raw_text != item.raw_text:
                        raise ValueError("manual raw_text changed between ingestion and analysis")
                    document.run_id = run_id
                    document.event_key_hint = item.event_key
                    document.fixture_payload = item.model_dump(mode="json")
                    document.optional_notes = item.optional_notes
                    self.session.flush()
                else:
                    source = Source(
                        name=item.source.name,
                        source_type=item.source.source_type,
                        trust_tier=item.source.trust_tier,
                        url=str(item.source.url),
                        active=True,
                    )
                    self.session.add(source)
                    self.session.flush()
                    document = Document(
                        run_id=run_id,
                        source_id=source.id,
                        title=item.title,
                        url=str(item.url),
                        published_at=item.published_at,
                        fetched_at=datetime.now(timezone.utc),
                        language=item.language,
                        raw_text=item.raw_text,
                        content_hash=hashlib.sha256(item.raw_text.encode()).hexdigest(),
                        fact_type=item.fact_type,
                        verification_status=item.verification_status,
                        pipeline_state=PipelineState.COLLECTED,
                        event_key_hint=item.event_key,
                        fixture_payload=item.model_dump(mode="json"),
                        optional_notes=item.optional_notes,
                    )
                    self.session.add(document)
                    self.session.flush()
                fixture_to_document[item.fixture_id] = document
                transition(
                    self.session,
                    run_id=run_id,
                    entity=document,
                    to_state=PipelineState.NORMALIZED,
                    reason="fixture normalized into the document contract",
                )

            events: list[Event] = []
            by_event: dict[str, list] = {}
            for item in batch.documents:
                by_event.setdefault(item.event_key, []).append(item)
            for event_key, items in by_event.items():
                representative = fixture_to_document[items[0].fixture_id]
                transition(
                    self.session,
                    run_id=run_id,
                    entity=representative,
                    to_state=PipelineState.DEDUPED,
                    reason="selected as canonical event evidence",
                )
                for duplicate_item in items[1:]:
                    transition(
                        self.session,
                        run_id=run_id,
                        entity=fixture_to_document[duplicate_item.fixture_id],
                        to_state=PipelineState.DUPLICATE,
                        reason=f"merged into event {event_key}",
                    )
                event = Event(
                    run_id=run_id,
                    canonical_title=items[0].title,
                    event_key=event_key,
                    summary=items[0].event_summary,
                    document_ids=[fixture_to_document[item.fixture_id].id for item in items],
                    duplicate_count=len(items) - 1,
                    pipeline_state=PipelineState.DEDUPED,
                )
                self.session.add(event)
                self.session.flush()
                events.append(event)

            triage_payload = {
                "run_id": run_id,
                "events": [
                    {
                        "id": event.id,
                        "event_key": event.event_key,
                        "canonical_title": event.canonical_title,
                        "document_ids": event.document_ids,
                        "duplicate_count": event.duplicate_count,
                    }
                    for event in events
                ],
            }
            _json_dump(run_dir / "02-triage.json", triage_payload)
            audit.artifact_paths = list(audit.artifact_paths) + [str(run_dir / "02-triage.json")]

            signals: list[Signal] = []
            adapter_outputs: list[dict] = []
            schema_validation_results: list[bool] = []
            for event in events:
                transition(
                    self.session,
                    run_id=run_id,
                    entity=event,
                    to_state=PipelineState.TRIAGED,
                    reason="event mapped against versioned coverage",
                )
                event_items = by_event[event.event_key]
                representative_item = event_items[0]
                result = self.analysis.analyze(representative_item)
                schema_validation_results.append(result.schema_validated)
                adapter_outputs.append(
                    {
                        "event_key": event.event_key,
                        "analysis": result.model_dump(mode="json"),
                    }
                )
                if result.out_of_scope:
                    transition(
                        self.session,
                        run_id=run_id,
                        entity=event,
                        to_state=PipelineState.OUT_OF_SCOPE,
                        reason="cannot map to a monitored variable or dependency sentinel",
                    )
                    continue

                tiers = [item.source.trust_tier for item in event_items]
                final_level = result.signal_level
                gate_state = None
                if final_level == SignalLevel.S3 and set(tiers) == {TrustTier.T3}:
                    final_level = SignalLevel.S1
                    gate_state = PipelineState.UNVERIFIED
                elif final_level == SignalLevel.S3 and not (
                    TrustTier.T1 in tiers or sum(tier == TrustTier.T2 for tier in tiers) >= 2
                ):
                    final_level = SignalLevel.S2
                    gate_state = PipelineState.INSUFFICIENT_EVIDENCE

                signal = Signal(
                    run_id=run_id,
                    event_id=event.id,
                    coverage_topic_id=topic.id,
                    affected_variable=result.affected_variable,
                    thesis_version=thesis.version,
                    direction=result.direction,
                    novelty=result.novelty,
                    materiality=result.materiality,
                    source_quality=result.source_quality,
                    signal_level=final_level,
                    analysis_payload=result.model_dump(mode="json"),
                    red_team_payload=None,
                    adjudication=None,
                    pipeline_state=PipelineState.ANALYZED,
                )
                self.session.add(signal)
                self.session.flush()
                signals.append(signal)
                transition(
                    self.session,
                    run_id=run_id,
                    entity=event,
                    to_state=PipelineState.ANALYZED,
                    reason="deterministic analysis contract accepted",
                )

                needs_red_team = (
                    result.signal_level in {SignalLevel.S2, SignalLevel.S3}
                    if run_kind == "manual"
                    else final_level in {SignalLevel.S2, SignalLevel.S3}
                ) or (
                    representative_item.red_team is not None
                    and representative_item.red_team.action == RedTeamAction.RESURRECT
                )
                if needs_red_team:
                    review = self.red_team.review(representative_item, result)
                    schema_validation_results.append(review.schema_validated)
                    signal.red_team_payload = review.model_dump(mode="json")
                    signal.signal_level = review.resulting_level
                    if signal.signal_level == SignalLevel.S3 and set(tiers) == {TrustTier.T3}:
                        signal.signal_level = SignalLevel.S1
                        gate_state = PipelineState.UNVERIFIED
                    elif signal.signal_level == SignalLevel.S3 and not (
                        TrustTier.T1 in tiers or sum(tier == TrustTier.T2 for tier in tiers) >= 2
                    ):
                        signal.signal_level = SignalLevel.S2
                        gate_state = PipelineState.INSUFFICIENT_EVIDENCE
                    transition(
                        self.session,
                        run_id=run_id,
                        entity=signal,
                        to_state=PipelineState.RED_TEAMED,
                        reason=f"Red Team action {review.action.value}",
                    )
                    transition(
                        self.session,
                        run_id=run_id,
                        entity=event,
                        to_state=PipelineState.RED_TEAMED,
                        reason=f"Red Team action {review.action.value}",
                    )

                if any(item.conflicted for item in event_items):
                    signal.adjudication = Adjudication.REQUEST_HUMAN_REVIEW
                    transition(
                        self.session,
                        run_id=run_id,
                        entity=signal,
                        to_state=PipelineState.CONFLICTED,
                        reason="reliable sources conflict; automatic publication prohibited",
                    )
                    transition(
                        self.session,
                        run_id=run_id,
                        entity=event,
                        to_state=PipelineState.CONFLICTED,
                        reason="reliable sources conflict; automatic publication prohibited",
                    )
                    continue
                if gate_state is not None:
                    signal.adjudication = Adjudication.REQUEST_HUMAN_REVIEW
                    transition(
                        self.session,
                        run_id=run_id,
                        entity=signal,
                        to_state=gate_state,
                        reason="Coverage Gate rejected evidence for the requested grade",
                    )
                    if event.pipeline_state not in {PipelineState.UNVERIFIED, PipelineState.INSUFFICIENT_EVIDENCE}:
                        transition(
                            self.session,
                            run_id=run_id,
                            entity=event,
                            to_state=gate_state,
                            reason="Coverage Gate rejected evidence for the requested grade",
                        )
                    continue

                adjudication = result.adjudication
                if signal.red_team_payload:
                    action = RedTeamAction(signal.red_team_payload["action"])
                    if action in {RedTeamAction.DOWNGRADE, RedTeamAction.RESURRECT}:
                        adjudication = Adjudication.INCLUDE_WEEKLY
                signal.adjudication = adjudication
                transition(
                    self.session,
                    run_id=run_id,
                    entity=signal,
                    to_state=PipelineState.ADJUDICATED,
                    reason=f"final adjudication {adjudication.value}",
                )
                transition(
                    self.session,
                    run_id=run_id,
                    entity=event,
                    to_state=PipelineState.ADJUDICATED,
                    reason=f"final adjudication {adjudication.value}",
                )

            signal_payloads = self._signal_payloads(run_id, signals, fixture_to_document)
            analysis_payload = {
                "run_id": run_id,
                "coverage_version": topic.version,
                "thesis_version": thesis.version,
                "prompt_version": self.prompt_version,
                "analysis_prompt_version": self.analysis_prompt_version,
                "red_team_prompt_version": self.red_team_prompt_version,
                "model_name": self.model_name,
                "red_team_model_name": self.red_team_model_name,
                "adapter_versions": audit.adapter_versions,
                "adapter_outputs": adapter_outputs,
                "signals": signal_payloads,
            }
            _json_dump(run_dir / "03-analysis.json", analysis_payload)
            audit.artifact_paths = list(audit.artifact_paths) + [str(run_dir / "03-analysis.json")]
            red_team_payload = {
                "run_id": run_id,
                "reviews": [
                    {"signal_id": item["id"], **item["red_team"]}
                    for item in signal_payloads
                    if item["red_team"] is not None
                ],
            }
            _json_dump(run_dir / "04-red-team.json", red_team_payload)
            audit.artifact_paths = list(audit.artifact_paths) + [str(run_dir / "04-red-team.json")]

            published = [item for item in signal_payloads if item["published"]]
            held = [item for item in signal_payloads if item["pipeline_state"] in {"CONFLICTED", "INSUFFICIENT_EVIDENCE", "UNVERIFIED"}]
            excluded = self._excluded_payloads(
                run_id,
                events,
                adapter_outputs,
                fixture_to_document,
            )
            render_result = render_brief(
                run_id=run_id,
                coverage_name=topic.name,
                coverage_version=topic.version,
                thesis=thesis.statement,
                published_signals=published,
                held_signals=held,
                excluded_events=excluded,
                output_path=run_dir / "05-brief.html",
            )
            audit.artifact_paths = list(audit.artifact_paths) + [str(run_dir / "05-brief.html")]
            for signal in signals:
                if signal.pipeline_state == PipelineState.ADJUDICATED:
                    transition(
                        self.session,
                        run_id=run_id,
                        entity=signal,
                        to_state=PipelineState.RENDERED,
                        reason="included consistently in local brief disposition",
                    )
            self.session.add(
                DeliveryFeedback(
                    signal_id=None,
                    run_id=run_id,
                    delivery_status=DeliveryStatus.LOCAL_RENDERED,
                    rendered_path=str(run_dir / "05-brief.html"),
                    feedback=f"Local-only preview: {render_result['summary']}",
                )
            )
            audit.documents_processed = len(batch.documents)
            audit.events_created = len(events)
            audit.signals_created = len(signals)
            audit.alerts_selected = sum(item["adjudication"] == "SEND_ALERT" for item in signal_payloads)
            audit.llm_schema_validated = (
                bool(schema_validation_results) and all(schema_validation_results)
                if run_kind == "manual"
                else True
            )
            audit.status = RunStatus.READY_FOR_OPS
            audit.finished_at = datetime.now(timezone.utc)
            self.session.commit()
            if run_ops:
                OpsChecker(self.session, self.runs_dir).check(run_id)
            return self.session.scalar(select(RunAudit).where(RunAudit.run_id == run_id))
        except Exception as exc:
            self.session.rollback()
            failed = self.session.scalar(select(RunAudit).where(RunAudit.run_id == run_id))
            if failed is not None:
                failed.status = RunStatus.FAILED
                failed.errors = list(failed.errors or []) + [f"{type(exc).__name__}: {exc}"]
                failed.finished_at = datetime.now(timezone.utc)
                self.session.commit()
            raise

    def _signal_payloads(self, run_id: str, signals: list[Signal], fixture_to_document: dict) -> list[dict]:
        events = {
            event.id: event
            for event in self.session.scalars(select(Event).where(Event.run_id == run_id)).all()
        }
        docs_by_id = {document.id: document for document in fixture_to_document.values()}
        payloads = []
        for signal in signals:
            event = events[signal.event_id]
            sources = [
                {
                    "tier": docs_by_id[document_id].source.trust_tier.value,
                    "name": docs_by_id[document_id].source.name,
                    "published_at": docs_by_id[document_id].published_at.isoformat(),
                    "url": docs_by_id[document_id].url,
                }
                for document_id in event.document_ids
            ]
            adjudication = signal.adjudication.value if signal.adjudication else None
            published = signal.pipeline_state == PipelineState.ADJUDICATED and adjudication in {
                Adjudication.INCLUDE_WEEKLY.value,
                Adjudication.SEND_ALERT.value,
            }
            payloads.append(
                {
                    "id": signal.id,
                    "event_key": event.event_key,
                    "event_title": event.canonical_title,
                    "affected_variable": signal.affected_variable,
                    "signal_level": signal.signal_level.value,
                    "analysis": signal.analysis_payload,
                    "red_team": signal.red_team_payload,
                    "adjudication": adjudication,
                    "pipeline_state": signal.pipeline_state.value,
                    "published": published,
                    "sources": sources,
                }
            )
        return payloads

    def _excluded_payloads(
        self,
        run_id: str,
        events: list[Event],
        adapter_outputs: list[dict],
        fixture_to_document: dict,
    ) -> list[dict]:
        outputs = {item["event_key"]: item["analysis"] for item in adapter_outputs}
        docs_by_id = {document.id: document for document in fixture_to_document.values()}
        payloads = []
        for event in events:
            if event.pipeline_state != PipelineState.OUT_OF_SCOPE:
                continue
            payloads.append(
                {
                    "event_title": event.canonical_title,
                    "pipeline_state": event.pipeline_state.value,
                    "analysis": outputs[event.event_key],
                    "sources": [
                        {
                            "tier": docs_by_id[document_id].source.trust_tier.value,
                            "name": docs_by_id[document_id].source.name,
                            "published_at": docs_by_id[document_id].published_at.isoformat(),
                            "url": docs_by_id[document_id].url,
                        }
                        for document_id in event.document_ids
                    ],
                }
            )
        return payloads
