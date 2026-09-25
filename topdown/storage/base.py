"""Abstract storage backend interface."""

import math
from abc import ABC, abstractmethod

from topdown.storage.models import Run, Sample
from topdown.units import is_quality_metric

# query_by_bottleneck match modes.
MATCH_EXACT = "exact"          # the node itself or any node path ending in ".<name>"
MATCH_SUBSTRING = "substring"  # case-insensitive substring of the node path
DEFAULT_NODE_LIMIT = 200
# Longest time window honoured (~100 years); larger values overflow the dialects' date math.
MAX_WINDOW_HOURS = 24 * 365 * 100


def window_hours(last_hours: float) -> float:
    """``last_hours`` capped at ``MAX_WINDOW_HOURS``; NaN or infinity mean the whole history."""
    return min(last_hours, MAX_WINDOW_HOURS) if math.isfinite(last_hours) else MAX_WINDOW_HOURS


def escape_like(text: str) -> str:
    """Escape LIKE wildcards (use with ``ESCAPE '\\'``); node names contain '_'."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        limit: int = DEFAULT_NODE_LIMIT,
    ) -> list[dict]:
        """Per-run averages of every bottleneck candidate (not MUX, not useful work).

        Same ordering and row shape as ``query_nodes``.
        """
        ...

    @abstractmethod
    def list_metric_names(self) -> list[str]:
        """Distinct metric names across all stored samples."""
        ...

    @abstractmethod
    def query_nodes(
        self,
        metric_names: list[str],
        min_pct: float = 5.0,
        labels: dict[str, str] | None = None,
        last_hours: float | None = None,
        limit: int = DEFAULT_NODE_LIMIT,
    ) -> list[dict]:
        """Per-run averages of the given nodes at or above ``min_pct``.

        Slot shares come first, then other denominators, each highest first
        (ties by name, then run), so the limit never drops a slot share in
        favour of a "% Clocks" value. Quality metrics and non-percentage units
        are never returned. Rows carry the normalized ``unit`` and a text
        ``run_id``.
        """
        ...

    def resolve_node_names(self, metric_name: str, match: str = MATCH_EXACT) -> list[str]:
        """Stored node names that ``metric_name`` refers to under ``match``.

        Both modes ignore case; exact means the whole name or a whole dotted
        suffix ("l3_bound" -> "Backend_Bound.Memory_Bound.L3_Bound").
        """
        if not metric_name:
            return []
        needle = metric_name.lower()
        names = [n for n in self.list_metric_names() if not is_quality_metric(n)]
        if match == MATCH_SUBSTRING:
            return [n for n in names if needle in n.lower()]
        return [n for n in names if n.lower() == needle or n.lower().endswith("." + needle)]

    def query_by_bottleneck(
        self,
        metric_name: str,
        min_pct: float = 5.0,
        labels: dict[str, str] | None = None,
        last_hours: float | None = None,
        match: str = MATCH_EXACT,
        limit: int = DEFAULT_NODE_LIMIT,
    ) -> list[dict]:
        """Find runs where a TMA node exceeds a threshold.

        ``match=MATCH_EXACT`` selects the node named ``metric_name`` at any depth
        ("L3_Bound" or "Backend_Bound.Memory_Bound.L3_Bound"); ``MATCH_SUBSTRING``
        matches any node containing it. Both ignore case. Quality metrics (MUX)
        are never matched. See ``query_nodes`` for ordering.
        """
        names = self.resolve_node_names(metric_name, match)
        if not names:
            return []
        return self.query_nodes(names, min_pct=min_pct, labels=labels, last_hours=last_hours, limit=limit)

    def find_runs_by_node(
        self, metric_name: str, limit: int = DEFAULT_NODE_LIMIT, **kwargs
    ) -> tuple[list[dict], str, bool]:
        """``query_by_bottleneck`` by exact node name, or by substring if no node has that name.

        Whether a node exists is decided from the stored names, not from the
        rows, so a node that is merely below the threshold returns no rows
        rather than a substring match. Returns the rows (at most ``limit``),
        the match mode used, and whether more rows matched than ``limit``.
        """
        names = self.resolve_node_names(metric_name, MATCH_EXACT)
        match = MATCH_EXACT
        if not names:
            names, match = self.resolve_node_names(metric_name, MATCH_SUBSTRING), MATCH_SUBSTRING
        rows = self.query_nodes(names, limit=limit + 1, **kwargs) if names else []
        return rows[:limit], match, len(rows) > limit

    @abstractmethod
    def close(self) -> None:
        ...
