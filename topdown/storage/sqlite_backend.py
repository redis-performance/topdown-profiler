"""SQLite storage backend implementation."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from topdown.storage.base import StorageBackend, escape_like, window_hours
from topdown.storage.models import Run, Sample
from topdown.storage.schema import SQLITE_SCHEMA
from topdown.storage.sql_queries import SqlNodeQueries
from topdown.units import normalized_unit_sql

_SAMPLE_UNIT = normalized_unit_sql("unit", pct="%")


def _cutoff_iso(last_hours: float) -> str:
    """Oldest started_at to keep, compared as text.

    Correct for the tz-aware UTC ``isoformat()`` this tool writes; rows stored
    in another format or offset by other tools are not normalised.
    """
    return (datetime.now(timezone.utc) - timedelta(hours=window_hours(last_hours))).isoformat()


# Matches a top-level label key exactly (dots, quotes, backslashes and all) on
# every SQLite version; a quoted JSON path is not portable.
_LABEL_MATCH = "EXISTS (SELECT 1 FROM json_each({labels}) WHERE json_each.key = ? AND json_each.value = ?)"


class SQLiteBackend(SqlNodeQueries, StorageBackend):
    PH = "?"
    PCT = "%"
    RUN_ID = "r.run_id"
    COLLATE = ""

    def _time_condition(self, last_hours: float) -> tuple[str, list]:
        return "r.started_at >= ?", [_cutoff_iso(last_hours)]

    def _label_condition(self, key: str, value: str) -> tuple[str, list]:
        return _LABEL_MATCH.format(labels="r.labels"), [key, value]

    def _name_filter(self, metric_names: list[str]) -> tuple[str, list]:
        return f"s.metric_name IN ({', '.join('?' * len(metric_names))})", metric_names

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def initialize(self) -> None:
        conn = self._get_conn()
        conn.executescript(SQLITE_SCHEMA)
        conn.commit()

    def insert_run(self, run: Run) -> str:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO runs (run_id, started_at, ended_at, duration_seconds,
               process_name, level, system_wide, labels)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.run_id,
                run.started_at.isoformat(),
                run.ended_at.isoformat() if run.ended_at else None,
                run.duration_seconds,
                run.process_name,
                run.level,
                int(run.system_wide),
                json.dumps(run.labels),
            ),
        )
        conn.commit()
        return run.run_id

    def update_run(self, run_id: str, ended_at: datetime, duration_seconds: float) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE runs SET ended_at = ?, duration_seconds = ? WHERE run_id = ?",
            (ended_at.isoformat(), duration_seconds, run_id),
        )
        conn.commit()

    def insert_samples(self, samples: list[Sample]) -> int:
        if not samples:
            return 0
        conn = self._get_conn()
        conn.executemany(
            """INSERT INTO samples (run_id, timestamp, cpu, metric_name, value, unit, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (s.run_id, s.timestamp, s.cpu, s.metric_name, s.value, s.unit, s.status)
                for s in samples
            ],
        )
        conn.commit()
        return len(samples)

    def get_run(self, run_id: str) -> Run | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def list_runs(
        self,
        process_name: str | None = None,
        labels: dict[str, str] | None = None,
        last_hours: float | None = None,
        limit: int = 50,
    ) -> list[Run]:
        conn = self._get_conn()
        query = "SELECT * FROM runs WHERE 1=1"
        params: list = []

        if process_name:
            query += " AND process_name = ?"
            params.append(process_name)

        if last_hours is not None:
            query += " AND started_at >= ?"
            params.append(_cutoff_iso(last_hours))

        if labels:
            for key, value in labels.items():
                query += " AND " + _LABEL_MATCH.format(labels="labels")
                params.extend([key, value])

        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_run(r) for r in rows]

    def get_samples(self, run_id: str, metric_name: str | None = None) -> list[Sample]:
        conn = self._get_conn()
        if metric_name:
            rows = conn.execute(
                "SELECT * FROM samples WHERE run_id = ? AND metric_name LIKE ? ESCAPE '\\' ORDER BY timestamp",
                (run_id, "%" + escape_like(metric_name) + "%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM samples WHERE run_id = ? ORDER BY timestamp",
                (run_id,),
            ).fetchall()
        return [self._row_to_sample(r) for r in rows]

    def get_aggregated_metrics(self, run_id: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            f"""SELECT metric_name, AVG(value) as avg_value, {_SAMPLE_UNIT} as unit,
                      MIN(value) as min_value, MAX(value) as max_value, COUNT(*) as sample_count
               FROM samples WHERE run_id = ?
               GROUP BY metric_name, {_SAMPLE_UNIT}
               ORDER BY avg_value DESC""",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            duration_seconds=row["duration_seconds"],
            process_name=row["process_name"],
            level=row["level"],
            system_wide=bool(row["system_wide"]),
            labels=json.loads(row["labels"]) if row["labels"] else {},
        )

    @staticmethod
    def _row_to_sample(row: sqlite3.Row) -> Sample:
        return Sample(
            sample_id=row["sample_id"],
            run_id=row["run_id"],
            timestamp=row["timestamp"],
            cpu=row["cpu"],
            metric_name=row["metric_name"],
            value=row["value"],
            unit=row["unit"],
            status=row["status"],
        )
