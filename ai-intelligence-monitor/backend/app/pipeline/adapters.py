from abc import ABC, abstractmethod
from pathlib import Path

from app.pipeline.contracts import (
    AnalysisResult,
    CollectedBatch,
    DocumentInput,
    FixtureScenario,
    RedTeamResult,
)


class CollectorAdapter(ABC):
    name: str
    version: str

    @abstractmethod
    def collect(self, scenario: str) -> CollectedBatch: ...


class AnalysisAdapter(ABC):
    name: str
    version: str

    @abstractmethod
    def analyze(self, document: DocumentInput) -> AnalysisResult: ...


class RedTeamAdapter(ABC):
    name: str
    version: str

    @abstractmethod
    def review(
        self,
        document: DocumentInput,
        analysis: AnalysisResult | None = None,
    ) -> RedTeamResult: ...


class FixtureCollector(CollectorAdapter):
    name = "FixtureCollector"
    version = "fixture-collector-v1"

    def __init__(self, fixtures_dir: Path):
        self.fixtures_dir = Path(fixtures_dir)

    def collect(self, scenario: str) -> CollectedBatch:
        path = self.fixtures_dir / f"{scenario}.json"
        fixture = FixtureScenario.model_validate_json(path.read_text(encoding="utf-8"))
        return CollectedBatch(
            adapter_name=self.name,
            adapter_version=self.version,
            scenario=fixture.scenario,
            documents=fixture.documents,
        )


class DeterministicAnalysisAdapter(AnalysisAdapter):
    name = "DeterministicAnalysisAdapter"
    version = "deterministic-analysis-v1"

    def analyze(self, document: DocumentInput) -> AnalysisResult:
        return AnalysisResult(
            adapter_name=self.name,
            adapter_version=self.version,
            **document.analysis.model_dump(),
        )


class DeterministicRedTeamAdapter(RedTeamAdapter):
    name = "DeterministicRedTeamAdapter"
    version = "deterministic-red-team-v1"

    def review(
        self,
        document: DocumentInput,
        analysis: AnalysisResult | None = None,
    ) -> RedTeamResult:
        if document.red_team is None:
            raise ValueError(f"fixture {document.fixture_id} has no red-team directive")
        return RedTeamResult(
            adapter_name=self.name,
            adapter_version=self.version,
            **document.red_team.model_dump(),
        )
