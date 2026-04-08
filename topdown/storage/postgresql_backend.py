"""PostgreSQL storage backend implementation using psycopg v3."""

import json
import uuid
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from topdown.storage.base import StorageBackend
from topdown.storage.models import Run, Sample
from topdown.storage.schema import POSTGRESQL_SCHEMA


def _is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(val)
        return True
    except (ValueError, AttributeError):
        return False


class PostgreSQLBackend(StorageBackend):
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: psycopg.Connection | None = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row)
        return self._conn

    def initialize(self) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            for statement in POSTGRESQL_SCHEMA.split(";"):
                statement = statement.strip()
                if statement:
                    cur.execute(statement)
        conn.commit()

    def insert_run(self, run: Run) -> str:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO runs (run_id, started_at, ended_at, duration_seconds,
               process_name, level, system_wide, labels)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                run.run_id,
                run.started_at,
                run.ended_at,
                run.duration_seconds,
                run.process_name,
                run.level,
                run.system_wide,
                json.dumps(run.labels),
            ),
        )
        conn.commit()
        return run.run_id

    def update_run(self, run_id: str, ended_at: datetime, duration_seconds: float) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE runs SET ended_at = %s, duration_seconds = %s WHERE run_id = %s",
            (ended_at, duration_seconds, run_id),
        )
        conn.commit()

    def insert_samples(self, samples: list[Sample]) -> int:
        if not samples:
            return 0
        conn = self._get_conn()
        with conn.cursor() as cur:
            with cur.copy(
                "COPY samples (run_id, timestamp, cpu, metric_name, value, unit, status) "
                "FROM STDIN"
            ) as copy:
                for s in samples:
                    copy.write_row((s.run_id, s.timestamp, s.cpu, s.metric_name, s.value, s.unit, s.status))
        conn.commit()
        return len(samples)

    def get_run(self, run_id: str) -> Run | None:
        if not _is_valid_uuid(run_id):
            return None
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,)).fetchone()
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
        query = "SELECT * FROM runs WHERE TRUE"
        params: list = []

        if process_name:
            query += " AND process_name = %s"
            params.append(process_name)

        if last_hours is not None:
            query += " AND started_at >= NOW() - make_interval(secs => %s)"
            params.append(last_hours * 3600)

        if labels:
            for key, value in labels.items():
                query += " AND labels->>%s = %s"
                params.extend([key, value])

        query += " ORDER BY started_at DESC LIMIT %s"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_run(r) for r in rows]

    def get_samples(self, run_id: str, metric_name: str | None = None) -> list[Sample]:
        if not _is_valid_uuid(run_id):
            return []
        conn = self._get_conn()
        if metric_name:
            rows = conn.execute(
                "SELECT * FROM samples WHERE run_id = %s AND metric_name LIKE %s ORDER BY timestamp",
                (run_id, f"%{metric_name}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM samples WHERE run_id = %s ORDER BY timestamp",
                (run_id,),
            ).fetchall()
        return [self._row_to_sample(r) for r in rows]

    def get_aggregated_metrics(self, run_id: str) -> list[dict]:
        if not _is_valid_uuid(run_id):
            return []
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT metric_name, AVG(value) as avg_value, unit,
                      MIN(value) as min_value, MAX(value) as max_value, COUNT(*) as sample_count
               FROM samples WHERE run_id = %s
               GROUP BY metric_name, unit
               ORDER BY avg_value DESC""",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def query_bottlenecks(
        self,
        process_name: str | None = None,
        labels: dict[str, str] | None = None,
        last_hours: float | None = None,
        min_percentage: float = 0.0,
    ) -> list[dict]:
        conn = self._get_conn()
        query = """
            SELECT r.run_id, r.started_at, r.process_name, r.labels, r.level,
                   s.metric_name, AVG(s.value) as avg_value, s.unit
            FROM runs r
            JOIN samples s ON r.run_id = s.run_id
            WHERE s.unit IN ('%%', '%%_Slots', 'slots')
        """
        params: list = []

        if process_name:
            query += " AND r.process_name = %s"
            params.append(process_name)

        if last_hours is not None:
            query += " AND r.started_at >= NOW() - make_interval(secs => %s)"
            params.append(last_hours * 3600)

        if labels:
            for key, value in labels.items():
                query += " AND r.labels->>%s = %s"
                params.extend([key, value])

        query += " GROUP BY r.run_id, s.metric_name, s.unit HAVING AVG(s.value) >= %s"
        params.append(min_percentage)
        query += " ORDER BY avg_value DESC"

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def query_by_bottleneck(
        self,
        metric_name: str,
        min_pct: float = 5.0,
        labels: dict[str, str] | None = None,
        last_hours: float | None = None,
    ) -> list[dict]:
        conn = self._get_conn()
        query = """
            SELECT r.run_id, r.started_at, r.process_name, r.labels, r.level,
                   s.metric_name, AVG(s.value) as avg_value, s.unit
            FROM runs r
            JOIN samples s ON r.run_id = s.run_id
            WHERE s.metric_name LIKE %s
              AND s.unit IN ('%%', '%%_Slots', 'slots')
        """
        params: list = [f"%{metric_name}%"]

        if last_hours is not None:
            query += " AND r.started_at >= NOW() - make_interval(secs => %s)"
            params.append(last_hours * 3600)

        if labels:
            for key, value in labels.items():
                query += " AND r.labels->>%s = %s"
                params.extend([key, value])

        query += " GROUP BY r.run_id, s.metric_name, s.unit HAVING AVG(s.value) >= %s"
        params.append(min_pct)
        query += " ORDER BY avg_value DESC"

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_run(row: dict) -> Run:
        labels = row.get("labels", "{}")
        if isinstance(labels, str):
            labels = json.loads(labels)
        return Run(
            run_id=str(row["run_id"]),
            started_at=row["started_at"] if isinstance(row["started_at"], datetime) else datetime.fromisoformat(str(row["started_at"])),
            ended_at=row["ended_at"] if isinstance(row.get("ended_at"), datetime) else (datetime.fromisoformat(str(row["ended_at"])) if row.get("ended_at") else None),
            duration_seconds=float(row.get("duration_seconds", 0)),
            process_name=row.get("process_name", ""),
            level=int(row.get("level", 1)),
            system_wide=bool(row.get("system_wide", False)),
            labels=labels,
        )

    @staticmethod
    def _row_to_sample(row: dict) -> Sample:
        return Sample(
            sample_id=row.get("sample_id"),
            run_id=str(row["run_id"]),
            timestamp=float(row.get("timestamp", 0)),
            cpu=row.get("cpu"),
            metric_name=row.get("metric_name", ""),
            value=float(row.get("value", 0)),
            unit=row.get("unit", "%"),
            status=row.get("status", ""),
        )
