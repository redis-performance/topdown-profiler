"""Node and bottleneck queries shared by the SQL backends.

Both backends run the same statements; only placeholders, how a literal '%'
is written, JSON label access, the time filter and collation differ, and
those are the class attributes/hooks a backend sets.
"""

from __future__ import annotations

from topdown.storage.base import DEFAULT_NODE_LIMIT
from topdown.units import (
    bottleneck_candidate_sql,
    normalized_unit_sql,
    percent_unit_sql,
    quality_metrics_sql,
    slot_unit_sql,
)


def _order_key(avg: str) -> str:
    """An average rounded to 1e-9 for ordering, as a float on both dialects.

    Magnitudes beyond 1e290 would overflow the scaling, so they tie at the ends
    (and fall back to the name/run tie-break).
    """
    return f"CASE WHEN {avg} > 1e290 THEN 1e300 WHEN {avg} < -1e290 THEN -1e300 ELSE round({avg} * 1e9) END"


class SqlNodeQueries:
    """Mixin: list_metric_names, query_nodes and query_bottlenecks over ``samples``/``runs``."""

    PH = "?"  # parameter placeholder
    PCT = "%"  # a literal '%' in query text
    RUN_ID = "r.run_id"  # run_id as text in result rows
    COLLATE = ""  # appended to metric_name in ORDER BY, for byte-order ties
    VALUE_FILTER = ""  # extra sample predicate, e.g. to drop NaN where it can be stored

    def _time_condition(self, last_hours: float) -> tuple[str, list]:
        raise NotImplementedError

    def _label_condition(self, key: str, value: str) -> tuple[str, list]:
        raise NotImplementedError

    def _name_filter(self, metric_names: list[str]) -> tuple[str, list]:
        raise NotImplementedError

    def _fragments(self) -> dict[str, str]:
        unit = normalized_unit_sql("s.unit", pct=self.PCT)
        return {
            "unit": unit,
            "percent": percent_unit_sql(unit, pct=self.PCT),
            "quality": quality_metrics_sql("s.metric_name"),
            "candidate": bottleneck_candidate_sql("s.metric_name"),
            "slot_first": f"CASE WHEN {slot_unit_sql(unit, pct=self.PCT)} THEN 0 ELSE 1 END",
            "slot_first_agg": f"CASE WHEN {slot_unit_sql('a.unit', pct=self.PCT)} THEN 0 ELSE 1 END",
        }

    def _run_filter(self, process_name, labels, last_hours) -> tuple[str, list]:
        """``AND s.run_id IN (...)`` restricting samples to matching runs, or ``""``."""
        conds, params = [], []
        if process_name:
            conds.append(f"r.process_name = {self.PH}")
            params.append(process_name)
        if last_hours is not None:
            cond, cond_params = self._time_condition(last_hours)
            conds.append(cond)
            params.extend(cond_params)
        for key, value in (labels or {}).items():
            cond, cond_params = self._label_condition(key, value)
            conds.append(cond)
            params.extend(cond_params)
        if not conds:
            return "", []
        return " AND s.run_id IN (SELECT r.run_id FROM runs r WHERE " + " AND ".join(conds) + ")", params

    def _aggregate(self, name_filter, name_params, candidates_key, min_value, limit, run_filter) -> list[dict]:
        """Per-run averages, aggregated over samples alone; runs are joined last.

        Carrying the runs columns (labels JSON) through the GROUP BY sort made
        unbounded queries spill to disk; joining the <= ``limit`` survivors is
        cheap. Averages are rounded for ordering (see ``_order_key``): parallel
        aggregation sums floats in varying order, and the last bits must not
        reorder ties.
        """
        f = self._fragments()
        sql_filter, filter_params = run_filter
        order_value = _order_key("AVG(s.value)")
        query = f"""
            WITH agg AS (
                SELECT s.run_id, s.metric_name, {f["unit"]} AS unit, AVG(s.value) AS avg_value
                FROM samples s
                WHERE {name_filter} AND {f["percent"]} AND {f[candidates_key]}{self.VALUE_FILTER}{sql_filter}
                GROUP BY s.run_id, s.metric_name, {f["unit"]}
                HAVING AVG(s.value) >= {self.PH}
                ORDER BY {f["slot_first"]}, {order_value} DESC, s.metric_name{self.COLLATE}, s.run_id
                LIMIT {self.PH}
            )
            SELECT {self.RUN_ID}, r.started_at, r.process_name, r.labels, r.level,
                   a.metric_name, a.avg_value, a.unit
            FROM agg a JOIN runs r ON r.run_id = a.run_id
            ORDER BY {f["slot_first_agg"]}, {_order_key("a.avg_value")} DESC,
                     a.metric_name{self.COLLATE}, r.run_id
        """
        params = [*name_params, *filter_params, min_value, limit]
        rows = self._get_conn().execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def list_metric_names(self) -> list[str]:
        # Skip scan over idx_samples_metric_name: one index probe per distinct
        # name instead of reading every sample.
        rows = (
            self._get_conn()
            .execute(
                """WITH RECURSIVE names(metric_name) AS (
                   SELECT min(metric_name) FROM samples
                   UNION ALL
                   SELECT (SELECT min(metric_name) FROM samples WHERE metric_name > names.metric_name)
                   FROM names WHERE names.metric_name IS NOT NULL
               )
               SELECT metric_name FROM names WHERE metric_name IS NOT NULL"""
            )
            .fetchall()
        )
        return [r["metric_name"] for r in rows]

    def query_nodes(
        self, metric_names, min_pct=5.0, labels=None, last_hours=None, limit=DEFAULT_NODE_LIMIT
    ) -> list[dict]:
        if not metric_names:
            return []
        name_filter, name_params = self._name_filter(list(metric_names))
        return self._aggregate(
            name_filter, name_params, "quality", min_pct, limit, self._run_filter(None, labels, last_hours)
        )

    def query_bottlenecks(
        self, process_name=None, labels=None, last_hours=None, min_percentage=0.0, limit=DEFAULT_NODE_LIMIT
    ) -> list[dict]:
        return self._aggregate(
            "TRUE", [], "candidate", min_percentage, limit, self._run_filter(process_name, labels, last_hours)
        )
