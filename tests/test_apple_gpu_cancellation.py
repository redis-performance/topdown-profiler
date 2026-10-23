"""GPU cancellation retains evidence and cleans up only its own replay session."""

import asyncio
import json
import os
import subprocess
import sys
import threading

from mcp.server.fastmcp import FastMCP
import pytest
from typer.testing import CliRunner

from topdown import mcp_server
from topdown.cli import app
from topdown.collector import apple_gpu, apple_runtime
from topdown.collector.apple import AppleProfileError
from topdown.config import TopdownConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["startup", "replay", "unknown-session"])
async def test_cancelled_gpu_replay_retains_partial_logs_and_cleans_owned_session(tmp_path, monkeypatch, phase):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    output = tmp_path / "replay"
    started, release = threading.Event(), threading.Event()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "--terminate" in command:
            apple_runtime.check_cancelled()
            return subprocess.CompletedProcess(command, 0, "terminated", "")
        if "-t" in command and phase == "replay":
            return subprocess.CompletedProcess(command, 0, "Session 42 created.\n", "")
        started.set()
        assert release.wait(3), "GPU cancellation blocked the MCP event loop"
        apple_runtime.check_cancelled()
        stdout = "partial replay" if phase == "replay" else (
            "starting" if phase == "unknown-session" else "Session 42 created.\n")
        return subprocess.CompletedProcess(command, 0, stdout, "partial stderr")

    monkeypatch.setattr(apple_runtime, "run", run)
    task = asyncio.create_task(apple_runtime.worker(lambda: apple_gpu.collect(trace, output), capture=True)())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancelled before native command could finish safely"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not (output / "profile.json").exists()
    assert b"partial stderr" in b"".join(p.read_bytes() for p in output.glob("*.stderr"))
    terminations = [command for command in commands if "--terminate" in command]
    assert terminations == ([] if phase == "unknown-session" else [["xcrun", "gpudebug", "--terminate", "42"]])


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("tool,arguments", [
    ("collect_apple_gpu_profile", {}), ("profile_apple_gpu_trace", {"path": "test.gputrace"}),
])
async def test_gpu_mcp_cancellation_keeps_readers_responsive_and_prevents_storage(
        tmp_path, monkeypatch, authenticated, tool, arguments):
    started, release = threading.Event(), threading.Event()
    def capture(*args):
        started.set()
        assert release.wait(3), "GPU capture blocked the MCP event loop"
    monkeypatch.setattr(apple_gpu, "record", capture)
    monkeypatch.setattr(apple_gpu, "collect", capture)
    monkeypatch.setattr(mcp_server, "get_config", lambda: TopdownConfig(db_path=tmp_path / "gpu.db"))
    monkeypatch.setattr(mcp_server, "_get_backend", lambda: pytest.fail("stored a cancelled GPU result"))
    monkeypatch.setattr("topdown.collector.apple.doctor", lambda: {"responsive": True})
    server = FastMCP("gpu-test") if authenticated else mcp_server.mcp
    if authenticated:
        mcp_server._register_tools_and_resources(server)
    task = asyncio.create_task(server.call_tool(tool, arguments))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        reply = await asyncio.wait_for(server.call_tool("apple_doctor", {}), 2)
        content = reply[0] if isinstance(reply, tuple) else reply
        assert json.loads(content[0].text) == {"responsive": True}
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_gpu_compile_preserves_partial_logs(tmp_path, monkeypatch):
    started = threading.Event()
    def run(*args, **kwargs):
        started.set()
        try:
            apple_runtime.sleep(5)
        except AppleProfileError as exc:
            exc.stdout, exc.stderr = b"compiler output", b"compiler diagnostic"
            raise
    monkeypatch.setattr(apple_gpu, "_require_host", lambda: None)
    monkeypatch.setattr(apple_runtime, "run", run)
    output = tmp_path / "capture"
    task = asyncio.create_task(apple_runtime.worker(lambda: apple_gpu.record(output), capture=True)())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        await asyncio.gather(task, return_exceptions=True)
    assert (output / "compile.stdout").read_bytes() == b"compiler output"
    assert (output / "compile.stderr").read_bytes() == b"compiler diagnostic"
    assert not (output / "profile.json").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_gpu_local_reports_reject_fifo_without_reading(tmp_path):
    fifo = tmp_path / "profile.json"
    os.mkfifo(fifo)
    code = '''import signal,sys
from topdown.cli import app
from typer.testing import CliRunner
def timeout(*args): raise AssertionError("GPU report blocked on a FIFO")
signal.signal(signal.SIGALRM,timeout)
signal.alarm(2)
result=CliRunner().invoke(app,["apple","gpu","report",sys.argv[1]])
assert result.exit_code==1 and "regular Apple JSON artifact" in result.stderr,result.output
'''
    result = subprocess.run([sys.executable, "-c", code, str(fifo)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    standalone = subprocess.run([sys.executable, "-m", "topdown.apple_gpu_report", str(fifo)],
                                capture_output=True, text=True, timeout=5)
    assert standalone.returncode == 2 and "regular Apple JSON artifact" in standalone.stderr


def test_gpu_local_report_supports_regular_symlink(tmp_path):
    from tests.test_apple_gpu_replay import native_profile

    path = tmp_path / "profile.json"
    path.write_text(json.dumps(native_profile()))
    link = tmp_path / "latest.json"
    link.symlink_to(path)
    result = CliRunner().invoke(app, ["apple", "gpu", "report", str(link), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["device"] == "Apple M6"


@pytest.mark.parametrize("field,reason", [
    (b'"device":"Apple M1"', "Duplicate"),
    (b'"metadata":1e999', "Non-finite"),
    (b'"metadata":NaN', "Non-finite"),
])
def test_local_gpu_reports_reject_ambiguous_json_consistently(tmp_path, field, reason):
    from tests.test_apple_gpu_replay import native_profile

    path = tmp_path / "profile.json"
    original = json.dumps(native_profile()).encode()
    path.write_bytes(original[:-1] + b"," + field + b"}")
    result = CliRunner().invoke(app, ["apple", "gpu", "report", str(path)])
    assert result.exit_code == 1 and reason in result.stderr
    standalone = subprocess.run([sys.executable, "-m", "topdown.apple_gpu_report", str(path)],
                                text=True, capture_output=True, timeout=5)
    assert standalone.returncode == 2 and reason in standalone.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("tool,arguments", [
    ("collect_apple_gpu_profile", {"passes": True}),
    ("collect_apple_gpu_profile", {"passes": 4.0}),
    ("collect_apple_gpu_profile", {"items": "128"}),
    ("collect_apple_gpu_profile", {"items": True}),
    ("get_apple_gpu_report", {"run_id": "unused", "limit": True}),
    ("get_apple_gpu_report", {"run_id": "unused", "offset": "1"}),
])
async def test_gpu_mcp_rejects_coerced_parameters_before_host_or_database_access(
        monkeypatch, authenticated, tool, arguments):
    from mcp.server.fastmcp.exceptions import ToolError
    from unittest.mock import Mock

    config = Mock(side_effect=AssertionError("configuration read for invalid GPU input"))
    backend = Mock(side_effect=AssertionError("database opened for invalid GPU input"))
    capture = Mock(side_effect=AssertionError("GPU capture started with coerced input"))
    monkeypatch.setattr(mcp_server, "get_config", config)
    monkeypatch.setattr(mcp_server, "_get_backend", backend)
    monkeypatch.setattr(apple_gpu, "record", capture)
    server = FastMCP("strict-gpu-arguments") if authenticated else mcp_server.mcp
    if authenticated:
        mcp_server._register_tools_and_resources(server)
    with pytest.raises(ToolError, match="validation"):
        await server.call_tool(tool, arguments)
    config.assert_not_called()
    backend.assert_not_called()
    capture.assert_not_called()
