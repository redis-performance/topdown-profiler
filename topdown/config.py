import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_DIR = Path.home() / ".topdown"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "data.db"


@dataclass
class TopdownConfig:
    backend: str = "sqlite"
    dsn: str | None = None
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    toplev_path: str = "toplev.py"
    pmu_tools_dir: str | None = None
    uprof_pcm_path: str | None = None  # explicit path to AMDuProfPcm (optional)
    collector: str | None = None  # "toplev", "perf_stat", "uprof_pcm", or None (auto-detect)

    @classmethod
    def from_env(cls) -> "TopdownConfig":
        return cls(
            backend=os.environ.get("TOPDOWN_BACKEND", "sqlite"),
            dsn=os.environ.get("TOPDOWN_DSN"),
            db_path=Path(os.environ.get("TOPDOWN_DB_PATH", str(DEFAULT_DB_PATH))),
            toplev_path=os.environ.get("TOPDOWN_TOPLEV_PATH", "toplev.py"),
            pmu_tools_dir=os.environ.get("TOPDOWN_PMU_TOOLS_DIR"),
            uprof_pcm_path=os.environ.get("TOPDOWN_UPROF_PCM_PATH"),
            collector=os.environ.get("TOPDOWN_COLLECTOR"),
        )


def get_config(db_path: str | None = None) -> TopdownConfig:
    config = TopdownConfig.from_env()
    if db_path:
        config.db_path = Path(db_path)
    return config
