"""Abstract storage backend interface."""

from abc import ABC, abstractmethod

from topdown.storage.models import Run, Sample


class StorageBackend(ABC):
    @abstractmethod
    def initialize(self) -> None:
        """Create tables and indexes if they don't exist."""
        ...

    @abstractmethod
    def insert_run(self, run: Run) -> str:
        """Insert a run record. Returns run_id."""
        ...

    @abstractmethod
    def update_run(self, run_id: str, ended_at, duration_seconds: float) -> None:
        """Update run completion data."""
        ...

    @abstractmethod
    def insert_samples(self, samples: list[Sample]) -> int:
        """Bulk insert samples. Returns count inserted."""
        ...

    @abstractmethod
    def get_run(self, run_id: str) -> Run | None:
        ...

    @abstractmethod
    def list_runs(
        self,
        process_name: str | None = None,
        labels: dict[str, str] | None = None,
        last_hours: float | None = None,
        limit: int = 50,
    ) -> list[Run]:
        ...

    @abstractmethod
    def get_samples(self, run_id: str, metric_name: str | None = None) -> list[Sample]:
        ...

    @abstractmethod
    def get_aggregated_metrics(self, run_id: str) -> list[dict]:
        """Return avg value per metric_name for a run, ordered by value desc."""
        ...

    @abstractmethod
    def query_bottlenecks(
        self,
        process_name: str | None = None,
        labels: dict[str, str] | None = None,
        last_hours: float | None = None,
        min_percentage: float = 0.0,
    ) -> list[dict]:
        """Query across runs: find top bottleneck metrics."""
        ...

    @abstractmethod
    def query_by_bottleneck(
        self,
        metric_name: str,
        min_pct: float = 5.0,
        labels: dict[str, str] | None = None,
        last_hours: float | None = None,
    ) -> list[dict]:
        """Find runs where a specific TMA node exceeds threshold. Returns runs with labels."""
        ...

    @abstractmethod
    def close(self) -> None:
        ...
