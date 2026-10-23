"""GPU replay collection, native persistence, CLI and MCP share the same artifacts."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess

from mcp.server.fastmcp import FastMCP
import pytest
from typer.testing import CliRunner

from topdown import apple_gpu_report, mcp_server
from topdown.apple_storage import query_apple_result, store_apple_result, stored_apple_gpu_report, validate_result
from topdown.cli import app
from topdown.collector import apple_gpu
from topdown.collector.apple import AppleProfileError, TMA_ROOTS
from topdown.config import TopdownConfig
from tests.test_apple_gpu_replay import native_profile


def write_profile(output, *args, **kwargs):
    output.mkdir(parents=True)
    result = native_profile()
    result["encoders"][0]["counters"]["Compute Shader Launch Limiter"] = "105.14%"
    (output / "profile.json").write_text(json.dumps(result))
    return result


def check_round_trip(backend, tmp_path):
    path = tmp_path / "profile.json"
    original = native_profile()
    original["encoders"][0]["counters"]["Compute Shader Launch Limiter"] = "105.14%"
    path.write_text(json.dumps(original))
    run = store_apple_result(backend, path, labels={"device": "fake", "processor": "cpu", "test_name": "gpu"})
    assert run.labels["device"] == "Apple M6" and run.labels["processor"] == "gpu"
    assert run.labels["measurement_kind"] == apple_gpu.GPU_KIND
    assert backend.get_apple_result(run.run_id)["result"] == original
    assert backend.get_samples(run.run_id) == [] and backend.query_bottlenecks() == []
    assert backend.list_runs(labels={"test_name": "gpu"})[0].run_id == run.run_id
    assert "encoders" not in query_apple_result(backend, run.run_id)["result"]
    page = query_apple_result(backend, run.run_id, "encoders", 0, 1)
    assert page["rows"] == original["encoders"] and page["total"] == 1
    assert page["rows"][0]["counters"]["Instructions"] == "18446744073709551615"
    assert query_apple_result(backend, run.run_id, "encoders", 1, 1)["rows"] == []
    report = stored_apple_gpu_report(backend, run.run_id, 1)
    assert report["encoders"][0]["limiters"][0]["exceeds_100"]
    assert report["timeline"] == original["timeline"]
    assert "get_apple_gpu_report" in mcp_server._apple_run_guidance(run)
    return run


def test_gpu_sqlite_round_trip(sqlite_backend, tmp_path):
    check_round_trip(sqlite_backend, tmp_path)


@pytest.mark.parametrize("mutation", [
    {"device": None}, {"settings": {"execution": []}}, {"scope": None}, {"tma_available": True},
    {"tma": {"Retiring": 100}}, {"encoders": []}, {"timeline": {"GPU time": 1}},
])
def test_gpu_import_rejects_invalid_native_metadata(mutation):
    result = native_profile()
    result.update(mutation)
    with pytest.raises(AppleProfileError):
        validate_result(result)


@pytest.mark.parametrize("mutation", [
    {"id": "../../etc"}, {"counters": {"Cost": "NaN%", "Duration": "1 ms"}},
    {"counters": {"Cost": "100%", "Duration": None}},
    {"counters": {"Cost": "100%", "Duration": "-1 ms"}},
    {"counters": {"Cost": "100%", "Duration": "1 ms", "Instructions": 18446744073709551615}},
])
def test_gpu_import_rejects_malformed_encoder_values(mutation):
    result = native_profile()
    result["encoders"][0].update(mutation)
    with pytest.raises(AppleProfileError):
        validate_result(result)


def test_report_pagination_preserves_full_profile_costs_and_order():
    result = native_profile()
    first = result["encoders"][0]
    first["counters"]["Cost"] = "20%"
    second = deepcopy(first)
    second["id"], second["counters"]["Cost"] = "ce1", "80%"
    result["encoders"].append(second)
    report = apple_gpu_report.summarize(result, limit=1)
    assert report["total_encoders"] == 2 and report["encoders"][0]["id"] == "ce1"
    assert report["encoders"][0]["cost_percent"] == 80
    assert apple_gpu_report.summarize(result, 1, 1)["encoders"][0]["cost_percent"] == 20
    assert apple_gpu_report.summarize(result, 1, 2)["encoders"] == []
    result["encoders"].append(first)
    with pytest.raises(ValueError, match="unique"):
        apple_gpu_report.summarize(result)


@pytest.mark.parametrize("limit,offset", [(0, 0), (1001, 0), (True, 0), (1, -1), (1, True)])
def test_report_rejects_invalid_pagination(limit, offset):
    with pytest.raises(ValueError):
        apple_gpu_report.summarize(native_profile(), limit, offset)


@pytest.mark.parametrize("options", [
    {"mode": "idle"}, {"mode": "bad"}, {"passes": 0}, {"passes": True}, {"items": 0},
    {"items": 16777217}, {"passes": 500, "items": 16777216}, {"execution": "bad"},
    {"mode": "stream", "items": 129},
])
def test_invalid_record_options_are_rejected_before_host_or_output(tmp_path, monkeypatch, options):
    monkeypatch.setattr(apple_gpu, "_require_host", lambda: pytest.fail("host checked before arguments"))
    with pytest.raises(ValueError):
        apple_gpu.record(tmp_path / "unused", **options)
    assert not (tmp_path / "unused").exists()


def test_record_requires_apple_host(tmp_path, monkeypatch):
    monkeypatch.setattr(apple_gpu.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="Apple silicon"):
        apple_gpu.record(tmp_path / "unused")


@pytest.fixture
def fake_capture(monkeypatch):
    state = {"commands": [], "failure": None}
    monkeypatch.setattr(apple_gpu, "_require_host", lambda: None)
    monkeypatch.setattr(apple_gpu, "collect", lambda trace, output, execution, gpu_state: write_profile(output))

    def run(command, **kwargs):
        state["commands"].append((command, kwargs))
        compile_step = command[0] == "xcrun"
        if state["failure"] == "timeout":
            raise subprocess.TimeoutExpired(command, 90, output=b"partial", stderr=b"error")
        if state["failure"] == "command":
            return subprocess.CompletedProcess(command, 1, "", "compiler unavailable")
        if not compile_step:
            path = Path(command[command.index("--result") + 1])
            path.write_text(json.dumps({"checksum_verified": state["failure"] != "checksum",
                "mode": command[command.index("--mode") + 1], "passes": int(command[1]),
                "items": int(command[command.index("--items") + 1])}))
        return subprocess.CompletedProcess(command, 0, "native output", "")
    monkeypatch.setattr(apple_gpu.subprocess, "run", run)
    return state


def test_record_packages_probe_checks_checksum_and_retains_provenance(tmp_path, fake_capture):
    output = tmp_path / "capture"
    result = apple_gpu.record(output, "alu", 8, 128, "serial")
    assert result["probe"]["checksum_verified"] and result["source_sha256"]
    assert result["started_at"] <= result["ended_at"]
    compile_command, capture_command = fake_capture["commands"]
    assert Path(compile_command[0][-3]).is_file()
    assert capture_command[1]["env"]["MTL_CAPTURE_ENABLED"] == "1"
    assert json.loads((output / "profile.json").read_text()) == result
    assert (output / "compile.command.json").is_file() and (output / "capture.stdout").is_file()
    validate_result(result)


@pytest.mark.parametrize("failure", ["command", "timeout", "checksum"])
def test_capture_failures_keep_artifacts_without_publishing_profile(tmp_path, fake_capture, failure):
    fake_capture["failure"] = failure
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="artifacts"):
        apple_gpu.record(output)
    assert not (output / "profile.json").exists() and list(output.glob("*.stdout"))


def test_gpu_cli_record_query_report_and_import(tmp_path, monkeypatch):
    monkeypatch.setattr(apple_gpu, "record", write_profile)
    runner, db = CliRunner(), tmp_path / "gpu.db"
    output = tmp_path / "capture"
    result = runner.invoke(app, ["apple", "gpu", "record", "--output", str(output), "--store", "--db", str(db),
                                 "--label", "test_name=gpu-cli"])
    assert result.exit_code == 0, result.output
    run_id = json.loads(result.stdout)["run_id"]
    page = runner.invoke(app, ["apple", "query", run_id, "--section", "encoders", "--limit", "1", "--db", str(db)])
    assert page.exit_code == 0 and json.loads(page.stdout)["total"] == 1
    for source in [[str(output)], ["--run-id", run_id, "--db", str(db)]]:
        report = runner.invoke(app, ["apple", "gpu", "report", *source])
        assert report.exit_code == 0, report.output
        assert "Control Flow Limiter" in report.stdout and "exceeds 100%" in report.stdout
        as_json = runner.invoke(app, ["apple", "gpu", "report", *source, "--json"])
        assert json.loads(as_json.stdout)["total_encoders"] == 1
    query = runner.invoke(app, ["query", "--run-id", run_id, "--db", str(db)])
    assert query.exit_code == 0 and "apple gpu report" in query.stdout
    imported = runner.invoke(app, ["apple", "store", str(output), "--db", str(db)])
    assert imported.exit_code == 0 and json.loads(imported.stdout)["run_id"] != run_id


def test_gpu_cli_profiles_existing_trace(tmp_path, monkeypatch):
    trace = tmp_path / "saved.gputrace"
    trace.mkdir()
    calls = []
    def collect(path, output, execution, gpu_state):
        calls.append((path, execution))
        return write_profile(output)
    monkeypatch.setattr(apple_gpu, "collect", collect)
    result = CliRunner().invoke(app, ["apple", "gpu", "profile", str(trace), "--output", str(tmp_path / "replay"),
                                      "--execution", "serial"])
    assert result.exit_code == 0 and calls == [(trace, "serial")]


@pytest.mark.parametrize("args", [["--db", "x.db"], ["--label", "x=y"], ["--store", "--label", "bad"]])
def test_cli_storage_options_fail_before_capture(tmp_path, monkeypatch, args):
    monkeypatch.setattr(apple_gpu, "record", lambda *a: pytest.fail("capture started"))
    result = CliRunner().invoke(app, ["apple", "gpu", "record", "--output", str(tmp_path / "unused"), *args])
    assert result.exit_code == 1 and "Error:" in result.stderr


@pytest.mark.parametrize("args", [[], ["--run-id", "missing"], ["missing.json"], ["missing.json", "--run-id", "id"],
                                 ["missing.json", "--db", "x.db"]])
def test_cli_report_errors_are_actionable(tmp_path, monkeypatch, args):
    monkeypatch.setattr("topdown.config.get_config", lambda *a: TopdownConfig(db_path=tmp_path / "gpu.db"))
    result = CliRunner().invoke(app, ["apple", "gpu", "report", *args])
    assert result.exit_code == 1 and "Error:" in result.stderr


def test_mcp_gpu_collect_query_report_and_profile_trace(tmp_path, monkeypatch):
    config = TopdownConfig(db_path=tmp_path / "gpu.db")
    monkeypatch.setattr(mcp_server, "get_config", lambda: config)
    monkeypatch.setattr(apple_gpu, "record", write_profile)
    monkeypatch.setattr(apple_gpu, "collect", lambda trace, output, execution, gpu_state: write_profile(output))
    result = mcp_server.collect_apple_gpu_profile("alu", labels={"test_name": "gpu-mcp"})
    assert "error" not in result, result
    assert "encoders" not in result["result"] and Path(result["artifacts"]).is_dir()
    run_id = result["run_id"]
    assert mcp_server.query_apple_result(run_id, "encoders", 0, 1)["total"] == 1
    assert mcp_server.get_apple_gpu_report(run_id, 1)["encoders"][0]["limiters"][0]["exceeds_100"]
    assert "get_apple_gpu_report" in mcp_server.get_funnel(run_id)
    assert "error" in mcp_server.get_apple_gpu_report("missing")
    assert "error" in mcp_server.get_apple_gpu_report(run_id, limit=0)
    again = mcp_server.import_apple_result(str(Path(result["artifacts"]) / "profile.json"))
    assert again["run_id"] != run_id and "error" not in again
    replay = mcp_server.profile_apple_gpu_trace("/tmp/existing.gputrace", execution="serial")
    assert "error" not in replay and replay["run_id"] != run_id


def test_mcp_failed_capture_keeps_artifacts_and_does_not_store(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "get_config", lambda: TopdownConfig(db_path=tmp_path / "gpu.db"))
    def fail(output, *args):
        output.mkdir(parents=True)
        (output / "capture.stderr").write_text("unsupported replay")
        raise ValueError("unsupported replay")
    monkeypatch.setattr(apple_gpu, "record", fail)
    monkeypatch.setattr(mcp_server, "_get_backend", lambda: pytest.fail("opened database after failed capture"))
    result = mcp_server.collect_apple_gpu_profile()
    assert result["error"] == "unsupported replay" and (Path(result["artifacts"]) / "capture.stderr").is_file()
    invalid = mcp_server.collect_apple_gpu_profile(labels={"invalid": 1})
    assert "Labels" in invalid["error"] and not Path(invalid["artifacts"]).exists()


@pytest.mark.asyncio
async def test_gpu_tools_registered_for_default_and_authenticated_servers():
    expected = {"collect_apple_gpu_profile", "profile_apple_gpu_trace", "get_apple_gpu_report"}
    fresh = FastMCP("authenticated-test")
    mcp_server._register_tools_and_resources(fresh)
    for server in [mcp_server.mcp, fresh]:
        assert expected <= {tool.name for tool in await server.list_tools()}


def test_gpu_tma_fields_are_null_when_present():
    result = native_profile()
    result["tma"] = dict.fromkeys(TMA_ROOTS)
    validate_result(result)


def check_measured_profiles(backend, tmp_path):
    from tests.test_apple_gpu_replay import EVIDENCE

    for case in EVIDENCE["replay_cases"]:
        result = native_profile()
        result.update(settings=case["settings"], timeline=case["timeline"],
            encoders=[{"id": row["id"], "listing": {"values": [{"value": row["label"]}]},
                       "counters": row["native_counters"]} for row in case["encoders"]])
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(result))
        run = store_apple_result(backend, path, labels={"test_name": case["case"]})
        assert backend.get_apple_result(run.run_id)["result"] == result
        page = query_apple_result(backend, run.run_id, "encoders", 1, 2)
        assert page["total"] == case["probe"]["passes"] and page["rows"] == result["encoders"][1:3]
        report = stored_apple_gpu_report(backend, run.run_id)
        assert report["timeline"]["Cores"] == case["timeline"]["Cores"]
        assert {r["id"]: r["native_counters"] for r in report["encoders"]} == {
            r["id"]: r["native_counters"] for r in case["encoders"]}
        assert not backend.get_samples(run.run_id)


def test_sqlite_measured_m6_profiles_keep_counters_and_coverage(sqlite_backend, tmp_path):
    check_measured_profiles(sqlite_backend, tmp_path)


def check_mixed_profile(backend, tmp_path):
    import hashlib

    root = Path(__file__).resolve().parents[1]
    evidence = json.loads((root / "docs/apple-gpu-m6-mixed-validation.json").read_text())
    assert hashlib.sha256((root / evidence["source"]).read_bytes()).hexdigest() == evidence["source_sha256"]
    assert evidence["probe"]["buffer_verified"] and evidence["probe"]["pixel"] == [255, 0, 0, 255]
    profile = evidence["profile"]
    path = tmp_path / "mixed-profile.json"
    path.write_text(json.dumps(profile))
    run = store_apple_result(backend, path)
    assert backend.get_apple_result(run.run_id)["result"] == profile
    assert query_apple_result(backend, run.run_id, "encoders", 1, 1)["rows"] == profile["encoders"][1:2]
    report = stored_apple_gpu_report(backend, run.run_id)
    assert report == {**evidence["report"], "run_id": run.run_id}
    assert {row["native_counters"]["Encoder type"] for row in report["encoders"]} == {"Compute", "Blit", "Render"}
    assert not backend.get_samples(run.run_id)


def test_sqlite_measured_m6_mixed_encoder_profile(sqlite_backend, tmp_path):
    check_mixed_profile(sqlite_backend, tmp_path)


def test_packaged_record_preserves_replay_host_identity(tmp_path, fake_capture, monkeypatch):
    environment = {"cpu": "Apple M6", "arch": "arm64", "kernel_uuid": "REPLAY-KERNEL"}
    def replay(trace, output, execution, gpu_state):
        profile = write_profile(output)
        profile["environment"] = environment.copy()
        return profile
    monkeypatch.setattr(apple_gpu, "collect", replay)
    result = apple_gpu.record(tmp_path / "capture")
    assert result["environment"] == environment


def test_mcp_methodology_separates_native_gpu_replay_and_sampled_counter_scopes():
    text = mcp_server.methodology()
    assert "get_apple_gpu_report" in text and "query_apple_result" in text
    assert "Limiter percentages overlap, can exceed 100%" in text
    assert "different denominators" in text and "not interchangeable with replay costs" in text
    assert "Timeline hardware counters can include other GPU activity" in text
    assert "independently floored" in text and "raw Mach counters" in text


def test_gpu_report_retains_recorded_environment_for_comparison():
    profile = native_profile()
    profile["environment"] = {"cpu": "Apple M6", "arch": "arm64", "os_build": "26A434",
                              "kernel_uuid": "RECORDED-KERNEL"}
    report = apple_gpu_report.summarize(profile)
    assert report["environment"] == profile["environment"]
    assert "environment" not in apple_gpu_report.summarize(native_profile())


@pytest.mark.parametrize("command", ["record", "profile"])
def test_gpu_cli_cancellation_retains_artifacts_and_skips_storage(tmp_path, monkeypatch, command):
    from unittest.mock import Mock

    output = tmp_path / "cancelled"
    def interrupted(path, *args):
        path.mkdir()
        (path / "native.command.json").write_text('[]')
        raise KeyboardInterrupt
    store = Mock(side_effect=AssertionError("stored a cancelled GPU capture"))
    monkeypatch.setattr(apple_gpu, "record", interrupted)
    monkeypatch.setattr(apple_gpu, "collect", lambda trace, output, execution, gpu_state: interrupted(output))
    monkeypatch.setattr("topdown.apple_cli._store_capture", store)
    args = ["apple", "gpu", command]
    if command == "profile":
        trace = tmp_path / "input.gputrace"
        trace.mkdir()
        args.append(str(trace))
    result = CliRunner().invoke(app, args + ["--output", str(output), "--store", "--db", str(tmp_path / "unused.db")])
    assert result.exit_code == 130 and "artifacts retained" in result.stderr
    assert (output / "native.command.json").exists()
    store.assert_not_called()


def test_standalone_gpu_replay_cancellation_exits_with_artifact_path(tmp_path, monkeypatch, capsys):
    trace = tmp_path / "input.gputrace"
    trace.mkdir()
    output = tmp_path / "cancelled"
    def interrupted(*args):
        output.mkdir()
        raise KeyboardInterrupt
    monkeypatch.setattr(apple_gpu, "collect", interrupted)
    monkeypatch.setattr("sys.argv", ["apple-gpu", str(trace), "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        apple_gpu.main()
    assert error.value.code == 130 and str(output) in capsys.readouterr().err
