"""Stax v3 replay, native units and hostile archive boundaries."""

import copy
import json
import os
from pathlib import Path

import pytest

from topdown.stax_archive import StaxError, load_archive


def archive_document():
    sample = {
        "timestamp_ns": 120,
        "stack": [1012, 1022, 1013],
        "kernel_stack": [9012],
        "pmc": {"cycles": 2**64 - 1, "instructions": 2**53 + 1, "l1d_misses": 0, "branch_mispreds": 0},
    }
    run = {
        "id": [1],
        "state": "Stopped",
        "stop_reason": "TimeLimit",
        "started_at_unix_ns": 1700000000000000001,
        "stopped_at_unix_ns": 1700000001000000001,
        "target_pid": 42,
        "label": "/usr/bin/redis-server",
        "pet_samples": 1,
        "off_cpu_intervals": 1,
    }
    binary = {
        "path": "/usr/bin/redis-server",
        "base_avma": 1000,
        "avma_end": 1100,
        "text_svma": 2000,
        "arch": "aarch64",
        "is_executable": True,
        "text_bytes": None,
        "symbols": [
            {"start_svma": 2010, "end_svma": 2020, "name": list(b"hot")},
            {"start_svma": 2020, "end_svma": 2030, "name": list(b"caller")},
        ],
    }
    off_cpu = {
        "start_ns": 120,
        "end_ns": 150,
        "kind": {"OffCpu": {"stack": [1022], "waker_tid": 43, "waker_user_stack": [1012]}},
    }
    thread = {
        "tid": 42,
        "pet_samples": [sample],
        "intervals": [{"start_ns": 100, "end_ns": 130, "kind": "OnCpu"}, off_cpu],
        "wakeups": [{"timestamp_ns": 150, "waker_tid": 43, "waker_user_stack": [1012], "waker_kernel_stack": []}],
    }
    return {
        "format_version": 3,
        "saved_at_unix_ns": 1700000001000000010,
        "provenance": {"producer": "stax-server", "producer_version": "0.1.0", "os": "linux", "arch": "aarch64"},
        "runs": [run],
        "aggregator": {
            "session_start_ns": 100,
            "last_event_ns": 200,
            "thread_names": [{"tid": 42, "name": "main"}],
            "threads": [thread],
            "markers": [],
        },
        "binaries": {"binaries": [binary]},
        "target_ingest": {"spans_recorded": 0},
        "observability": {"events": []},
        "events": [],
        "blobs": [],
    }


def archive_events(document):
    events = [{"ArchiveSaved": {"saved_at_unix_ns": document["saved_at_unix_ns"]}}]
    events.extend({"RunSummary": {"run": run}} for run in document["runs"])
    events.append(
        {"AggregatorClock": {key: document["aggregator"][key] for key in ("session_start_ns", "last_event_ns")}}
    )
    events.extend({"ThreadName": row} for row in document["aggregator"]["thread_names"])
    events.extend({"BinaryLoaded": {"binary": binary}} for binary in document["binaries"]["binaries"])
    for thread in document["aggregator"]["threads"]:
        for field, variant, key in (
            ("pet_samples", "PetSample", "sample"),
            ("intervals", "Interval", "interval"),
            ("wakeups", "Wakeup", "wakeup"),
        ):
            events.extend({variant: {"tid": thread["tid"], key: row}} for row in thread[field])
    events.append({"TargetIngestDiagnostics": {"diagnostics": document["target_ingest"]}})
    return events


@pytest.fixture
def stax_archive(tmp_path):
    path = tmp_path / "redis.stax"
    path.write_text(json.dumps(archive_document()))
    return path


def test_native_addresses_counters_and_interval_units(stax_archive):
    doc = load_archive(stax_archive)
    result = doc["result"]
    assert result["tma_available"] is False
    assert result["samples"][0]["pmc"]["cycles"] == 2**64 - 1
    table = {f["address"]: f for f in result["frame_table"]}
    frames = [table[a] for a in result["stack_table"][result["samples"][0]["stack_id"]]]
    assert [frame["name"] for frame in frames] == ["hot", "caller", "hot"]
    assert frames[0]["address"] == 1012 and frames[0]["symbol_address"] == 1010
    assert table[result["stack_table"][result["samples"][0]["kernel_stack_id"]][0]]["name"] is None
    assert result["threads"][0]["on_cpu_interval_ns"] == 30
    assert result["threads"][0]["off_cpu_interval_ns"] == 30
    assert doc["metadata"]["artifacts"][0]["size_bytes"] == stax_archive.stat().st_size


def test_events_override_stale_aggregates(stax_archive):
    doc = archive_document()
    doc["events"] = archive_events(doc)
    doc["aggregator"]["threads"] = []
    doc["binaries"] = {"binaries": []}
    stax_archive.write_text(json.dumps(doc))
    result = load_archive(stax_archive)
    assert result["metadata"]["ingestion"] == "event-replay"
    assert result["result"]["frame_table"][0]["name"] == "hot"


@pytest.mark.parametrize("event_log", [False, True])
def test_directory_and_manifest_import(tmp_path, event_log):
    doc = archive_document()
    files = {key: key + ".json" for key in ("aggregator", "binaries", "target_ingest", "observability")}
    for key, filename in files.items():
        (tmp_path / filename).write_text(json.dumps(doc[key]))
    manifest = {key: doc[key] for key in ("format_version", "saved_at_unix_ns", "provenance", "runs")}
    manifest["files"] = files
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    if event_log:
        (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(row) for row in archive_events(doc)))
        (tmp_path / files["aggregator"]).unlink()
    a = load_archive(tmp_path)
    b = load_archive(tmp_path / "manifest.json")
    assert a == b
    assert a["result"]["frame_table"][0]["name"] == "hot"


def test_target_spans_do_not_inflate_cpu_time(stax_archive):
    doc = archive_document()
    doc["aggregator"]["threads"].append(
        {
            "tid": 4293918720,
            "pet_samples": [doc["aggregator"]["threads"][0]["pet_samples"][0]],
            "intervals": [
                {
                    "start_ns": 100,
                    "end_ns": 200,
                    "kind": {"SyntheticSpan": {"stack": [1012], "origin_tid": 42, "lane_kind": "Metal"}},
                }
            ],
            "wakeups": [],
        }
    )
    stax_archive.write_text(json.dumps(doc))
    result = load_archive(stax_archive)["result"]
    assert result["threads"][0]["on_cpu_interval_ns"] == 30
    assert result["threads"][1]["target_span_ns"] == 100
    assert result["threads"][1]["sample_count"] == 0
    assert result["counts"]["cpu_samples"] == 1
    assert result["counts"]["target_stack_samples"] == 1
    assert result["samples"][1]["kind"] == "target-span-stack"


def test_open_interval_uses_last_event(stax_archive):
    doc = archive_document()
    doc["aggregator"]["threads"][0]["intervals"][0]["end_ns"] = 0
    stax_archive.write_text(json.dumps(doc))
    interval = load_archive(stax_archive)["result"]["intervals"][0]
    assert interval["open_at_save"] is True and interval["duration_ns"] == 100


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(format_version=2),
        lambda d: d.update(format_version=True),
        lambda d: d.update(runs=[]),
        lambda d: d["runs"].append(copy.deepcopy(d["runs"][0])),
        lambda d: d["runs"][0].update(started_at_unix_ns=-1),
        lambda d: d["runs"][0].update(stopped_at_unix_ns=1),
        lambda d: d["runs"][0].update(target_pid=True),
        lambda d: d["aggregator"]["threads"].append(copy.deepcopy(d["aggregator"]["threads"][0])),
        lambda d: d["aggregator"]["threads"][0]["pet_samples"][0].update(timestamp_ns=1.5),
        lambda d: d["aggregator"]["threads"][0]["pet_samples"][0].update(stack=[2**64]),
        lambda d: d["aggregator"]["threads"][0]["pet_samples"][0]["pmc"].update(cycles=-1),
        lambda d: d["aggregator"]["threads"][0]["intervals"][0].update(end_ns=99),
        lambda d: d["aggregator"]["threads"][0]["intervals"][0].update(kind="Unknown"),
        lambda d: d["binaries"]["binaries"][0].update(avma_end=1),
        lambda d: d["binaries"]["binaries"][0]["symbols"][0].update(name=[256]),
        lambda d: d.update(events=[{"FutureEvent": {}}]),
    ],
)
def test_invalid_native_fields_rejected(stax_archive, mutation):
    doc = archive_document()
    mutation(doc)
    stax_archive.write_text(json.dumps(doc))
    with pytest.raises(StaxError):
        load_archive(stax_archive)


@pytest.mark.parametrize(
    "text", ['{"format_version":3,"format_version":3}', '{"x":NaN}', '{"x":1e999}', '{"x":"\\ud800"}', "[]"]
)
def test_invalid_json(stax_archive, text):
    stax_archive.write_text(text)
    with pytest.raises(StaxError):
        load_archive(stax_archive)


@pytest.mark.parametrize("symlink", [False, True])
def test_manifest_cannot_escape_directory(tmp_path, symlink):
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    name = "../outside.json"
    if symlink:
        (root / "escape.json").symlink_to(outside)
        name = "escape.json"
    doc = archive_document()
    doc["files"] = {"aggregator": name}
    (root / "manifest.json").write_text(json.dumps(doc))
    with pytest.raises(StaxError, match="escapes"):
        load_archive(root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO")
def test_fifo_import_never_waits(tmp_path):
    path = tmp_path / "fifo.stax"
    os.mkfifo(path)
    with pytest.raises(StaxError, match="regular"):
        load_archive(path)


def test_real_v3_excerpt_separates_native_and_synthetic_samples():
    doc = load_archive(Path(__file__).parent / "test_data/stax_v3_linux_target_excerpt.json")
    result = doc["result"]
    assert result["counts"]["cpu_samples"] == 1
    assert result["counts"]["target_stack_samples"] == 1
    assert result["quality"]["cpu_sample_counts_match"] is True
    assert doc["metadata"]["provenance"]["os"] == "linux"
    span = next(row for row in result["intervals"] if row["kind"] == "SyntheticSpan")
    assert span["lane_kind"] == "Metal"
    assert span["duration_ns"] == 1722000
