"""Replay-state selection reaches native collection, SQL storage and both MCP registrations."""

import json
import hashlib
from pathlib import Path

from mcp.server.fastmcp import FastMCP
import pytest
from typer.testing import CliRunner

from tests.test_apple_gpu_replay import native_profile
from topdown import mcp_server
from topdown.cli import app
from topdown.collector import apple_gpu
from topdown.config import TopdownConfig


def write_state(output, state):
    result = native_profile()
    result["settings"]["gpu_state"] = state
    result["timeline"]["Profiled state"] = apple_gpu.GPU_STATE_LABELS[state][-1]
    output.mkdir(parents=True)
    (output / "profile.json").write_text(json.dumps(result))
    return result


@pytest.mark.parametrize("command", ["record", "profile"])
@pytest.mark.parametrize("state", ["default", "low", "medium", "high"])
def test_gpu_cli_state_survives_capture_and_storage(tmp_path, sqlite_backend, monkeypatch, command, state):
    calls = []

    def record(output, mode, passes, items, execution, gpu_state):
        calls.append((execution, gpu_state))
        return write_state(output, gpu_state)

    def collect(trace, output, execution, gpu_state):
        return record(output, None, None, None, execution, gpu_state)

    monkeypatch.setattr(apple_gpu, "record", record)
    monkeypatch.setattr(apple_gpu, "collect", collect)
    trace = tmp_path / "existing.gputrace"
    trace.mkdir()
    args = ["apple", "gpu", command, *([str(trace)] if command == "profile" else []),
            "--output", str(tmp_path / "result"), "--execution", "serial", "--gpu-state", state,
            "--store", "--db", str(sqlite_backend.db_path)]
    reply = CliRunner().invoke(app, args)
    assert reply.exit_code == 0, reply.output
    assert calls == [("serial", state)]
    run_id = json.loads(reply.stdout)["run_id"]
    stored = sqlite_backend.get_apple_result(run_id)["result"]
    assert stored["settings"]["gpu_state"] == state
    assert stored["timeline"]["Profiled state"] == apple_gpu.GPU_STATE_LABELS[state][-1]


@pytest.mark.parametrize("tool", ["collect_apple_gpu_profile", "profile_apple_gpu_trace"])
@pytest.mark.parametrize("state", ["default", "low", "medium", "high"])
def test_gpu_mcp_state_survives_capture_storage_and_report(tmp_path, monkeypatch, tool, state):
    monkeypatch.setattr(mcp_server, "get_config", lambda: TopdownConfig(db_path=tmp_path / "native.db"))

    def record(output, mode, passes, items, execution, gpu_state):
        assert execution == "serial" and gpu_state == state
        return write_state(output, gpu_state)

    monkeypatch.setattr(apple_gpu, "record", record)
    monkeypatch.setattr(apple_gpu, "collect", lambda trace, output, execution, gpu_state:
                        record(output, None, None, None, execution, gpu_state))
    options = {"path": "/server/existing.gputrace"} if tool == "profile_apple_gpu_trace" else {}
    result = getattr(mcp_server, tool)(execution="serial", gpu_state=state, **options)
    assert "error" not in result, result
    assert result["result"]["settings"]["gpu_state"] == state
    report = mcp_server.get_apple_gpu_report(result["run_id"])
    assert report["settings"]["gpu_state"] == state
    assert report["timeline"]["Profiled state"] == apple_gpu.GPU_STATE_LABELS[state][-1]


@pytest.mark.asyncio
async def test_gpu_state_schema_matches_stdio_and_http_registrations():
    authenticated = FastMCP("state-schema")
    mcp_server._register_tools_and_resources(authenticated)
    for server in (mcp_server.mcp, authenticated):
        tools = {tool.name: tool for tool in await server.list_tools()}
        for name in ("collect_apple_gpu_profile", "profile_apple_gpu_trace"):
            field = tools[name].inputSchema["properties"]["gpu_state"]
            assert field["enum"] == list(apple_gpu.GPU_STATE_LABELS)
            assert field["default"] == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["collect_apple_gpu_profile", "profile_apple_gpu_trace"])
@pytest.mark.parametrize("state", [None, True, "max", "min", "turbo", "high --exec serial"])
async def test_invalid_mcp_states_fail_before_configuration_or_capture(monkeypatch, tool, state):
    def config():
        raise AssertionError("invalid state reached configuration")

    monkeypatch.setattr(mcp_server, "get_config", config)
    arguments = {"gpu_state": state, **({"path": "/ignored.gputrace"} if tool == "profile_apple_gpu_trace" else {})}
    with pytest.raises(Exception, match="gpu_state"):
        await mcp_server.mcp.call_tool(tool, arguments)


def test_packaged_record_forwards_state_to_replay(tmp_path, monkeypatch):
    from tests.test_apple_gpu_integration import write_profile

    monkeypatch.setattr(apple_gpu, "_require_host", lambda: None)

    def run(command, **kwargs):
        import subprocess

        if command[0] != "xcrun":
            probe = {"mode": "compute", "passes": 4, "items": 16384, "checksum_verified": True}
            Path(command[command.index("--result") + 1]).write_text(json.dumps(probe))
        return subprocess.CompletedProcess(command, 0, "", "")

    def replay(trace, output, execution, state):
        assert state == "high"
        result = write_profile(output)
        result["settings"]["gpu_state"] = state
        return result

    monkeypatch.setattr(apple_gpu.apple_runtime, "run", run)
    monkeypatch.setattr(apple_gpu, "collect", replay)
    result = apple_gpu.record(tmp_path / "record", gpu_state="high")
    assert result["settings"]["gpu_state"] == "high"


EVIDENCE = json.loads((Path(__file__).resolve().parents[1] / "docs/apple-gpu-m6-replay-states.json").read_text())


def test_measured_replay_states_use_one_verified_control_and_balanced_order():
    source = Path(__file__).resolve().parents[1] / EVIDENCE["source"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == EVIDENCE["source_sha256"]
    assert EVIDENCE["original_probe"]["checksum_verified"] is True
    assert EVIDENCE["order"][:4] == list(reversed(EVIDENCE["order"][4:]))


@pytest.mark.parametrize("case", EVIDENCE["cases"], ids=lambda case: str(case["trial"]))
def test_measured_replay_state_requests_and_labels_are_exact(case):
    from topdown.apple_gpu_report import summarize

    state, result = case["requested_state"], case["profile"]
    assert result["settings"]["gpu_state"] == state
    assert result["timeline"]["Profiled state"] in apple_gpu.GPU_STATE_LABELS[state]
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == case["profile_canonical_sha256"]
    report = summarize(result)
    assert report["total_encoders"] == 4
    assert report["settings"] == result["settings"] and report["timeline"] == result["timeline"]
    assert report["timeline_counter_scope"]["target_exclusive"] is False


def check_measured_replay_state_storage(backend, tmp_path):
    from topdown.apple_storage import query_apple_result, store_apple_result, stored_apple_gpu_report

    for case in EVIDENCE["cases"]:
        profile = case["profile"]
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        run = store_apple_result(backend, path)
        assert backend.get_apple_result(run.run_id)["result"] == profile
        assert query_apple_result(backend, run.run_id, "encoders", 0, 4)["rows"] == profile["encoders"]
        report = stored_apple_gpu_report(backend, run.run_id)
        assert report["settings"] == profile["settings"]
        assert report["timeline"] == profile["timeline"]


def test_sqlite_measured_replay_state_storage(sqlite_backend, tmp_path):
    check_measured_replay_state_storage(sqlite_backend, tmp_path)
