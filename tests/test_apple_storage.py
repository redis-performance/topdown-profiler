"""Native Apple documents must retain precision, provenance and separation from slot TMA."""

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from topdown.apple_storage import (SOC_KIND, STAT_KIND, collect_apple_result, query_apple_result,
                                   store_apple_result, stored_apple_hotspots, validate_result)
from topdown.collector.apple import (AppleProfileError, FIXED_EVENTS, M1_EVENTS, M1_FETCH_EVENTS,
                                     M1_MAP_EVENTS, MODES, TMA_ROOTS, parse_apple_bottlenecks, parse_profile)
from topdown.collector.apple_trace import parse_trace_profile
from topdown.storage.models import Run, Sample
from topdown.storage.sqlite_backend import SQLiteBackend

DATA = Path(__file__).parent / "test_data"


@pytest.fixture
def apple_artifact(tmp_path):
    def write(kind="fixed", capture=False):
        common = {"tma_available": False, "tma": dict.fromkeys(TMA_ROOTS)}
        if kind == "fixed":
            result = parse_profile(DATA / "apple_fixed_profile_m6.xml", "counters-profile", 3362, fixed_events=FIXED_EVENTS)
            result["measurement_kind"] = "native-fixed-counter-samples"
            result["samples"][0]["event_values"]["FIXED_INSTRUCTIONS"] = 2**64 - 1
            name = "profile.json"
        elif kind == "stat":
            start = datetime.now(timezone.utc)
            result = {**common, "measurement_kind": STAT_KIND, "scope": {"pid": 42, "descendants": False},
                      "command": ["/usr/bin/true"], "started_at": start.isoformat(),
                      "ended_at": (start + timedelta(seconds=2)).isoformat(), "status": "completed", "exit_code": 0,
                      "counters": {"instructions": 2**64 - 1, "cycles": 2**53 + 1},
                      "environment": {"arch": "arm64", "os_version": "27.0.1"}}
            name = "stat.json"
        elif kind == "soc":
            channel = json.loads((DATA / "apple_soc_dram_stall_m6.json").read_text())
            result = {**common, "measurement_kind": SOC_KIND, "scope": "system-and-cluster",
                      "channels": [channel], "channel_count": 1, "status": "completed", "exit_code": 0}
            name = "soc.json"
        elif kind == "trace":
            result = parse_trace_profile(DATA / "apple_processor_intervals_m6.xml",
                                         DATA / "apple_processor_gaps_m6.xml", 42)
            result["measurement_kind"] = "hardware-traced-execution-intervals"
            name = "profile.json"
        elif kind == "guided":
            from tests.test_apple_bottlenecks import metric_xml
            xml = tmp_path / "guided.xml"
            xml.write_text(metric_xml())
            result = parse_apple_bottlenecks(xml, 42)
            result["measurement_kind"] = "apple-guided-bottleneck-intervals"
            name = "profile.json"
        elif kind in {"cpu", "time", "counters"}:
            from tests.test_apple import xml_table
            xml = tmp_path / f"{kind}.xml"
            xml.write_text(xml_table(MODES[kind][1]))
            result = parse_profile(xml, MODES[kind][1], 42)
            result["measurement_kind"] = MODES[kind][2]
            name = "profile.json"
        else:
            from tests.test_apple_m1_mode import synthetic_m1_xml
            xml = tmp_path / f"{kind}.xml"
            synthetic_m1_xml(xml)
            events = {"m1": M1_EVENTS, "m1-map": M1_MAP_EVENTS, "m1-fetch": M1_FETCH_EVENTS}[kind]
            result = parse_profile(xml, "counters-profile", 3362, fixed_events=events)
            result["measurement_kind"] = MODES[kind][2]
            name = "profile.json"
        folder = tmp_path / kind
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_text(json.dumps(result))
        if capture:
            start = datetime.now(timezone.utc)
            (folder / "capture.json").write_text(json.dumps({"started_at": start.isoformat(),
                "ended_at": (start + timedelta(seconds=3)).isoformat(), "command": ["/tmp/workload", "arg"],
                "environment": {"cpu": "Apple M6", "arch": "arm64"}, "status": "exported"}))
        return path, result
    return write


@pytest.mark.parametrize("kind", ["fixed", "stat", "soc", "trace", "guided", "cpu", "time", "counters", "m1", "m1-map", "m1-fetch"])
def test_native_documents_round_trip_without_linux_metric_rows(sqlite_backend, apple_artifact, kind):
    path, result = apple_artifact(kind)
    run = store_apple_result(sqlite_backend, path.parent, labels={"test_name": "apple-control", "collector": "fake"})
    document = sqlite_backend.get_apple_result(run.run_id)
    assert document["result"] == result
    assert document["metadata"]["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert run.labels["collector"] == "apple" and run.level == 0
    assert run.system_wide == (kind == "soc")
    assert sqlite_backend.list_runs(labels={"test_name": "apple-control"})[0].run_id == run.run_id
    assert sqlite_backend.get_samples(run.run_id) == []
    assert sqlite_backend.query_bottlenecks() == []
    assert sqlite_backend.get_apple_result("missing") is None
    if kind == "fixed":
        assert query_apple_result(sqlite_backend, run.run_id, "samples", 0, 1)["rows"][0]["event_values"]["FIXED_INSTRUCTIONS"] == 2**64 - 1
        assert stored_apple_hotspots(sqlite_backend, run.run_id)["hotspots"][0]["function"] == "alu_work"
    if kind == "stat":
        assert run.duration_seconds == 2 and run.process_name == "true"
        assert document["metadata"]["time_basis"] == "result_timestamps"
        assert document["result"]["counters"]["cycles"] == 2**53 + 1
    if kind == "trace":
        page = query_apple_result(sqlite_backend, run.run_id, "gaps")
        assert page["rows"] == result["gaps"] and page["result"]["coverage_complete"] is False


def test_capture_metadata_and_import_time_are_distinguished(sqlite_backend, apple_artifact):
    path, _ = apple_artifact(capture=True)
    run = store_apple_result(sqlite_backend, path)
    assert run.process_name == "workload" and run.duration_seconds == 3
    assert run.labels["cpu"] == "Apple M6" and run.labels["time_basis"] == "capture_metadata"
    assert sqlite_backend.get_apple_result(run.run_id)["metadata"]["capture"]["status"] == "exported"
    (path.parent / "capture.json").unlink()
    second = store_apple_result(sqlite_backend, path, process_name="redis")
    assert second.labels["time_basis"] == "import_time" and second.process_name == "redis"
    assert second.run_id != run.run_id


@pytest.mark.parametrize("kind", ["stat", "fixed"])
def test_offset_timestamps_keep_recent_filters_and_ordering_in_utc(sqlite_backend, apple_artifact, kind):
    now = datetime.now(timezone.utc)
    runs = []
    for age, offset in [(timedelta(minutes=5), -7), (timedelta(hours=5), 12)]:
        path, result = apple_artifact(kind, capture=kind == "fixed")
        metadata_path = path if kind == "stat" else path.parent / "capture.json"
        times = json.loads(metadata_path.read_text())
        stamp = (now - age).astimezone(timezone(timedelta(hours=offset)))
        times.update(started_at=stamp.isoformat(), ended_at=(stamp + timedelta(seconds=2)).isoformat())
        metadata_path.write_text(json.dumps(times))
        run = store_apple_result(sqlite_backend, path)
        runs.append(run)
        assert run.started_at == stamp and run.started_at.utcoffset() == timedelta(0)
        assert run.ended_at.utcoffset() == timedelta(0) and run.duration_seconds == 2
        document = sqlite_backend.get_apple_result(run.run_id)
        stored_times = document["result"] if kind == "stat" else document["metadata"]["capture"]
        assert stored_times["started_at"] == times["started_at"]
        if kind == "fixed":
            assert document["result"] == result
    assert [r.run_id for r in sqlite_backend.list_runs()] == [r.run_id for r in runs]
    assert [r.run_id for r in sqlite_backend.list_runs(last_hours=1)] == [runs[0].run_id]


def test_queries_paginate_and_keep_native_scope_and_metadata(sqlite_backend, apple_artifact):
    path, original = apple_artifact()
    run = store_apple_result(sqlite_backend, path)
    summary = query_apple_result(sqlite_backend, run.run_id)
    assert "samples" not in summary["result"] and "leaf_sample_counts" not in summary["result"]
    assert summary["result"]["scope"] == original["scope"]
    page = query_apple_result(sqlite_backend, run.run_id, "samples", 1, 1)
    assert page["total"] == 3 and page["rows"] == original["samples"][1:2]
    assert query_apple_result(sqlite_backend, run.run_id, "samples", 100)["rows"] == []
    for section, offset, limit in [("bad", 0, 1), ("samples", -1, 1), ("samples", 0, 0), ("samples", 0, 1001)]:
        with pytest.raises(AppleProfileError):
            query_apple_result(sqlite_backend, run.run_id, section, offset, limit)
    with pytest.raises(AppleProfileError, match="no channels"):
        query_apple_result(sqlite_backend, run.run_id, "channels")
    with pytest.raises(AppleProfileError, match="No stored"):
        query_apple_result(sqlite_backend, "missing")
    with pytest.raises(AppleProfileError, match="No stored"):
        stored_apple_hotspots(sqlite_backend, "missing")


def test_native_insert_rolls_back_parent_run_on_failure(sqlite_backend, apple_artifact):
    path, _ = apple_artifact()
    sqlite_backend._get_conn().execute("""CREATE TRIGGER reject_apple BEFORE INSERT ON apple_results
        BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []
    sqlite_backend.insert_run(Run(process_name="still-usable"))
    assert len(sqlite_backend.list_runs()) == 1


def test_initialize_adds_native_table_without_changing_linux_data(tmp_path, apple_artifact):
    backend = SQLiteBackend(tmp_path / "migration.db")
    backend.initialize()
    backend.insert_run(Run(run_id="linux", process_name="redis"))
    backend.insert_samples([Sample(run_id="linux", metric_name="Retiring", value=30)])
    backend._get_conn().execute("DROP TABLE apple_results")
    backend.close()

    backend.initialize()
    path, _ = apple_artifact()
    native = store_apple_result(backend, path)
    assert backend.get_run(native.run_id)
    assert backend.get_samples("linux")[0].value == 30
    backend._get_conn().execute("DELETE FROM runs WHERE run_id = ?", (native.run_id,))
    backend._get_conn().commit()
    assert backend.get_apple_result(native.run_id) is None
    backend.close()


@pytest.mark.parametrize("labels,process", [({"bad": 2}, None), ([], None), (None, ""), (None, 42)])
def test_invalid_labels_and_names_do_not_insert(sqlite_backend, apple_artifact, labels, process):
    path, _ = apple_artifact()
    with pytest.raises(AppleProfileError):
        store_apple_result(sqlite_backend, path, process, labels)
    assert sqlite_backend.list_runs() == []


@pytest.mark.parametrize("change", [
    {"tma_available": True}, {"tma": {"Retiring": 30}}, {"measurement_kind": "unknown"},
    {"schema": "MetricTable"}, {"sample_count": True}, {"sample_count": 0}, {"samples": []},
    {"environment": {"bad": float("nan")}},
])
def test_bad_profile_and_nonfinite_json_are_rejected_before_insert(sqlite_backend, apple_artifact, change):
    path, result = apple_artifact()
    path.write_text(json.dumps({**result, **change}))
    with pytest.raises(AppleProfileError):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []


@pytest.mark.parametrize("content", [None, "not-json", "[]"])
def test_invalid_artifact_file(sqlite_backend, tmp_path, content):
    path = tmp_path / "stat.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(AppleProfileError):
        store_apple_result(sqlite_backend, path)


def test_ambiguous_capture_directory_and_bad_dates(sqlite_backend, apple_artifact):
    path, result = apple_artifact("stat")
    (path.parent / "profile.json").write_text("{}")
    with pytest.raises(AppleProfileError, match="explicitly"):
        store_apple_result(sqlite_backend, path.parent)
    for timestamp in ["yesterday", "2026-10-22T12:00:00", None]:
        path.write_text(json.dumps({**result, "ended_at": timestamp or "2020-01-01T00:00:00+00:00"}))
        with pytest.raises(AppleProfileError):
            store_apple_result(sqlite_backend, path)


@pytest.mark.parametrize("kind", ["stat", "soc", "trace", "guided"])
def test_invalid_native_payloads(apple_artifact, kind):
    _, result = apple_artifact(kind)
    invalid = deepcopy(result)
    if kind == "stat":
        invalid["counters"]["instructions"] = True
    elif kind == "soc":
        invalid["channels"][0]["states"][0]["value"] = -1
    elif kind == "trace":
        invalid["coverage_complete"] = True
    else:
        invalid["apple_bottlenecks"]["shares"]["useful"] = 5
    with pytest.raises(AppleProfileError):
        validate_result(invalid)


@pytest.mark.parametrize("kind,field,value", [
    ("fixed", ("scope", "pid"), True),
    ("fixed", ("scope", "start_ns"), -1),
    ("fixed", ("scope", "end_ns"), 0),
    ("fixed", ("excluded_rows",), True),
    ("fixed", ("samples", 0, "pid"), 9999),
    ("fixed", ("samples", 0, "time_ns"), -1),
    ("fixed", ("samples", 0, "tid"), True),
    ("fixed", ("samples", 0, "core"), -1),
    ("fixed", ("samples", 0, "weight"), 1.5),
    ("fixed", ("samples", 0, "frames"), None),
    ("fixed", ("samples", 0, "frames", 0, "name"), 3),
    ("fixed", ("samples", 0, "event_values", "FIXED_CYCLES"), float(2**53 + 1)),
    ("fixed", ("samples", 0, "event_values", "FIXED_INSTRUCTIONS"), 2**64),
    ("fixed", ("configured_events",), ["UNKNOWN"]),
    ("fixed", ("event_value_order_verified",), False),
    ("m1", ("samples", 0, "native_event_values"), [1]),
    ("m1-map", ("samples", 0, "native_event_values", 0), True),
    ("m1-fetch", ("samples", 0, "native_event_values", 0), -1),
    ("counters", ("samples", 0, "value"), float(2**53 + 1)),
    ("counters", ("samples", 0, "event"), " "),
    ("counters", ("samples", 0, "sample_index"), -1),
    ("time", ("samples", 0, "weight"), False),
    ("trace", ("intervals", 0), {}),
    ("trace", ("intervals", 0, "tid"), 0),
    ("trace", ("intervals", 0, "self_cycles"), 0.5),
    ("trace", ("intervals", 0, "self_instructions"), 2**64),
    ("trace", ("intervals", 0, "self_duration_ns"), 2**64 - 1),
    ("trace", ("intervals", 0, "duration_ns"), 2**64 - 1),
    ("trace", ("intervals", 0, "binary_offset"), -1),
    ("trace", ("gaps", 0), None),
    ("trace", ("gaps", 0, "pid"), 999),
    ("trace", ("gaps", 0, "time_ns"), 2**64),
    ("trace", ("gaps", 0, "description"), False),
    ("guided", ("samples", 0, "cycles"), 0),
    ("guided", ("samples", 0, "duration_ns"), -1),
    ("guided", ("samples", 0, "shares", "useful"), 0),
    ("guided", ("samples", 0, "shares", "useful"), 10**1000),
    ("guided", ("apple_bottlenecks", "cycles"), 0),
    ("guided", ("apple_bottlenecks", "shares", "delivery"), 0.405),
    ("guided", ("apple_bottlenecks_by_core", 0, "core"), 7),
    ("guided", ("apple_bottlenecks_by_core", 0, "cycles"), 999),
    ("guided", ("invalid_cycles_with_known_count",), -1),
    ("guided", ("invalid_cycles_with_known_count",), 100),
    ("guided", ("invalid_intervals_without_cycle_count",), 1),
    ("guided", ("valid_cycle_fraction_of_scoped_intervals",), 0.5),
    ("guided", ("valid_cycle_fraction_of_scoped_intervals",), None),
])
def test_corrupt_native_observations_are_rejected_before_storage(sqlite_backend, apple_artifact, kind, field, value):
    path, result = apple_artifact(kind)
    parent = result
    for part in field[:-1]:
        parent = parent[part]
    parent[field[-1]] = value
    path.write_text(json.dumps(result))
    with pytest.raises(AppleProfileError):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []
    assert sqlite_backend._get_conn().execute("SELECT COUNT(*) FROM apple_results").fetchone()[0] == 0


def test_guided_import_retains_consistent_partial_cycle_coverage(apple_artifact):
    _, result = apple_artifact("guided")
    result.update(invalid_intervals=1, invalid_cycles_with_known_count=100,
                  valid_cycle_fraction_of_scoped_intervals=0.8)
    validate_result(result)
    result["valid_cycle_fraction_of_scoped_intervals"] = 0.9
    with pytest.raises(AppleProfileError, match="cycle coverage"):
        validate_result(result)
    result["invalid_cycles_with_known_count"] = 2**64
    with pytest.raises(AppleProfileError, match="coverage counts"):
        validate_result(result)


def test_valid_zero_counters_and_unresolved_frames_are_retained(sqlite_backend, apple_artifact):
    path, result = apple_artifact()
    result["samples"][0].update(weight=0, tid=0, core=0, frames=[])
    result["samples"][0]["event_values"] = dict.fromkeys(FIXED_EVENTS, 0)
    result["samples"][1]["frames"] = [{"name": None, "address": None, "binary": None}]
    path.write_text(json.dumps(result))
    run = store_apple_result(sqlite_backend, path)
    assert sqlite_backend.get_apple_result(run.run_id)["result"] == result


@pytest.mark.parametrize("kind", ["fixed", "cpu", "time", "counters", "guided", "trace"])
def test_native_observations_must_stay_in_the_selected_window(sqlite_backend, apple_artifact, kind):
    path, result = apple_artifact(kind)
    rows = result["intervals"] if kind == "trace" else result["samples"]
    field = "start_ns" if kind == "trace" else "time_ns"
    first = rows[0][field]
    result["scope"]["end_ns"] = first
    path.write_text(json.dumps(result))
    with pytest.raises(AppleProfileError, match="PID/time scope"):
        store_apple_result(sqlite_backend, path)
    if kind == "trace":
        result["scope"]["end_ns"] = first + rows[0]["duration_ns"] - 1
        path.write_text(json.dumps(result))
        with pytest.raises(AppleProfileError, match="complete native trace intervals"):
            store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []


def test_trace_zero_duration_and_empty_gaps_are_valid(sqlite_backend, apple_artifact):
    path, result = apple_artifact("trace")
    result.update(gaps=[], gap_count=0)
    for row in result["intervals"]:
        row["gap_markers_inside"] = 0
    result["intervals"][0].update(duration_ns=0, self_duration_ns=None)
    path.write_text(json.dumps(result))
    run = store_apple_result(sqlite_backend, path)
    assert sqlite_backend.get_apple_result(run.run_id)["result"] == result


def test_guided_boundaries_and_summed_cycles_above_uint64_are_valid(sqlite_backend, tmp_path):
    from tests.test_apple_bottlenecks import metric_xml
    xml = tmp_path / "guided.xml"
    xml.write_text(metric_xml().replace("<fixed-decimal>100</fixed-decimal>", f"<fixed-decimal>{2**64 - 1}</fixed-decimal>")
                   .replace("<fixed-decimal>300</fixed-decimal>", f"<fixed-decimal>{2**64 - 1}</fixed-decimal>"))
    path = tmp_path / "profile.json"
    for end in [150, 250]:
        result = parse_apple_bottlenecks(xml, 42, end_ns=end)
        result["measurement_kind"] = "apple-guided-bottleneck-intervals"
        assert result["samples"][-1]["time_ns"] + result["samples"][-1]["duration_ns"] > end
        path.write_text(json.dumps(result))
        run = store_apple_result(sqlite_backend, path)
        assert sqlite_backend.get_apple_result(run.run_id)["result"] == result
    assert result["apple_bottlenecks"]["cycles"] > 2**64


@pytest.mark.parametrize("mode,command,pid,duration", [
    ("stat", None, None, 1), ("stat", ["/bin/true"], 1, 1), ("stat", [], None, 1),
    ("stat", None, 0, 1), ("soc", ["/bin/true"], None, 1), ("fixed", None, 42, 1),
    ("fixed", ["/bin/true"], None, 0.5), ("unknown", None, None, 1),
    ("stat", None, 42, float("nan")), ("stat", None, 42, 301),
])
def test_collect_rejects_ambiguous_or_unbounded_requests_before_launch(tmp_path, mode, command, pid, duration):
    with pytest.raises(AppleProfileError):
        collect_apple_result(tmp_path / "capture", mode, command, pid, duration)
    assert not (tmp_path / "capture").exists()


def test_collect_dispatch_and_failed_capture_artifact_retention(tmp_path, monkeypatch):
    calls = []
    def fake(output, *args):
        output.mkdir()
        calls.append(args)
        return {"exit_code": 0}
    monkeypatch.setattr("topdown.collector.apple_stat.stat_process", fake)
    monkeypatch.setattr("topdown.collector.apple_stat.stat_pid", fake)
    monkeypatch.setattr("topdown.collector.apple_soc.sample_soc", fake)
    monkeypatch.setattr("topdown.collector.apple.record_profile", fake)
    for i, (mode, command, pid, name) in enumerate([
        ("stat", ["/bin/true"], None, "stat.json"), ("stat", None, 42, "stat.json"),
        ("soc", None, None, "soc.json"), ("fixed", ["/bin/true"], None, "profile.json"),
    ]):
        path = collect_apple_result(tmp_path / str(i), mode, command, pid, 1)
        assert path.name == name and path.parent.is_dir()
    assert calls == [(["/bin/true"], 1), (42, 1), (1,), (["/bin/true"], 1, "fixed")]
    def failed(output, *args):
        output.mkdir()
        return {"exit_code": 5, "status": "timeout"}
    monkeypatch.setattr("topdown.collector.apple_stat.stat_process", failed)
    with pytest.raises(AppleProfileError, match="timeout"):
        collect_apple_result(tmp_path / "failed", "stat", ["/bin/true"], None, 1)
    assert (tmp_path / "failed").is_dir()


def test_postgresql_atomic_method_rolls_back_and_keeps_integer_json():
    from topdown.storage.postgresql_backend import PostgreSQLBackend
    backend = PostgreSQLBackend("unused")
    conn = MagicMock()
    backend._conn = conn
    conn.closed = False
    document = {"result": {"measurement_kind": STAT_KIND, "counters": {"instructions": 2**64 - 1}}}
    run = Run()
    assert backend.insert_apple_result(run, document) == run.run_id
    payload = conn.execute.call_args_list[1].args[1][2]
    assert json.loads(payload)["result"]["counters"]["instructions"] == 2**64 - 1
    conn.commit.assert_called_once()
    conn.execute.side_effect = [None, RuntimeError("insert failed")]
    with pytest.raises(RuntimeError):
        backend.insert_apple_result(Run(), document)
    conn.rollback.assert_called_once()
    conn.execute.side_effect = None
    conn.execute.return_value.fetchone.return_value = {"document": document}
    assert backend.get_apple_result(run.run_id) == document
    assert backend.get_apple_result("invalid uuid") is None
    conn.execute.return_value.fetchone.return_value = None
    assert backend.get_apple_result(run.run_id) is None


def test_cli_import_query_report_and_missing_runs(tmp_path, monkeypatch, apple_artifact):
    from typer.testing import CliRunner
    from topdown.cli import app

    monkeypatch.setenv("TOPDOWN_BACKEND", "sqlite")
    db = str(tmp_path / "cli.db")
    path, result = apple_artifact(capture=True)
    runner = CliRunner()
    imported = runner.invoke(app, ["apple", "store", str(path.parent), "--db", db,
                                  "--process", "redis", "--label", "test_name=stored"])
    assert imported.exit_code == 0, imported.output
    run_id = json.loads(imported.output)["run_id"]
    page = runner.invoke(app, ["apple", "query", run_id, "--db", db, "--section", "samples", "--limit", "1"])
    assert page.exit_code == 0, page.output
    assert json.loads(page.output)["rows"] == result["samples"][:1]
    report = runner.invoke(app, ["apple", "report", "--run-id", run_id, "--db", db, "--json"])
    assert report.exit_code == 0, report.output
    assert json.loads(report.output)["hotspots"][0]["function"] == "alu_work"
    terminal = runner.invoke(app, ["apple", "report", "--run-id", run_id, "--db", db])
    assert terminal.exit_code == 0 and "alu_work" in terminal.output
    for args in [["query", "missing", "--db", db], ["report", "--run-id", "missing", "--db", db],
                 ["report"], ["report", str(path), "--run-id", run_id],
                 ["store", str(path), "--db", db, "--label", "bad"]]:
        error = runner.invoke(app, ["apple", *args])
        assert error.exit_code == 1 and "Error:" in error.output
    backend = SQLiteBackend(Path(db))
    backend.initialize()
    assert len(backend.list_runs(labels={"test_name": "stored"})) == 1
    backend.close()

    listing = runner.invoke(app, ["list", "--db", db, "--label", "test_name=stored"])
    assert listing.exit_code == 0 and "Apple" in listing.output
    native_query = runner.invoke(app, ["query", "--run-id", run_id, "--db", db, "--funnel"])
    assert native_query.exit_code == 0 and "topdown apple query" in native_query.output
    comparison = runner.invoke(app, ["compare", run_id, run_id, "--db", db])
    assert comparison.exit_code == 0 and "unavailable" in comparison.output
    for args in [["query", "--run-id", run_id, "--json"], ["query", "--csv"],
                 ["query", "--funnel", "--json"], ["compare", run_id, run_id, "--json"]]:
        unsupported = runner.invoke(app, [*args, "--db", db])
        assert unsupported.exit_code == 1 and unsupported.stdout == ""
        assert "topdown apple query" in unsupported.stderr


@pytest.mark.parametrize("kind", ["stat", "soc", "fixed"])
def test_cli_capture_can_store_without_modifying_source(tmp_path, monkeypatch, apple_artifact, kind):
    from typer.testing import CliRunner
    from topdown.cli import app

    monkeypatch.setenv("TOPDOWN_BACKEND", "sqlite")
    path, native = apple_artifact(kind)
    called = MagicMock(return_value=native)
    if kind == "fixed":
        monkeypatch.setattr("topdown.apple_cli.record_profile", called)
        args = ["record", "--mode", "fixed"]
    else:
        monkeypatch.setattr(f"topdown.collector.apple_{kind}.{'stat_process' if kind == 'stat' else 'sample_soc'}", called)
        args = [kind]
    runner = CliRunner()
    options = ["--output", str(path.parent), "--store", "--label", "test_name=captured", "--db", str(tmp_path / "capture.db")]
    target = [] if kind == "soc" else ["--", "/bin/true"]
    bad = runner.invoke(app, ["apple", *args, *options, "--label", "bad", *target])
    assert bad.exit_code == 1
    called.assert_not_called()
    result = runner.invoke(app, ["apple", *args, *options, *target])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["run_id"]
    assert "run_id" not in json.loads(path.read_text())


def test_cli_failed_capture_and_storage_keep_artifacts(tmp_path, monkeypatch, apple_artifact):
    from typer.testing import CliRunner
    from topdown.cli import app

    monkeypatch.setenv("TOPDOWN_BACKEND", "sqlite")
    path, native = apple_artifact("stat")
    capture = MagicMock(return_value={**native, "exit_code": 5, "status": "timeout"})
    monkeypatch.setattr("topdown.collector.apple_stat.stat_process", capture)
    runner = CliRunner()
    db = tmp_path / "failed.db"
    args = ["apple", "stat", "--output", str(path.parent), "--store", "--db", str(db), "--", "/bin/true"]
    failed = runner.invoke(app, args)
    assert failed.exit_code == 5 and not db.exists() and path.exists()
    capture.return_value = native
    monkeypatch.setattr("topdown.storage.get_backend", MagicMock(side_effect=OSError("database offline")))
    failed = runner.invoke(app, args)
    assert failed.exit_code == 1 and "artifacts retained" in failed.output and path.exists()
    no_store = runner.invoke(app, ["apple", "stat", "--output", str(path.parent), "--db", str(db), "--", "/bin/true"])
    assert no_store.exit_code == 1 and "Use --store" in no_store.output


def test_mcp_native_import_query_and_common_run_discovery(tmp_path, monkeypatch, apple_artifact):
    from topdown import mcp_server as server
    from topdown.config import TopdownConfig

    monkeypatch.setattr(server, "get_config", lambda: TopdownConfig(db_path=tmp_path / "mcp.db"))
    path, native = apple_artifact(capture=True)
    imported = server.import_apple_result(str(path), "redis-apple", {"test_name": "mcp"})
    assert "error" not in imported
    run_id = imported["run_id"]
    assert imported["labels"]["cpu"] == "Apple M6"
    assert server.query_apple_result(run_id, "samples", 0, 1)["rows"] == native["samples"][:1]
    assert server.get_apple_hotspots(run_id)["hotspots"][0]["function"] == "alu_work"
    assert "Apple native-fixed-counter-samples" in server.list_profiling_runs(labels={"test_name": "mcp"})
    assert "query_apple_result" in server.query_bottlenecks(process_name="redis-apple")
    for response in [server.get_funnel(run_id=run_id), server.compare_funnel(run_id, run_id),
                     server.compare_runs(run_id, run_id), server.compare_by_labels({"test_name": "mcp"}, {"test_name": "mcp"})]:
        assert "query_apple_result" in response and "unavailable" in response
    assert "error" in server.query_apple_result("missing")
    assert "error" in server.get_apple_hotspots("missing")
    assert "error" in server.import_apple_result(str(path.parent / "missing"))
    monkeypatch.setattr("topdown.collector.apple.doctor", lambda: {"arch": "arm64"})
    assert server.apple_doctor() == {"arch": "arm64"}


def test_mcp_collection_stores_success_and_retains_failure_artifacts(tmp_path, monkeypatch, apple_artifact):
    from topdown import mcp_server as server
    from topdown.config import TopdownConfig

    config = TopdownConfig(db_path=tmp_path / "mcp.db")
    monkeypatch.setattr(server, "get_config", lambda: config)
    path, native = apple_artifact("stat")
    def collect(output, *args):
        output.mkdir()
        (output / "stat.json").write_text(json.dumps(native))
        return output / "stat.json"
    collector = MagicMock(side_effect=collect)
    monkeypatch.setattr("topdown.apple_storage.collect_apple_result", collector)
    result = server.collect_apple_profile(command=["/bin/true"], labels={"test_name": "mcp-capture"})
    assert result["result"]["counters"]["instructions"] == 2**64 - 1
    assert Path(result["artifacts"]).is_dir()
    assert server.query_apple_result(result["run_id"])["labels"]["test_name"] == "mcp-capture"
    invalid = server.collect_apple_profile(command=["/bin/true"], labels={"bad": 2})
    assert "error" in invalid and collector.call_count == 1
    collector.side_effect = AppleProfileError("no counters")
    failed = server.collect_apple_profile(command=["/bin/true"])
    assert "no counters" in failed["error"] and "artifacts" in failed
    collector.side_effect = collect
    monkeypatch.setattr(server, "_get_backend", MagicMock(side_effect=RuntimeError("DB down")))
    failed = server.collect_apple_profile(command=["/bin/true"])
    assert failed["error"] == "DB down" and (Path(failed["artifacts"]) / path.name).is_file()


@pytest.mark.asyncio
async def test_mcp_native_tools_registered_on_both_transports(tmp_path, monkeypatch, apple_artifact):
    from mcp.server.fastmcp import FastMCP
    from topdown import mcp_server as server
    from topdown.config import TopdownConfig

    monkeypatch.setattr(server, "get_config", lambda: TopdownConfig(db_path=tmp_path / "protocol.db"))
    http = FastMCP("test-http")
    server._register_tools_and_resources(http)
    expected = {"apple_doctor", "import_apple_result", "collect_apple_profile", "query_apple_result", "get_apple_hotspots"}
    for instance in [server.mcp, http]:
        assert expected <= {t.name for t in await instance.list_tools()}
        path, _ = apple_artifact("stat")
        # FastMCP's protocol serializer must retain integers above the JS safe-integer range.
        content = await instance.call_tool("import_apple_result", {"path": str(path)})
        blocks = content[0] if isinstance(content, tuple) else content
        payload = json.loads(blocks[0].text)
        assert payload["result"]["counters"]["instructions"] == 2**64 - 1
