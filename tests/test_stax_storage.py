"""Sampling storage transactions, denominators and CLI/MCP routing."""

import json
import sqlite3

import pytest
from typer.testing import CliRunner

from tests.test_stax_archive import archive_document
from topdown.cli import app
from topdown.stax_archive import StaxError
from topdown.stax_storage import (
    get_stax_hotspots,
    get_stax_target_spans,
    get_stax_thread_waits,
    query_stax_result,
    store_stax_result,
)
from topdown.storage.models import Run


@pytest.fixture
def stax_archive(tmp_path):
    path = tmp_path / "redis.stax"
    path.write_text(json.dumps(archive_document()))
    return path


def test_atomic_labeled_result_and_cascade(sqlite_backend, stax_archive):
    backend = sqlite_backend
    run = store_stax_result(backend, stax_archive, labels={"test_name": "stax", "collector": "toplev"})
    assert run.labels["collector"] == "stax" and run.level == 0
    assert run.duration_seconds == 1
    assert run.process_name == "redis-server"
    assert backend.list_runs(labels={"test_name": "stax"})[0].run_id == run.run_id
    result = backend.get_stax_result(run.run_id)["result"]
    assert result["samples"][0]["pmc"]["cycles"] == 2**64 - 1
    assert result["scope"]["started_at_unix_ns"] == 1700000000000000001
    assert backend.get_samples(run.run_id) == []
    assert backend.get_apple_result(run.run_id) is None
    assert backend.query_bottlenecks() == []
    backend._get_conn().execute("DELETE FROM runs WHERE run_id = ?", (run.run_id,))
    backend._get_conn().commit()
    assert backend.get_stax_result(run.run_id) is None


def test_failed_document_rolls_back_run(sqlite_backend, stax_archive):
    conn = sqlite_backend._get_conn()
    conn.execute(
        "CREATE TRIGGER reject_stax BEFORE INSERT ON stax_results BEGIN SELECT RAISE(ABORT, 'test rejection'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="test rejection"):
        store_stax_result(sqlite_backend, stax_archive)
    assert sqlite_backend.list_runs() == []
    sqlite_backend.insert_run(Run(process_name="connection-usable"))


def test_hotspots_do_not_double_count_recursive_frames(sqlite_backend, stax_archive):
    run = store_stax_result(sqlite_backend, stax_archive)
    report = get_stax_hotspots(sqlite_backend, run.run_id)
    assert report["unit"] == "samples" and report["denominator_samples"] == 1
    hot, caller = report["rows"]
    assert hot["name"] == "hot" and hot["self_samples"] == 1 and hot["total_samples"] == 1
    assert caller["total_samples"] == 1 and caller["self_samples"] == 0
    assert hot["self_sample_share"] == 1
    assert get_stax_hotspots(sqlite_backend, run.run_id, tid=43)["rows"] == []
    kernel = get_stax_hotspots(sqlite_backend, run.run_id, stack="kernel")
    assert kernel["rows"][0]["address"] == 9012


def test_waits_retain_waker_and_units(sqlite_backend, stax_archive):
    run = store_stax_result(sqlite_backend, stax_archive)
    report = get_stax_thread_waits(sqlite_backend, run.run_id)
    assert report["unit"] == "ns"
    assert report["rows"][0]["duration_ns"] == 30
    assert report["rows"][0]["waker_tid"] == 43
    assert get_stax_thread_waits(sqlite_backend, run.run_id, tid=43)["rows"] == []


def test_query_summary_and_pages(sqlite_backend, stax_archive):
    run = store_stax_result(sqlite_backend, stax_archive)
    summary = query_stax_result(sqlite_backend, run.run_id)
    assert "samples" not in summary["result"]
    assert summary["counts"]["samples"] == 1
    page = query_stax_result(sqlite_backend, run.run_id, "intervals", 1, 1)
    assert page["total"] == 2 and page["rows"][0]["kind"] == "OffCpu"
    assert query_stax_result(sqlite_backend, run.run_id, "samples", 999)["rows"] == []


@pytest.mark.parametrize(
    "options", [{"section": "bad"}, {"offset": -1}, {"limit": 0}, {"limit": 1001}, {"limit": True}]
)
def test_bad_page_options(sqlite_backend, options):
    with pytest.raises(StaxError):
        query_stax_result(sqlite_backend, "missing", **options)


@pytest.mark.parametrize(
    "function,options",
    [
        (get_stax_hotspots, {"tid": True}),
        (get_stax_hotspots, {"stack": "bad"}),
        (get_stax_hotspots, {"sort": "bad"}),
        (get_stax_thread_waits, {"tid": -1}),
    ],
)
def test_bad_report_options(sqlite_backend, function, options):
    with pytest.raises(StaxError):
        function(sqlite_backend, "missing", **options)


def test_missing_and_other_collector_runs(sqlite_backend):
    with pytest.raises(StaxError, match="not found"):
        query_stax_result(sqlite_backend, "missing")
    run = Run()
    sqlite_backend.insert_run(run)
    with pytest.raises(StaxError, match="not a Stax"):
        query_stax_result(sqlite_backend, run.run_id)
    run.labels["collector"] = "stax"
    sqlite_backend._get_conn().execute(
        "UPDATE runs SET labels = ? WHERE run_id = ?", (json.dumps(run.labels), run.run_id)
    )
    sqlite_backend._get_conn().commit()
    with pytest.raises(StaxError, match="missing"):
        query_stax_result(sqlite_backend, run.run_id)


def test_cli_import_query_and_tma_guards(tmp_path, stax_archive):
    runner = CliRunner()
    db = str(tmp_path / "cli.db")
    imported = runner.invoke(app, ["stax", "import", str(stax_archive), "--db", db, "-L", "test_name=cli"])
    assert imported.exit_code == 0, imported.output
    run_id = json.loads(imported.stdout)["run_id"]
    for args in (["stax", "query", run_id], ["stax", "hotspots", run_id], ["stax", "waits", run_id]):
        result = runner.invoke(app, [*args, "--db", db])
        assert result.exit_code == 0 and json.loads(result.stdout)["run_id"] == run_id
    result = runner.invoke(app, ["query", "--run-id", run_id, "--json", "--db", db])
    assert result.exit_code == 1 and "Stax" in result.stderr and result.stdout == ""


def test_mcp_import_and_queries(monkeypatch, tmp_path, stax_archive):
    from topdown import mcp_server as server

    monkeypatch.setenv("TOPDOWN_DB_PATH", str(tmp_path / "mcp.db"))
    imported = server.import_stax_result(str(stax_archive), labels={"test_name": "mcp"})
    assert "error" not in imported
    run_id = imported["run_id"]
    assert server.query_stax_result(run_id)["counts"]["samples"] == 1
    assert server.get_stax_hotspots(run_id)["rows"][0]["name"] == "hot"
    assert server.get_stax_thread_waits(run_id)["rows"][0]["duration_ns"] == 30
    assert "Stax" in server.list_profiling_runs(last_hours=1e9)
    assert "slot-TMA" in server._apple_run_guidance(Run(labels={"collector": "stax"}))
    assert "error" in server.query_stax_result("missing")
    assert "error" in server.import_stax_result(str(tmp_path / "missing.stax"))


def test_cli_bad_archive_error(tmp_path):
    result = CliRunner().invoke(app, ["stax", "import", str(tmp_path / "absent.stax"), "--db", str(tmp_path / "db")])
    assert result.exit_code == 1 and "Error:" in result.stderr


def test_synthetic_stack_records_never_enter_cpu_denominator(sqlite_backend, stax_archive):
    doc = archive_document()
    synthetic = 4293918720
    sample = doc["aggregator"]["threads"][0]["pet_samples"][0]
    doc["aggregator"]["threads"].append(
        {
            "tid": synthetic,
            "pet_samples": [sample, sample],
            "wakeups": [],
            "intervals": [
                {
                    "start_ns": 100,
                    "end_ns": 200,
                    "kind": {"SyntheticSpan": {"stack": [1012], "origin_tid": 42, "lane_kind": "Metal"}},
                }
            ],
        }
    )
    stax_archive.write_text(json.dumps(doc))
    run = store_stax_result(sqlite_backend, stax_archive)
    assert get_stax_hotspots(sqlite_backend, run.run_id)["denominator_samples"] == 1
    assert get_stax_hotspots(sqlite_backend, run.run_id, tid=synthetic)["denominator_samples"] == 0
    report = get_stax_target_spans(sqlite_backend, run.run_id)
    row = report["rows"][0]
    assert row["lane_kind"] == "Metal" and row["sum_target_span_ns"] == 100
    assert row["origin_tids"] == [42] and row["leaf"]["name"] == "hot"
    assert get_stax_target_spans(sqlite_backend, run.run_id, tid=42)["rows"] == []


def test_companion_settings_are_verified_and_retained(sqlite_backend, stax_archive):
    doc = archive_document()
    capture = {
        "collector": "stax",
        "pid": 42,
        "frequency_hz": 99,
        "run": doc["runs"][0],
        "command": ["/opt/redis-custom"],
    }
    metadata_path = stax_archive.parent / "capture.json"
    metadata_path.write_text(json.dumps(capture))
    run = store_stax_result(sqlite_backend, stax_archive)
    assert run.process_name == "redis-custom"
    assert run.labels["sampling_frequency_hz"] == "99"
    result = query_stax_result(sqlite_backend, run.run_id)
    assert result["metadata"]["capture"] == capture
    assert result["metadata"]["capture_settings_status"] == "recorded"
    assert result["metadata"]["artifacts"][-1]["path"] == str(metadata_path.resolve())
    stax_archive.unlink()
    metadata_path.unlink()
    assert get_stax_hotspots(sqlite_backend, run.run_id)["rows"][0]["name"] == "hot"


@pytest.mark.parametrize(
    "change",
    [
        {"collector": "apple"},
        {"pid": 43},
        {"frequency_hz": True},
        {"frequency_hz": 0},
        {"run": {"id": [2], "target_pid": 42, "started_at_unix_ns": 1}},
    ],
)
def test_mismatched_companion_metadata_is_not_stored(sqlite_backend, stax_archive, change):
    capture = {"collector": "stax", "pid": 42, "frequency_hz": 99, **change}
    (stax_archive.parent / "capture.json").write_text(json.dumps(capture))
    with pytest.raises(StaxError):
        store_stax_result(sqlite_backend, stax_archive)
    assert sqlite_backend.list_runs() == []


def test_public_selected_legacy_surrogates_are_rejected(sqlite_backend, stax_archive):
    run = store_stax_result(sqlite_backend, stax_archive)
    document = sqlite_backend.get_stax_result(run.run_id)
    document["result"]["frame_table"][0]["name"] = "\ud800"
    copy_run = Run(labels={"collector": "stax"})
    sqlite_backend.insert_stax_result(copy_run, document)
    assert query_stax_result(sqlite_backend, copy_run.run_id)["counts"]["samples"] == 1
    with pytest.raises(StaxError, match="Unicode"):
        query_stax_result(sqlite_backend, copy_run.run_id, "samples")
    with pytest.raises(StaxError, match="Unicode"):
        get_stax_hotspots(sqlite_backend, copy_run.run_id)
