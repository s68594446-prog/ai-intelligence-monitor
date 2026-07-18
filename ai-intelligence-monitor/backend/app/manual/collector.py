from sqlalchemy.orm import Session

from app.core.enums import (
    Adjudication,
    Direction,
    FactType,
    SignalLevel,
    SourceType,
    TrustTier,
    VerificationStatus,
)
from app.models import Document
from app.pipeline.adapters import CollectorAdapter
from app.pipeline.contracts import (
    AnalysisDirective,
    CollectedBatch,
    DocumentInput,
    SourceInput,
)


def _placeholder_analysis() -> AnalysisDirective:
    return AnalysisDirective(
        affected_variable="pending coverage analysis",
        direction=Direction.NEUTRAL,
        novelty=0,
        materiality=0,
        source_quality=0,
        signal_level=SignalLevel.S0,
        facts=[],
        marginal_change="Pending structured analysis.",
        thesis_impact="Pending structured analysis.",
        unknowns=[],
        next_validation="Pending structured analysis.",
        adjudication=Adjudication.DROP,
        out_of_scope=False,
    )


def snapshot_to_document_input(snapshot: dict) -> DocumentInput:
    return DocumentInput(
        fixture_id=snapshot["reference_id"],
        source=SourceInput(
            key=f"manual-source-{snapshot['source_id']}",
            name=snapshot["source_name"],
            source_type=SourceType(snapshot["source_type"]),
            trust_tier=TrustTier(snapshot["source_tier"]),
            url=snapshot.get("url") or "",
        ),
        title=snapshot["title"],
        url=snapshot.get("url") or "",
        published_at=snapshot["published_at"],
        language=snapshot["language"],
        raw_text=snapshot["raw_text"],
        fact_type=FactType.UNKNOWN,
        verification_status=VerificationStatus(snapshot["verification_status"]),
        event_key=f"manual-{snapshot['content_hash'][:24]}",
        event_summary=snapshot["title"],
        conflicted=False,
        analysis=_placeholder_analysis(),
        red_team=None,
        optional_notes=snapshot.get("optional_notes"),
    )


class ManualCollector(CollectorAdapter):
    name = "ManualCollector"
    version = "manual-collector-v1"

    def __init__(self, session: Session, document_id: int):
        self.session = session
        self.document_id = document_id

    def collect(self, scenario: str) -> CollectedBatch:
        document = self.session.get(Document, self.document_id)
        if document is None or document.ingestion_id is None:
            raise ValueError(f"manual document not found: {self.document_id}")
        snapshot = manual_snapshot(document)
        item = snapshot_to_document_input(snapshot)
        return CollectedBatch(
            adapter_name=self.name,
            adapter_version=self.version,
            scenario=scenario,
            documents=[item],
            persisted_document_ids={item.fixture_id: document.id},
        )


class ManualSnapshotCollector(CollectorAdapter):
    name = "ManualSnapshotCollector"
    version = "manual-snapshot-collector-v1"

    def __init__(self, snapshot: dict):
        self.snapshot = snapshot

    def collect(self, scenario: str) -> CollectedBatch:
        item = snapshot_to_document_input(self.snapshot)
        return CollectedBatch(
            adapter_name=self.name,
            adapter_version=self.version,
            scenario=scenario,
            documents=[item],
        )


def manual_snapshot(document: Document) -> dict:
    return {
        "document_id": document.id,
        "ingestion_id": document.ingestion_id,
        "reference_id": f"manual-{document.id}",
        "source_id": document.source_id,
        "source_name": document.source.name,
        "source_type": document.source.source_type.value,
        "source_tier": document.source.trust_tier.value,
        "title": document.title,
        "url": document.url,
        "published_at": document.published_at.isoformat(),
        "language": document.language,
        "raw_text": document.raw_text,
        "content_hash": document.content_hash,
        "verification_status": document.verification_status.value,
        "optional_notes": document.optional_notes,
    }

