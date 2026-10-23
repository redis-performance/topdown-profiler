"""Timeline counters retain native units and distinguish absence from measured zero."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from topdown.apple_gpu_report import render, summarize
from topdown.apple_storage import query_apple_result, store_apple_result, stored_apple_gpu_report
from topdown.collector import apple_gpu
from tests import test_apple_gpu_replay as replay_tests

fake_gpudebug = replay_tests.fake_gpudebug

EVIDENCE = json.loads((Path(__file__).resolve().parents[1] / "docs/apple-gpu-m6-stream-granularity.json").read_text())
BACKGROUND = json.loads((Path(__file__).resolve().parents[1] / "docs/apple-gpu-m6-background-scope.json").read_text())


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: str(case["trial"]))
def test_measured_timeline_units_and_unobserved_leaves(case):
    profile = case["profile"]
    result = summarize(profile)
    metrics = {row["id"]: row for row in result["timeline_metrics"]}
    assert metrics["gpu_bandwidth"]["native_value"].endswith(" GiB / Second")
    assert metrics["vs_occupancy"]["native_value"] is None
    assert metrics["fs_occupancy"]["observed"] is False
    assert "stalled launching" in metrics["compute_shader_launch_limiter"]["description"]
    text = render(result)
    assert "GPU Bandwidth:" in text and "VS Occupancy:" not in text
    assert profile["timeline"]["Cores"] == "12 sampled / 12 total"
    assert case["probe"]["checksum_verified"] and case["probe"]["elements_verified"]


def test_observed_zero_is_not_treated_as_unavailable():
    profile = deepcopy(EVIDENCE["cases"][0]["profile"])
    entry = profile["timeline_counter_groups"]["occupancy"]
    entry["listing"]["children"][0]["values"] = [{"type": "string", "value": "0,00%"}]
    entry["counters"]["vs_occupancy"] = {
        "Name": "VS Occupancy", "Value": "0,00% Percentage of Shader Core Resources",
        "Scope": "timeline", "Description": "Vertex shader resources"}
    result = summarize(profile)
    assert "VS Occupancy: 0,00% Percentage of Shader Core Resources" in render(result)


def test_timeline_report_order_survives_database_json_object_reordering():
    profile = deepcopy(EVIDENCE["cases"][0]["profile"])
    expected = summarize(profile)
    profile["timeline_counter_groups"] = dict(reversed(list(profile["timeline_counter_groups"].items())))
    assert summarize(profile) == expected


@pytest.mark.parametrize("broken", [None, "groups", "leaves"])
def test_collect_discovers_all_groups_and_skips_info_for_unobserved_leaves(tmp_path, fake_gpudebug, monkeypatch, broken):
    native_run = apple_gpu.subprocess.run
    groups = EVIDENCE["cases"][0]["profile"]["timeline_counter_groups"]
    context = [None]
    def run(command, **kwargs):
        instruction = command[-1]
        if instruction == "list --all" and fake_gpudebug["timeline_opened"]:
            result = groups[context[0]]["listing"] if context[0] else {
                "children": [{"name": name} for name in groups], "totalCount": len(groups)}
            if (broken == "groups" and context[0] is None) or (broken == "leaves" and context[0]):
                result = {"children": [], "totalCount": 1}
        elif instruction.startswith("go /performance/timeline/counters/"):
            context[0] = instruction.rsplit("/", 1)[1]
            result = groups[context[0]]["listing"]
        elif instruction.startswith("info /performance/timeline/counters/"):
            name = instruction.rsplit("/", 1)[1]
            result = groups[context[0]]["counters"][name]
            assert result is not None, "queried a synthetic zero for an unobserved counter"
        else:
            return native_run(command, **kwargs)
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
    monkeypatch.setattr(apple_gpu.subprocess, "run", run)
    trace = tmp_path / "stream.gputrace"
    trace.mkdir()
    if broken:
        with pytest.raises(ValueError, match="Incomplete"):
            apple_gpu.collect(trace, tmp_path / "capture")
        assert fake_gpudebug["commands"][-1][-2:] == ["--terminate", "42"]
        assert not (tmp_path / "capture/profile.json").exists()
        return
    profile = apple_gpu.collect(trace, tmp_path / "capture")
    assert profile["timeline_counter_groups"] == groups
    assert "GPU Bandwidth:" in render(summarize(profile))


@pytest.mark.parametrize("mutation", [
    lambda entry: entry.update(listing={"children": [], "totalCount": 1}),
    lambda entry: entry["listing"].update(totalCount=True),
    lambda entry: entry["listing"]["children"].append(entry["listing"]["children"][0]),
    lambda entry: entry["counters"].pop("vs_occupancy"),
    lambda entry: entry["counters"].update(vs_occupancy={"Value": "0%"}),
    lambda entry: entry["counters"]["kernel_occupancy"].update(Scope="encoder"),
    lambda entry: entry["counters"]["kernel_occupancy"].update(Value=None),
    lambda entry: entry["listing"]["children"][2].update(values=[{"type": "string", "value": 0}]),
])
def test_timeline_counter_scope_coverage_and_native_units_are_validated(mutation):
    profile = deepcopy(EVIDENCE["cases"][0]["profile"])
    mutation(profile["timeline_counter_groups"]["occupancy"])
    with pytest.raises(ValueError):
        summarize(profile)


@pytest.mark.parametrize("groups", [None, [], {"occupancy": None}, {"../../": {}}])
def test_malformed_timeline_groups_fail_cleanly(groups):
    profile = deepcopy(EVIDENCE["cases"][0]["profile"])
    profile["timeline_counter_groups"] = groups
    with pytest.raises(ValueError):
        summarize(profile)


def check_native_timeline_evidence(backend, tmp_path):
    for case in [*EVIDENCE["cases"], *BACKGROUND["cases"]]:
        profile = case["profile"]
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        run = store_apple_result(backend, path)
        assert backend.get_apple_result(run.run_id)["result"] == profile
        assert query_apple_result(backend, run.run_id)["result"]["timeline_counter_groups"] == profile["timeline_counter_groups"]
        assert stored_apple_gpu_report(backend, run.run_id) == {"run_id": run.run_id, **summarize(profile, 20)}


def test_sqlite_preserves_native_timeline_evidence(sqlite_backend, tmp_path):
    check_native_timeline_evidence(sqlite_backend, tmp_path)


def test_stream_experiment_reproduces_the_measured_sources():
    import hashlib
    import runpy

    root = Path(__file__).resolve().parents[1]
    source = (root / EVIDENCE["base_source"]).read_text()
    script = runpy.run_path(str(root / "examples/apple_gpu/stream_granularity.py"))
    assert hashlib.sha256(source.encode()).hexdigest() == EVIDENCE["base_source_sha256"]
    assert [n * 4 for n in script["ORDER"]] == EVIDENCE["order_floats_per_thread"]
    replication = EVIDENCE["public_runner_replication"]
    assert hashlib.sha256((root / replication["source"]).read_bytes()).hexdigest() == replication["source_sha256"]
    assert replication["order_floats_per_thread"] == EVIDENCE["order_floats_per_thread"]
    assert len(replication["cases"]) == 8
    for case, observed in zip(replication["cases"], [9, 9, 9, 9, 9, 13, 11, 13]):
        assert case["probe"]["checksum_verified"] and case["probe"]["elements_verified"]
        assert case["timeline"]["Cores"] == "12 sampled / 12 total"
        assert len([row for row in case["timeline_metrics"] if row["observed"]]) == observed
    for case in EVIDENCE["cases"]:
        variant = script["variant_source"](source, case["float4_vectors_per_thread"])
        assert hashlib.sha256(variant.encode()).hexdigest() == case["source_sha256"]
    with pytest.raises(ValueError, match="Choose"):
        script["variant_source"](source, True)
    with pytest.raises(ValueError, match="changed"):
        script["variant_source"]("a different probe", 4)


def test_cli_and_mcp_expose_native_timeline_units_and_availability(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from topdown import mcp_server
    from topdown.cli import app
    from topdown.config import TopdownConfig

    profile = EVIDENCE["cases"][0]["profile"]
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    cli = CliRunner().invoke(app, ["apple", "gpu", "report", str(path)])
    assert cli.exit_code == 0 and "Timeline counters: 9 observed / 14 listed" in cli.stdout
    assert "GPU Bandwidth: 123,82 GiB / Second" in cli.stdout and "VS Occupancy:" not in cli.stdout
    monkeypatch.setattr(mcp_server, "get_config", lambda: TopdownConfig(db_path=tmp_path / "native.db"))
    imported = mcp_server.import_apple_result(str(path))
    assert "error" not in imported, imported
    run_id = imported["run_id"]
    summary = mcp_server.query_apple_result(run_id)
    assert summary["result"]["timeline_counter_groups"] == profile["timeline_counter_groups"]
    report = mcp_server.get_apple_gpu_report(run_id)
    assert report["timeline_metrics"] == summarize(profile)["timeline_metrics"]
    assert mcp_server.query_apple_result(run_id, "encoders", 0, 1)["rows"] == profile["encoders"][:1]


def test_native_background_control_keeps_timeline_scope_and_quality_visible():
    import hashlib

    root = Path(__file__).resolve().parents[1]
    assert hashlib.sha256((root / BACKGROUND["source"]).read_bytes()).hexdigest() == BACKGROUND["source_sha256"]
    assert BACKGROUND["background"]["completed"] and BACKGROUND["background"]["commands"] == 23021
    for case in BACKGROUND["cases"]:
        profile = case["profile"]
        report = summarize(profile)
        assert {row["counters"]["Encoder type"] for row in profile["encoders"]} == {"Compute"}
        assert len(profile["encoders"]) == 4
        assert report["timeline_counter_scope"] == {"kind": "gpu-timeline", "target_exclusive": False}
        fragment = next(row for row in report["timeline_metrics"] if row["id"] == "fs_occupancy")
        if case["case"] == "background":
            assert fragment["native_value"] == "19,46% Percentage of Shader Core Resources" and fragment["observed"]
            assert "2 sampled / 12 total" in render(report)
        else:
            assert fragment["native_value"] is None and fragment["observed"] is False
        assert "other device activity may contribute" in render(report)
        assert "Consistent state: yes" in render(report)
