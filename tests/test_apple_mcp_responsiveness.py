"""A native profiling request must leave the MCP event loop responsive."""

import asyncio
import json
import threading

import pytest

from topdown import mcp_server


@pytest.mark.asyncio
async def test_native_doctor_does_not_block_other_event_loop_work(monkeypatch):
    started, heartbeat = threading.Event(), threading.Event()
    def doctor():
        started.set()
        assert heartbeat.wait(1), "native tool blocked the MCP event loop"
        return {"responsive": True}
    monkeypatch.setattr("topdown.collector.apple.doctor", doctor)
    async def ping():
        assert await asyncio.to_thread(started.wait, 2)
        heartbeat.set()
    reply, _ = await asyncio.wait_for(asyncio.gather(mcp_server.mcp.call_tool("apple_doctor", {}), ping()), 4)
    content = reply[0] if isinstance(reply, tuple) else reply
    assert json.loads(content[0].text) == {"responsive": True}


@pytest.mark.asyncio
async def test_capture_worker_remains_serial_while_readers_are_responsive():
    from topdown.collector.apple_runtime import worker, sleep

    started, release = threading.Event(), threading.Event()
    order = []
    def first():
        order.append('first-start')
        started.set()
        while not release.is_set():
            sleep(.01)
        order.append('first-end')
        return 'first'
    def second():
        order.append('second')
        return 'second'
    active = asyncio.create_task(worker(first, capture=True)())
    queued = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        queued = asyncio.create_task(worker(second, capture=True)())
        assert await asyncio.wait_for(worker(lambda: 'reader')(), 2) == 'reader'
        assert order == ['first-start']
        release.set()
        assert await active == 'first'
        assert await queued == 'second'
        assert order == ['first-start', 'first-end', 'second']
    finally:
        release.set()
        await asyncio.gather(active, *([queued] if queued else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_queued_capture_never_starts():
    from topdown.collector.apple_runtime import worker, sleep

    started, release, invoked = threading.Event(), threading.Event(), threading.Event()
    def first():
        started.set()
        while not release.is_set():
            sleep(.01)
    def second():
        invoked.set()
    active = asyncio.create_task(worker(first, capture=True)())
    queued = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        queued = asyncio.create_task(worker(second, capture=True)())
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(queued, 2)
        release.set()
        await asyncio.wait_for(active, 2)
        assert not invoked.is_set()
    finally:
        release.set()
        await asyncio.gather(active, *([queued] if queued else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_workers_copy_request_context_without_leaking_between_calls():
    from contextvars import ContextVar
    from topdown.collector.apple_runtime import worker

    marker = ContextVar('request-marker', default=None)
    token = marker.set('first')
    try:
        assert await worker(marker.get)() == 'first'
    finally:
        marker.reset(token)
    assert await worker(marker.get)() is None
    assert not asyncio.iscoroutinefunction(mcp_server.apple_doctor)


@pytest.mark.asyncio
async def test_cancelled_native_command_is_reaped_and_keeps_partial_output(tmp_path):
    import os
    import sys
    import time
    from topdown.collector.apple import _run
    from topdown.collector.apple_runtime import worker

    pidfile = tmp_path / 'pid'
    program = ('import os,sys,time\nfrom pathlib import Path\n'
               'Path(sys.argv[1]).write_text(str(os.getpid()))\n'
               'print("partial-native-marker",flush=True)\ntime.sleep(30)')
    task = asyncio.create_task(worker(lambda: _run([sys.executable, '-u', '-c', program, str(pidfile)],
                                                   tmp_path, 'native', 20), capture=True)())
    try:
        deadline = time.monotonic() + 4
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 6)
        assert 'partial-native-marker' in (tmp_path / 'native.stdout').read_text()
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_command_timeout_is_bounded_and_keeps_partial_output(tmp_path):
    import subprocess
    import sys
    from topdown.collector.apple_runtime import worker, run

    command = [sys.executable, '-u', '-c', 'import time;print("before-timeout",flush=True);time.sleep(20)']
    with pytest.raises(subprocess.TimeoutExpired) as error:
        await worker(lambda: run(command, timeout=.2), capture=True)()
    assert b'before-timeout' in error.value.stdout


@pytest.mark.asyncio
async def test_native_command_exit_failures_preserve_standard_subprocess_errors():
    import subprocess
    import sys
    from topdown.collector.apple_runtime import worker, run

    command = [sys.executable, '-c', 'import sys;print("failed-native");sys.exit(7)']
    with pytest.raises(subprocess.CalledProcessError) as error:
        await worker(lambda: run(command, timeout=3, check=True), capture=True)()
    assert error.value.returncode == 7 and 'failed-native' in error.value.stdout


@pytest.mark.asyncio
async def test_cancellation_after_capture_prevents_automatic_storage(tmp_path, monkeypatch):
    from topdown.config import TopdownConfig

    started, release = threading.Event(), threading.Event()
    def capture(output, *args):
        output.mkdir(parents=True)
        path = output / 'stat.json'
        path.write_text('{}')
        started.set()
        assert release.wait(3)
        return path
    monkeypatch.setattr(mcp_server, 'get_config', lambda: TopdownConfig(db_path=tmp_path / 'native.db'))
    monkeypatch.setattr('topdown.apple_storage.collect_apple_result', capture)
    monkeypatch.setattr(mcp_server, '_get_backend', lambda: pytest.fail('stored after request cancellation'))
    task = asyncio.create_task(mcp_server.mcp.call_tool('collect_apple_profile', {'command':['/bin/true']}))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 4)
        assert list((tmp_path / 'apple-captures').glob('*/stat.json'))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_authenticated_registration_uses_the_same_nonblocking_native_handlers(monkeypatch):
    from mcp.server.fastmcp import FastMCP

    started, heartbeat = threading.Event(), threading.Event()
    def doctor():
        started.set()
        assert heartbeat.wait(1), 'authenticated MCP handler blocked the event loop'
        return {'authenticated_worker':True}
    monkeypatch.setattr('topdown.collector.apple.doctor', doctor)
    server = FastMCP('native-worker-registration')
    mcp_server._register_tools_and_resources(server)
    async def ping():
        assert await asyncio.to_thread(started.wait, 2)
        heartbeat.set()
    reply, _ = await asyncio.wait_for(asyncio.gather(server.call_tool('apple_doctor', {}), ping()), 4)
    content = reply[0] if isinstance(reply, tuple) else reply
    assert json.loads(content[0].text) == {'authenticated_worker':True}
    tools = {tool.name:tool for tool in await server.list_tools()}
    assert set(tools['collect_apple_profile'].inputSchema['properties']) == {
        'mode','command','pid','duration_seconds','process_name','labels'}


@pytest.mark.asyncio
async def test_cancelled_native_command_escalates_when_its_owned_target_ignores_term(tmp_path):
    import sys
    import time
    from topdown.collector.apple import _run
    from topdown.collector.apple_runtime import worker

    ready = tmp_path / 'ready'
    command = [sys.executable,'-u','-c',
        'import signal,sys,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        'Path(sys.argv[1]).write_text("ready");print("ignoring-term",flush=True);time.sleep(30)', str(ready)]
    task = asyncio.create_task(worker(lambda:_run(command,tmp_path,'ignore-term',20),capture=True)())
    try:
        deadline = time.monotonic() + 4
        while not ready.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        assert ready.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 6)
        assert 'ignoring-term' in (tmp_path / 'ignore-term.stdout').read_text()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_successful_native_command_retains_completed_process_output():
    import sys
    from topdown.collector.apple_runtime import worker, run

    result = await worker(lambda:run([sys.executable,'-c','print("native-success")'],timeout=3),capture=True)()
    assert result.returncode == 0 and result.stdout == 'native-success\n' and result.stderr == ''


@pytest.mark.asyncio
async def test_cancellable_sleep_releases_a_running_worker():
    from topdown.collector.apple_runtime import worker, sleep

    started = threading.Event()
    def pause():
        started.set()
        sleep(30)
    task = asyncio.create_task(worker(pause,capture=True)())
    assert await asyncio.to_thread(started.wait,2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,2)


def test_owned_process_cleanup_never_signals_a_reaped_or_unowned_pid(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from topdown.collector.apple import AppleProfileError
    from topdown.collector import apple_runtime

    kill = Mock()
    monkeypatch.setattr(apple_runtime.os,'killpg',kill)
    monkeypatch.setattr('topdown.collector.apple_stat._peek_exit',Mock(side_effect=ChildProcessError('lost child')))
    with pytest.raises(AppleProfileError,match='ownership was lost'):
        apple_runtime._signal_owned(SimpleNamespace(returncode=None,pid=42),15)
    apple_runtime._signal_owned(SimpleNamespace(returncode=0,pid=42),15)
    kill.assert_not_called()


def test_exited_native_process_group_during_cleanup_is_harmless(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from topdown.collector import apple_runtime

    monkeypatch.setattr('topdown.collector.apple_stat._peek_exit',lambda pid:object())
    monkeypatch.setattr(apple_runtime.os,'killpg',Mock(side_effect=ProcessLookupError))
    apple_runtime._signal_owned(SimpleNamespace(returncode=None,pid=42),15)


@pytest.mark.parametrize('sig,expected',[(15,'terminate'),(9,'kill')])
def test_non_posix_process_cleanup_uses_the_owned_process_handle(monkeypatch,sig,expected):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from topdown.collector import apple_runtime

    process=Mock(returncode=None)
    monkeypatch.setattr(apple_runtime,'os',SimpleNamespace(name='nt'))
    apple_runtime._signal_owned(process,sig)
    getattr(process,expected).assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_hide_cancellation_and_is_retained_in_logs(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from topdown.collector.apple import AppleProfileError, _run
    from topdown.collector import apple_runtime

    process = Mock(returncode=None, pid=42)
    process.communicate.side_effect = AppleProfileError('cancelled', code='interrupted')
    monkeypatch.setattr(apple_runtime.subprocess, 'Popen', Mock(return_value=process))
    monkeypatch.setattr(apple_runtime, '_signal_owned', Mock(side_effect=AppleProfileError('ownership lost; no signal')))
    with pytest.raises(AppleProfileError, match='cancelled') as error:
        await apple_runtime.worker(lambda:_run(['/native/tool'],tmp_path,'native',3),capture=True)()
    assert error.value.code == 'interrupted'
    assert 'ownership lost' in (tmp_path / 'native.cleanup.stderr').read_text()


@pytest.mark.asyncio
async def test_mcp_cancel_scope_waits_for_owned_worker_cleanup():
    import anyio
    from topdown.collector.apple_runtime import worker, sleep

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    returned = asyncio.Event()
    scope = anyio.CancelScope()
    def capture():
        started.set()
        try:
            while True:
                sleep(.01)
        finally:
            assert release.wait(2), 'cleanup was not allowed to finish'
            finished.set()
    async def request():
        with scope:
            await worker(capture, capture=True)()
        returned.set()
    async with anyio.create_task_group() as group:
        group.start_soon(request)
        try:
            assert await asyncio.to_thread(started.wait, 2)
            scope.cancel()
            await asyncio.sleep(.02)
            assert not returned.is_set(), 'request returned before its owned worker finished cleanup'
        finally:
            release.set()
        await asyncio.wait_for(returned.wait(), 2)
        assert finished.is_set()


@pytest.mark.asyncio
async def test_repeated_task_cancellation_waits_for_owned_worker_cleanup():
    from topdown.collector.apple_runtime import worker, sleep

    started, cleaning, release = threading.Event(), threading.Event(), threading.Event()
    def capture():
        started.set()
        try:
            while True:
                sleep(.01)
        finally:
            cleaning.set()
            assert release.wait(2)
    task = asyncio.create_task(worker(capture, capture=True)())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(cleaning.wait, 2)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['import_apple_result','collect_apple_profile'])
async def test_cancellation_during_artifact_read_prevents_database_insert(tmp_path,monkeypatch,tool):
    import sqlite3
    from topdown import apple_storage
    from topdown.collector.apple import TMA_ROOTS
    from topdown.config import TopdownConfig

    path=tmp_path/'stat.json'
    path.write_text(json.dumps({'measurement_kind':'process-accounted-fixed-counters',
        'scope':{'pid':42},'counters':{'instructions':100,'cycles':200},
        'tma_available':False,'tma':dict.fromkeys(TMA_ROOTS)}))
    started,release=threading.Event(),threading.Event()
    original=apple_storage._read_json
    def read(artifact):
        value=original(artifact)
        started.set()
        assert release.wait(3)
        return value
    monkeypatch.setattr(apple_storage,'_read_json',read)
    monkeypatch.setattr(mcp_server,'get_config',lambda:TopdownConfig(db_path=tmp_path/'native.db'))
    monkeypatch.setattr(apple_storage,'collect_apple_result',lambda *args:path)
    arguments={'path':str(path)} if tool=='import_apple_result' else {'command':['/usr/bin/true']}
    task=asyncio.create_task(mcp_server.mcp.call_tool(tool,arguments))
    try:
        assert await asyncio.to_thread(started.wait,2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,3)
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)
    db=sqlite3.connect(tmp_path/'native.db')
    try:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM apple_results').fetchone()[0]==0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_methodology_resource_exposes_native_apple_scope_on_both_servers():
    from mcp.server.fastmcp import FastMCP

    authenticated = FastMCP("methodology-test")
    mcp_server._register_tools_and_resources(authenticated)
    for server in [mcp_server.mcp, authenticated]:
        resources = await server.list_resources()
        resource = next(item for item in resources if str(item.uri) == "topdown://methodology")
        assert "Apple" in resource.description
    text = mcp_server.methodology()
    assert "Apple Silicon and macOS" in text and "query_apple_result" in text
    assert "sample shares are not CPU-time percentages" in text
    assert "keep Intel TMA roots null" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("tool,arguments", [
    ("collect_apple_profile", {"pid": True}),
    ("collect_apple_profile", {"pid": 42.0}),
    ("collect_apple_profile", {"pid": "42"}),
    ("collect_apple_profile", {"command": ["/usr/bin/true"], "duration_seconds": True}),
    ("collect_apple_profile", {"command": ["/usr/bin/true"], "duration_seconds": "0.5"}),
    ("query_apple_result", {"run_id": "unused", "offset": True}),
    ("query_apple_result", {"run_id": "unused", "limit": "1"}),
    ("get_apple_hotspots", {"run_id": "unused", "limit": True}),
])
async def test_mcp_rejects_coerced_native_arguments_before_any_io(monkeypatch, authenticated, tool, arguments):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from unittest.mock import Mock

    backend = Mock(side_effect=AssertionError("database opened for invalid MCP input"))
    capture = Mock(side_effect=AssertionError("native collection received coerced MCP input"))
    config = Mock(side_effect=AssertionError("configuration read for invalid MCP input"))
    monkeypatch.setattr(mcp_server, "_get_backend", backend)
    monkeypatch.setattr(mcp_server, "get_config", config)
    monkeypatch.setattr("topdown.apple_storage.collect_apple_result", capture)
    server = FastMCP("strict-arguments") if authenticated else mcp_server.mcp
    if authenticated:
        mcp_server._register_tools_and_resources(server)
    with pytest.raises(ToolError, match="validation"):
        await server.call_tool(tool, arguments)
    backend.assert_not_called()
    capture.assert_not_called()
    config.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [1, 0.5])
async def test_strict_mcp_duration_still_accepts_json_integer_and_fractional_numbers(tmp_path, monkeypatch, seconds):
    from topdown.config import TopdownConfig
    from topdown.collector.apple import AppleProfileError
    from unittest.mock import Mock

    capture = Mock(side_effect=AppleProfileError("stopped before native access"))
    monkeypatch.setattr(mcp_server, "get_config", lambda: TopdownConfig(db_path=tmp_path / "native.db"))
    monkeypatch.setattr("topdown.apple_storage.collect_apple_result", capture)
    await mcp_server.mcp.call_tool("collect_apple_profile", {"pid": 42, "duration_seconds": seconds})
    assert capture.call_args.args[3] == 42
    assert capture.call_args.args[4] == seconds and type(capture.call_args.args[4]) is float


@pytest.mark.skipif(__import__("os").name != "posix", reason="requires POSIX process groups")
def test_native_command_can_request_process_isolation_outside_mcp():
    import os
    import sys
    from topdown.collector.apple_runtime import run

    result = run([sys.executable, "-c", "import os;print(os.getpid(),os.getpgrp())"],
                 timeout=3, start_new_session=True)
    pid, group = map(int, result.stdout.split())
    assert result.returncode == 0 and pid == group and group != os.getpgrp()


def test_noninteractive_native_command_preserves_requested_stdin(monkeypatch):
    import subprocess
    from unittest.mock import Mock
    from topdown.collector.apple_runtime import run

    command = ["native", "metadata"]
    result = subprocess.CompletedProcess(command, 0, "", "")
    native = Mock(return_value=result)
    monkeypatch.setattr(subprocess, "run", native)
    assert run(command, timeout=3, stdin=subprocess.DEVNULL, start_new_session=True) is result
    assert native.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert native.call_args.kwargs["start_new_session"] is True
