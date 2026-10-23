"""Registered MCP tools, strict inputs and responsive sampling workers."""

import asyncio
import json
import threading

import pytest

from topdown import mcp_server as server
from topdown.collector.apple_runtime import sleep
from tests.test_stax_capture import fake_stax  # noqa: F401


@pytest.mark.asyncio
async def test_fresh_server_registers_all_stax_tools(monkeypatch):
    monkeypatch.setenv("TOPDOWN_AUTH_MODE", "none")
    target = server._create_mcp()
    server._register_tools_and_resources(target)
    tools = {tool.name: tool for tool in await target.list_tools()}
    assert {
        "stax_doctor",
        "collect_stax_profile",
        "import_stax_result",
        "query_stax_result",
        "get_stax_hotspots",
        "get_stax_thread_waits",
        "get_stax_target_spans",
    } <= set(tools)
    assert tools["collect_stax_profile"].inputSchema["properties"]["duration_seconds"]["type"] == "integer"


@pytest.mark.asyncio
async def test_stax_doctor_runs_off_event_loop(monkeypatch):
    started, heartbeat = threading.Event(), threading.Event()

    def inspect():
        started.set()
        assert heartbeat.wait(1)
        return {"responsive": True}

    monkeypatch.setattr("topdown.collector.stax.doctor", inspect)

    async def ping():
        assert await asyncio.to_thread(started.wait, 2)
        heartbeat.set()

    response, _ = await asyncio.wait_for(asyncio.gather(server.mcp.call_tool("stax_doctor", {}), ping()), 4)
    content = response[0] if isinstance(response, tuple) else response
    assert json.loads(content[0].text) == {"responsive": True}


@pytest.mark.asyncio
async def test_stax_and_apple_share_capture_serialization(monkeypatch, tmp_path):
    from tests.test_stax_archive import archive_document
    from topdown.collector.apple import AppleProfileError

    started, release, second = threading.Event(), threading.Event(), threading.Event()
    source = tmp_path / "profile.stax"
    source.write_text(json.dumps(archive_document()))
    monkeypatch.setenv("TOPDOWN_DB_PATH", str(tmp_path / "profiles.db"))

    def capture(**kwargs):
        started.set()
        while not release.is_set():
            sleep(0.01)
        return source, {}

    def apple(*args):
        second.set()
        raise AppleProfileError("test capture stopped")

    monkeypatch.setattr("topdown.collector.stax.record_profile", capture)
    monkeypatch.setattr("topdown.apple_storage.collect_apple_result", apple)
    active = asyncio.create_task(server.mcp.call_tool("collect_stax_profile", {"pid": 42}))
    queued = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        queued = asyncio.create_task(server.mcp.call_tool("collect_apple_profile", {"pid": 42}))
        await asyncio.wait_for(server.mcp.call_tool("query_stax_result", {"run_id": "missing"}), 2)
        assert not second.is_set()
        release.set()
        response = await asyncio.wait_for(active, 2)
        content = response[0] if isinstance(response, tuple) else response
        assert "run_id" in json.loads(content[0].text)
        await asyncio.wait_for(queued, 2)
        assert second.is_set()
    finally:
        release.set()
        await asyncio.gather(active, *([queued] if queued else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments", [{"pid": True}, {"pid": 42, "duration_seconds": 1.5}, {"pid": 42, "frequency_hz": True}]
)
async def test_capture_rejects_coerced_numbers_before_recording(monkeypatch, arguments):
    def fail(**kwargs):
        pytest.fail("started capture with invalid numeric input")

    monkeypatch.setattr("topdown.collector.stax.record_profile", fail)
    with pytest.raises(Exception, match="validation|integer"):
        await server.mcp.call_tool("collect_stax_profile", arguments)


def test_explicit_stax_collector_guides_tma_callers(monkeypatch):
    from topdown.collector import make_runner
    from topdown.config import TopdownConfig

    monkeypatch.setenv("TOPDOWN_COLLECTOR", "stax")
    assert "collect_stax_profile" in server.collect_topdown("redis-server")
    with pytest.raises(RuntimeError, match="sampling collector"):
        make_runner(TopdownConfig(collector="stax"), [42], False, 1)


@pytest.mark.asyncio
async def test_cancelled_registered_capture_reaps_children_and_never_stores(request, monkeypatch, tmp_path):
    root = request.getfixturevalue("fake_stax")
    monkeypatch.setenv("STAX_TEST_MODE", "hang")
    monkeypatch.setenv("TOPDOWN_DB_PATH", str(tmp_path / "data.db"))
    monkeypatch.setattr(server, "_get_backend", lambda: pytest.fail("stored a cancelled capture"))
    task = asyncio.create_task(server.mcp.call_tool("collect_stax_profile", {"pid": 42}))
    try:
        for _ in range(200):
            rows = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            if any(row["args"] and row["args"][0] == "record" for row in rows):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("recorder did not start")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        from tests.test_stax_capture import assert_reaped

        assert_reaped(root)
        outputs = list((tmp_path / "stax-captures").iterdir())
        assert len(outputs) == 1
        assert (outputs[0] / "partial-archive/manifest.json").is_file()
        assert json.loads((outputs[0] / "capture.json").read_text())["status"] == "interrupted"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
