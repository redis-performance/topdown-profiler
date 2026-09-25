"""Unit normalization and node matching in both storage backends.

The PostgreSQL cases run only when TOPDOWN_TEST_DSN is set. Each test gets
its own throwaway schema (dropped afterwards), so the DSN may point at a
shared database without touching its tables.
"""

import os
import uuid

import pytest

from topdown.storage.base import MATCH_EXACT, MATCH_SUBSTRING
from topdown.storage.models import Run, Sample
from topdown.storage.sqlite_backend import SQLiteBackend

PG_DSN = os.environ.get("TOPDOWN_TEST_DSN")


@pytest.fixture(params=["sqlite", "postgresql"])
def backend(request, tmp_path):
    if request.param == "sqlite":
        db = SQLiteBackend(db_path=tmp_path / "units.db")
        db.initialize()
        yield db
        db.close()
        return

    if not PG_DSN:
        pytest.skip("TOPDOWN_TEST_DSN not set")
    try:
        import psycopg
    except ImportError:  # asked for PostgreSQL but can't run it: fail, don't skip
        pytest.fail("TOPDOWN_TEST_DSN is set but psycopg is not installed")
    from psycopg.conninfo import make_conninfo

    from topdown.storage.postgresql_backend import PostgreSQLBackend

    schema = f"topdown_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    db = None
    try:
        db = PostgreSQLBackend(dsn=make_conninfo(PG_DSN, options=f"-c search_path={schema}"))
        # Refuse to create tables anywhere else (e.g. a pooler dropping `options`).
        current = db._get_conn().execute("SELECT current_schema() AS s").fetchone()["s"]
        assert current == schema, f"search_path not applied: {current}"
        db.initialize()
        yield db
    finally:
        if db is not None:
            db.close()
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _run_with(backend, rows, labels=None, started_at=None):
    run = Run(process_name="redis-server", level=4, labels=labels or {"test_name": "t"})
    if started_at is not None:
        run.started_at = started_at
    backend.insert_run(run)
    backend.insert_samples(
        [
            Sample(run_id=run.run_id, timestamp=float(i), metric_name=m, value=v, unit=u)
            for i, (m, v, u) in enumerate(rows)
        ]
    )
    return run


def test_threshold_crossing_does_not_split_a_metric(backend):
    # 3 intervals above threshold, 1 below: one row, averaged over all 4.
    run = _run_with(
        backend,
        [
            ("Frontend_Bound", 20.0, "% Slots"),
            ("Frontend_Bound", 20.0, "% Slots"),
            ("Frontend_Bound", 20.0, "% Slots"),
            ("Frontend_Bound", 4.0, "% Slots <"),
        ],
    )
    rows = backend.get_aggregated_metrics(run.run_id)
    assert [(r["metric_name"], r["unit"], r["avg_value"], r["sample_count"]) for r in rows] == [
        ("Frontend_Bound", "% Slots", 16.0, 4),
    ]


def test_exact_match_at_any_depth_and_units_normalized(backend):
    _run_with(
        backend,
        [
            ("Backend_Bound.Memory_Bound.L1_Bound", 12.0, "% Stalls <"),
            ("Backend_Bound.Memory_Bound.L1_Bound.L1_Latency_Dependency", 40.0, "% Clocks_est <"),
        ],
    )
    found = backend.query_by_bottleneck("L1_Bound", min_pct=5.0)
    assert [(r["metric_name"], r["unit"]) for r in found] == [("Backend_Bound.Memory_Bound.L1_Bound", "% Stalls")]
    assert isinstance(found[0]["run_id"], str)


def test_exact_match_full_path_and_top_level(backend):
    _run_with(backend, [("Backend_Bound", 40.0, "% Slots <"), ("Backend_Bound.Memory_Bound", 30.0, "% Slots")])
    assert len(backend.query_by_bottleneck("Backend_Bound", min_pct=5.0)) == 1
    assert len(backend.query_by_bottleneck("Backend_Bound.Memory_Bound", min_pct=5.0)) == 1


def test_exact_match_ignores_case_on_both_backends(backend):
    _run_with(backend, [("Backend_Bound.Memory_Bound.L3_Bound", 20.0, "% Stalls")])
    rows, match, _ = backend.find_runs_by_node("l3_bound", min_pct=5.0)
    assert ([r["metric_name"] for r in rows], match) == (["Backend_Bound.Memory_Bound.L3_Bound"], MATCH_EXACT)


def test_substring_match_is_case_insensitive_on_both_backends(backend):
    _run_with(backend, [("Backend_Bound.Memory_Bound.L3_Bound", 20.0, "% Stalls")])
    found = backend.query_by_bottleneck("l3_bo", min_pct=5.0, match=MATCH_SUBSTRING)
    assert [r["metric_name"] for r in found] == ["Backend_Bound.Memory_Bound.L3_Bound"]


def test_underscore_is_not_a_wildcard(backend):
    _run_with(backend, [("Backend_Bound.Memory_Bound.L1xBound", 40.0, "%")])
    assert backend.query_by_bottleneck("L1_Bound", min_pct=1.0) == []
    assert backend.query_by_bottleneck("L1_Bound", min_pct=1.0, match=MATCH_SUBSTRING) == []


def test_find_runs_by_node_falls_back_to_substring(backend):
    _run_with(backend, [("Backend_Bound.Memory_Bound", 30.0, "% Slots")])
    rows, match, truncated = backend.find_runs_by_node("Memory_Bound", min_pct=5.0)
    assert (len(rows), match, truncated) == (1, MATCH_EXACT, False)
    rows, match, _ = backend.find_runs_by_node("Memory", min_pct=5.0)
    assert (len(rows), match) == (1, MATCH_SUBSTRING)


def test_quality_metrics_excluded(backend):
    _run_with(backend, [("MUX", 100.0, "%"), ("Backend_Bound", 30.0, "% Slots <")])
    assert [r["metric_name"] for r in backend.query_bottlenecks(min_percentage=1.0)] == ["Backend_Bound"]
    assert backend.query_nodes(["MUX"], min_pct=1.0) == []
    assert backend.query_by_bottleneck("MUX", min_pct=1.0) == []
    assert backend.query_by_bottleneck("MUX", min_pct=1.0, match=MATCH_SUBSTRING) == []


def test_limit(backend):
    for i in range(5):
        _run_with(backend, [("Backend_Bound", 40.0 + i, "%")])
    found = backend.query_by_bottleneck("Backend_Bound", min_pct=1.0, limit=2)
    assert [r["avg_value"] for r in found] == [44.0, 43.0]


def test_get_samples_filter_treats_underscore_literally(backend):
    run = _run_with(backend, [("Backend_Bound.Memory_Bound.L1_Bound", 1.0, "%"), ("Backend_Bound.L1xBound", 2.0, "%")])
    assert [s.metric_name for s in backend.get_samples(run.run_id, "L1_Bound")] == [
        "Backend_Bound.Memory_Bound.L1_Bound",
    ]


def test_ranking_queries_merge_threshold_crossings(backend):
    _run_with(
        backend,
        [
            ("Backend_Bound.Memory_Bound", 20.0, "% Slots"),
            ("Backend_Bound.Memory_Bound", 20.0, "% Slots"),
            ("Backend_Bound.Memory_Bound", 20.0, "% Slots"),
            ("Backend_Bound.Memory_Bound", 4.0, "% Slots <"),
        ],
    )
    for rows in (backend.query_bottlenecks(min_percentage=1.0), backend.query_by_bottleneck("Memory_Bound")):
        assert [(r["metric_name"], r["unit"], r["avg_value"]) for r in rows] == [
            ("Backend_Bound.Memory_Bound", "% Slots", 16.0),
        ]
        assert isinstance(rows[0]["run_id"], str)


def test_exact_match_needs_a_dot_boundary(backend):
    _run_with(backend, [("Backend_Bound.Memory_Bound.XL1_Bound", 40.0, "%")])
    assert backend.resolve_node_names("L1_Bound") == []
    assert backend.query_by_bottleneck("L1_Bound", min_pct=1.0) == []


def test_existing_node_below_threshold_does_not_fall_back(backend):
    _run_with(
        backend,
        [
            ("Backend_Bound.Core_Bound", 30.0, "% Slots"),
            ("Backend_Bound.Core_Bound.Serializing_Operation", 61.0, "% Clocks"),
        ],
    )
    assert backend.find_runs_by_node("Core_Bound", min_pct=35.0) == ([], MATCH_EXACT, False)


def test_slot_shares_come_first_under_the_limit(backend):
    _run_with(
        backend,
        [
            ("Backend_Bound.Memory_Bound", 30.0, "% Slots"),
            ("Backend_Bound.Memory_Bound.L2_Bound.L2_Hit_Latency", 190.0, "% Clocks_Retired"),
        ],
    )
    rows = backend.query_by_bottleneck("memory", min_pct=1.0, match=MATCH_SUBSTRING, limit=1)
    assert [r["metric_name"] for r in rows] == ["Backend_Bound.Memory_Bound"]


def test_list_metric_names_is_distinct(backend):
    _run_with(backend, [("Frontend_Bound", 1.0, "%"), ("Frontend_Bound", 2.0, "%"), ("MUX", 3.0, "%")])
    assert sorted(backend.list_metric_names()) == ["Frontend_Bound", "MUX"]


def test_get_samples_filter_treats_percent_literally(backend):
    run = _run_with(backend, [("Backend_Bound", 1.0, "%")])
    assert backend.get_samples(run.run_id, "%") == []


def test_find_runs_by_node_reports_truncation(backend):
    for value in (10.0, 20.0, 30.0):
        _run_with(backend, [("Backend_Bound", value, "%")])
    rows, _, truncated = backend.find_runs_by_node("Backend_Bound", min_pct=1.0, limit=2)
    assert ([r["avg_value"] for r in rows], truncated) == ([30.0, 20.0], True)
    rows, _, truncated = backend.find_runs_by_node("Backend_Bound", min_pct=1.0, limit=3)
    assert (len(rows), truncated) == (3, False)


def test_default_limit_is_200():
    import inspect

    from topdown.storage.base import DEFAULT_NODE_LIMIT, StorageBackend

    assert DEFAULT_NODE_LIMIT == 200
    assert inspect.signature(StorageBackend.find_runs_by_node).parameters["limit"].default == 200


def test_non_percentage_nodes_are_never_returned(backend):
    _run_with(backend, [("Backend_Bound.IPC", 2.5, "Metric"), ("Backend_Bound", 30.0, "%")])
    assert backend.query_by_bottleneck("IPC", min_pct=0.0) == []
    assert [r["metric_name"] for r in backend.query_bottlenecks(min_percentage=0.0)] == ["Backend_Bound"]


def test_query_bottlenecks_skips_useful_work_and_lists_slots_first(backend):
    _run_with(
        backend,
        [
            ("Retiring", 40.0, "%"),
            ("Retiring.Light_Operations", 30.0, "%"),
            ("Retiring.Light_Operations.Memory_Operations", 25.0, "%"),
            ("Retiring.Heavy_Operations", 10.0, "%"),
            ("Backend_Bound", 20.0, "% Slots"),
            ("Backend_Bound.Core_Bound.Divider", 90.0, "% Clocks"),
        ],
    )
    assert [r["metric_name"] for r in backend.query_bottlenecks(min_percentage=1.0)] == [
        "Backend_Bound",
        "Retiring.Heavy_Operations",
        "Backend_Bound.Core_Bound.Divider",
    ]


def test_mux_never_resolves_as_a_node(backend):
    _run_with(backend, [("MUX", 3.0, "%"), ("Frontend_Bound", 20.0, "%")])
    assert backend.resolve_node_names("MUX") == []
    assert backend.resolve_node_names("MUX", MATCH_SUBSTRING) == []


def test_node_queries_honour_labels_and_time_window(backend):
    from datetime import datetime, timedelta, timezone

    _run_with(backend, [("Backend_Bound", 40.0, "%")], labels={"test_name": "keep"})
    _run_with(backend, [("Backend_Bound", 50.0, "%")], labels={"test_name": "other"})
    _run_with(
        backend,
        [("Backend_Bound", 60.0, "%")],
        labels={"test_name": "keep"},
        started_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    by_label = backend.query_by_bottleneck("Backend_Bound", min_pct=1.0, labels={"test_name": "keep"})
    assert sorted(r["avg_value"] for r in by_label) == [40.0, 60.0]
    recent = backend.query_by_bottleneck("Backend_Bound", min_pct=1.0, last_hours=24)
    assert sorted(r["avg_value"] for r in recent) == [40.0, 50.0]
    both = backend.query_bottlenecks(min_percentage=1.0, labels={"test_name": "keep"}, last_hours=24)
    assert [r["avg_value"] for r in both] == [40.0]


def test_process_filter_and_dotted_label_keys(backend):
    run = Run(process_name="other-server", level=4, labels={"build.variant": "x"})
    backend.insert_run(run)
    backend.insert_samples(
        [Sample(run_id=run.run_id, timestamp=1.0, metric_name="Backend_Bound", value=40.0, unit="%")]
    )
    _run_with(backend, [("Backend_Bound", 50.0, "%")])
    assert [r["avg_value"] for r in backend.query_bottlenecks(process_name="redis-server")] == [50.0]
    assert [r["avg_value"] for r in backend.query_by_bottleneck("Backend_Bound", labels={"build.variant": "x"})] == [
        40.0
    ]


def test_time_window_is_exact(backend):
    from datetime import datetime, timedelta, timezone

    _run_with(backend, [("Backend_Bound", 40.0, "%")], started_at=datetime.now(timezone.utc) - timedelta(hours=30))
    _run_with(backend, [("Backend_Bound", 50.0, "%")], started_at=datetime.now(timezone.utc) - timedelta(hours=20))
    assert [r["avg_value"] for r in backend.query_by_bottleneck("Backend_Bound", last_hours=24)] == [50.0]
    assert [r.duration_seconds for r in backend.list_runs(last_hours=24)] == [0.0]


def test_limit_cuts_ties_by_name_then_run(backend):
    runs = [_run_with(backend, [("Backend_Bound", 10.0, "%")]) for _ in range(3)]
    (row,) = backend.query_bottlenecks(min_percentage=1.0, limit=1)
    assert row["run_id"] == min(r.run_id for r in runs)


def test_limit_cuts_ties_by_name_before_run(backend):
    # Grouping emits rows in run order; the name must still win the tie.
    for run_id, name in (
        ("00000000-0000-4000-8000-000000000001", "Frontend_Bound"),
        ("ffffffff-ffff-4fff-bfff-ffffffffffff", "Backend_Bound"),
    ):
        backend.insert_run(Run(run_id=run_id, process_name="redis-server", level=1, labels={}))
        backend.insert_samples([Sample(run_id=run_id, timestamp=1.0, metric_name=name, value=10.0, unit="%")])
    (row,) = backend.query_bottlenecks(min_percentage=1.0, limit=1)
    assert row["metric_name"] == "Backend_Bound"


def test_equal_values_are_ordered_by_name_then_run(backend):
    runs = [_run_with(backend, [("Frontend_Bound", 10.0, "%"), ("Backend_Bound", 10.0, "%")]) for _ in range(2)]
    rows = backend.query_bottlenecks(min_percentage=1.0)
    expected = [(n, rid) for n in ("Backend_Bound", "Frontend_Bound") for rid in sorted(r.run_id for r in runs)]
    assert [(r["metric_name"], r["run_id"]) for r in rows] == expected


def test_label_keys_with_quotes_and_backslashes(backend):
    run = Run(process_name="redis-server", level=1, labels={'a"b': "x", "c\\d": "y", "e.f": "z"})
    backend.insert_run(run)
    backend.insert_samples(
        [Sample(run_id=run.run_id, timestamp=1.0, metric_name="Backend_Bound", value=40.0, unit="%")]
    )
    for labels in ({'a"b': "x"}, {"c\\d": "y"}, {"e.f": "z"}):
        assert len(backend.query_by_bottleneck("Backend_Bound", labels=labels)) == 1
        assert len(backend.list_runs(labels=labels)) == 1


@pytest.mark.parametrize("hours", [1e12, float("inf"), float("nan")])
def test_huge_or_non_finite_time_window_means_everything(backend, hours):
    _run_with(backend, [("Backend_Bound", 40.0, "%")])
    assert len(backend.query_by_bottleneck("Backend_Bound", last_hours=hours)) == 1
    assert len(backend.list_runs(last_hours=hours)) == 1


def test_enormous_averages_do_not_overflow_ordering(backend):
    _run_with(backend, [("Backend_Bound", 1e300, "%"), ("Frontend_Bound", 40.0, "%")])
    assert [r["metric_name"] for r in backend.query_bottlenecks(min_percentage=1.0)] == [
        "Backend_Bound",
        "Frontend_Bound",
    ]


def test_nan_samples_are_ignored(backend):
    if isinstance(backend, SQLiteBackend):
        pytest.skip("SQLite cannot store NaN (it becomes NULL)")
    _run_with(backend, [("Backend_Bound", 40.0, "%"), ("Backend_Bound", float("nan"), "%")])
    rows = backend.query_by_bottleneck("Backend_Bound", min_pct=1.0)
    assert [r["avg_value"] for r in rows] == [40.0]


def test_ties_use_byte_order_on_both_backends(backend):
    # "A_b" < "Ab" in byte order, the reverse in most locale collations.
    _run_with(backend, [("Ab", 10.0, "%"), ("A_b", 10.0, "%")])
    assert [r["metric_name"] for r in backend.query_bottlenecks(min_percentage=1.0)] == ["A_b", "Ab"]
