"""Domain models for topdown-profiler."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Run:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    duration_seconds: float = 0.0
    process_name: str = ""
    level: int = 1
    system_wide: bool = False
    labels: dict = field(default_factory=dict)


@dataclass
class Sample:
    sample_id: int | None = None
    run_id: str = ""
    timestamp: float = 0.0
    cpu: int | None = None
    metric_name: str = ""
    value: float = 0.0
    unit: str = "%"
    status: str = ""
