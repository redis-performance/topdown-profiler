"""MCP cancellation notifications must reach native capture ownership cleanup."""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
import pytest

from tests.test_stax_capture import assert_reaped, events, fake_stax  # noqa: F401


@pytest.mark.skipif(os.name != "posix", reason="POSIX Stax subprocess ownership")
@pytest.mark.asyncio
async def test_stdio_cancel_active_and_queued_capture(request, monkeypatch, tmp_path):
    root = request.getfixturevalue("fake_stax")
    monkeypatch.setenv("STAX_TEST_MODE", "hang")
    database = tmp_path / "data.db"
    environment = {**os.environ, "TOPDOWN_AUTH_MODE": "none", "TOPDOWN_BACKEND": "sqlite",
                   "TOPDOWN_DB_PATH": str(database)}
    program = """
from topdown.collector import stax
stax.platform.system = lambda: 'Linux'
from topdown.mcp_server import run_server
run_server()
"""
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    tasks = []

    async def until(condition):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if condition():
                return
            await asyncio.sleep(.01)
        pytest.fail("Stax protocol operation did not complete")

    def metadata():
        paths = list((tmp_path / "stax-captures").glob("*/capture.json"))
        try:
            return json.loads(paths[0].read_text()) if paths else None
        except json.JSONDecodeError:
            return None

    try:
        parameters = StdioServerParameters(command=sys.executable, args=["-c", program], env=environment,
                                          cwd=str(Path(__file__).resolve().parents[1]))
        with (tmp_path / "server.stderr").open("w") as errors:
            async with stdio_client(parameters, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    await session.list_tools()
                    active_id = session._request_id
                    active = asyncio.create_task(session.call_tool(
                        "collect_stax_profile", {"pid": target.pid, "duration_seconds": 30}))
                    tasks.append(active)
                    await until(lambda: any(row["args"] and row["args"][0] == "record" for row in events(root)))
                    queued_id = session._request_id
                    queued = asyncio.create_task(session.call_tool(
                        "collect_stax_profile", {"pid": target.pid, "duration_seconds": 30}))
                    tasks.append(queued)
                    await until(lambda: session._request_id > queued_id)
                    response = await asyncio.wait_for(session.call_tool("query_stax_result", {"run_id": "missing"}), 3)
                    assert "not found" in response.content[0].text

                    async def cancel(request_id, task):
                        await session.send_notification(types.ClientNotification(types.CancelledNotification(
                            params=types.CancelledNotificationParams(requestId=request_id, reason="test cleanup"))))
                        with pytest.raises(McpError, match="cancel"):
                            await asyncio.wait_for(task, 8)

                    await cancel(queued_id, queued)
                    await cancel(active_id, active)
                    await until(metadata)
                    assert metadata()["status"] == "interrupted"
                    outputs = list((tmp_path / "stax-captures").iterdir())
                    assert len(outputs) == 1
                    assert (outputs[0] / "partial-archive/manifest.json").is_file()
                    assert_reaped(root)
                    assert target.poll() is None
                    with sqlite3.connect(database) as connection:
                        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
                    assert not (await asyncio.wait_for(session.call_tool("stax_doctor", {}), 3)).isError
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        target.terminate()
        target.wait(timeout=3)
