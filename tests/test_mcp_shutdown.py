"""Stdio shutdown must wait for owned native children to be reaped."""

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import anyio
import pytest

from topdown.mcp_runtime import run_stdio


@pytest.mark.parametrize("fail", [False, True])
def test_stdio_restores_existing_sigterm_handler(fail):
    previous = signal.getsignal(signal.SIGTERM)

    async def serve():
        assert signal.getsignal(signal.SIGTERM) != previous
        if fail:
            raise ValueError("server failed")

    try:
        if fail:
            with pytest.raises(ValueError, match="server failed"):
                run_stdio(serve)
        else:
            run_stdio(serve)
    finally:
        assert signal.getsignal(signal.SIGTERM) == previous


def test_stdio_in_worker_thread_preserves_process_signal_handlers():
    previous = signal.getsignal(signal.SIGTERM)
    completed = []

    async def serve():
        completed.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=run_stdio, args=(serve,))
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    assert completed == [previous]


def test_sigterm_cancels_and_waits_for_shielded_cleanup():
    cleaned = []
    previous = signal.getsignal(signal.SIGTERM)

    async def serve():
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        try:
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(.01)
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                await anyio.sleep(.01)
                cleaned.append(True)

    with pytest.raises(SystemExit) as exc:
        run_stdio(serve)
    assert exc.value.code == 143
    assert cleaned == [True]
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups and pipe readiness")
@pytest.mark.parametrize("ignore_term", [False, True])
@pytest.mark.parametrize("shutdown", ["sigterm", "eof"])
def test_real_stdio_shutdown_reaps_only_owned_capture(tmp_path, ignore_term, shutdown):
    pidfile = tmp_path / "target.pid"
    marker = tmp_path / "cleanup-finished"
    child = ("import os,signal,sys,time;from pathlib import Path;"
             + ("signal.signal(signal.SIGTERM,signal.SIG_IGN);" if ignore_term else "")
             + "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)")
    program = '''
from pathlib import Path
from topdown import mcp_server, apple_storage
from topdown.collector.apple import _run
import sys
def capture(output, mode, command, pid, duration):
    output.mkdir(parents=True)
    try:
        _run(command, output, 'native', duration)
    finally:
        Path(sys.argv[1]).write_text('cleaned')
    raise AssertionError('cancelled capture returned successfully')
apple_storage.collect_apple_result = capture
mcp_server.run_server()
'''
    env = {**os.environ, "TOPDOWN_DB_PATH": str(tmp_path / "test.db"), "TOPDOWN_AUTH_MODE": "none"}
    with (tmp_path / "server.stderr").open("w") as errors:
        server = subprocess.Popen([sys.executable, "-c", program, str(marker)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=errors, text=True, env=env, start_new_session=True)
        target = None
        try:
            def send(message):
                server.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                server.stdin.flush()

            send({"id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                  "capabilities": {}, "clientInfo": {"name": "owned-signal-test", "version": "1"}}})
            assert select.select([server.stdout], [], [], 6)[0], "server did not initialize"
            assert json.loads(server.stdout.readline())["id"] == 0
            send({"method": "notifications/initialized"})
            send({"id": 1, "method": "tools/call", "params": {"name": "collect_apple_profile", "arguments": {
                "command": [sys.executable, "-c", child, str(pidfile)], "duration_seconds": 30}}})
            deadline = time.monotonic() + 6
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert pidfile.exists(), "owned target did not start"
            target = int(pidfile.read_text())
            if shutdown == "eof":
                server.stdin.close()
            else:
                server.send_signal(signal.SIGTERM)
                if ignore_term:
                    time.sleep(.1)
                    server.send_signal(signal.SIGTERM)
            assert server.wait(timeout=8) == (0 if shutdown == "eof" else 143), (tmp_path / "server.stderr").read_text()
            assert marker.read_text() == "cleaned"
            with pytest.raises(ProcessLookupError):
                os.kill(target, 0)
            assert not (tmp_path / "test.db").exists()
        finally:
            if server.poll() is None:
                server.kill()
                server.wait(timeout=3)
            if target is not None:
                try:
                    os.kill(target, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if not server.stdin.closed:
                server.stdin.close()
            server.stdout.close()


def test_http_server_keeps_its_own_signal_handling(monkeypatch):
    from topdown import mcp_server

    called = []
    previous = signal.getsignal(signal.SIGTERM)
    server = SimpleNamespace(run=lambda **kwargs: called.append(kwargs))
    monkeypatch.setattr(mcp_server, "_create_mcp", lambda **kwargs: server)
    monkeypatch.setattr(mcp_server, "_register_tools_and_resources", lambda server: None)
    mcp_server.run_server(transport="http")
    assert called == [{"transport": "streamable-http"}]
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.asyncio
async def test_stdio_reader_preserves_split_unicode_and_unterminated_lines(tmp_path):
    from topdown.mcp_runtime import _lines

    path = tmp_path / "input"
    text = "a" * 65535 + "🙂\nsecond\r\nlast"
    path.write_bytes(text.encode())
    with path.open("rb") as source:
        assert [line async for line in _lines(source.fileno())] == ["a" * 65535 + "🙂", "second", "last"]
    path.write_bytes(b"\xff\n\n")
    with path.open("rb") as source:
        assert [line async for line in _lines(source.fileno())] == ["�", ""]


@pytest.mark.asyncio
async def test_stdio_file_writer_preserves_utf8(tmp_path):
    from topdown.mcp_runtime import _Output

    path = tmp_path / "output"
    with path.open("wb") as destination:
        writer = _Output(destination.fileno())
        await writer.write("🙂\n" * 10000)
        await writer.flush()
    assert path.read_text() == "🙂\n" * 10000


@pytest.mark.asyncio
async def test_eof_cancels_old_sdk_handler_join_and_waits_for_cleanup(monkeypatch, tmp_path):
    from topdown import mcp_runtime

    cleaned = []

    async def run(reader, writer, options):
        try:
            async for _ in reader:
                pass
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(.01)
                cleaned.append(True)

    native = SimpleNamespace(run=run, create_initialization_options=lambda: None)
    with (tmp_path / "input").open("w+b") as source, (tmp_path / "output").open("wb") as destination:
        monkeypatch.setattr(sys, "stdin", source)
        monkeypatch.setattr(sys, "stdout", destination)
        with anyio.fail_after(1):
            await mcp_runtime.serve_stdio(SimpleNamespace(_mcp_server=native))
        assert os.get_blocking(source.fileno())
        assert os.get_blocking(destination.fileno())
    assert cleaned == [True]


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe readiness")
@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["read", "write"])
async def test_open_pipe_operations_remain_cancellable(direction):
    from topdown.mcp_runtime import _lines, _Output

    source, destination = os.pipe()
    try:
        os.set_blocking(source, False)
        os.set_blocking(destination, False)
        with anyio.move_on_after(.03) as cancelled:
            if direction == "read":
                await anext(_lines(source))
            else:
                await _Output(destination).write("z" * 10000000)
        assert cancelled.cancel_called
    finally:
        os.close(source)
        os.close(destination)


@pytest.mark.asyncio
async def test_stdio_pipe_races_retry_without_losing_bytes(monkeypatch, tmp_path):
    from topdown import mcp_runtime

    path = tmp_path / "race"
    path.write_bytes(b"value\n")
    original_read, original_write = os.read, os.write
    attempts = {"read": 0, "write": 0}

    def read(fd, size):
        attempts["read"] += 1
        if attempts["read"] == 1:
            raise BlockingIOError()
        return original_read(fd, size)

    def write(fd, data):
        attempts["write"] += 1
        if attempts["write"] == 1:
            raise BlockingIOError()
        return original_write(fd, data[:2])

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(os, "write", write)
    with path.open("rb") as source:
        assert [line async for line in mcp_runtime._lines(source.fileno())] == ["value"]
    with path.open("wb") as destination:
        await mcp_runtime._Output(destination.fileno()).write("🙂end")
    assert path.read_text() == "🙂end"


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking descriptors")
@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_stdio_restores_descriptor_modes(monkeypatch, tmp_path, fail):
    from topdown import mcp_runtime

    async def run(reader, writer, options):
        assert options == "options"
        assert not os.get_blocking(source.fileno())
        assert not os.get_blocking(destination.fileno())
        if fail:
            raise ValueError("failed transport")
        await writer.aclose()

    native = SimpleNamespace(run=run, create_initialization_options=lambda: "options")
    with (tmp_path / "input").open("w+b") as source, (tmp_path / "output").open("wb") as destination:
        with monkeypatch.context() as scoped:
            scoped.setattr(sys, "stdin", source)
            scoped.setattr(sys, "stdout", destination)
            os.set_blocking(destination.fileno(), False)
            try:
                if fail:
                    with pytest.raises(Exception, match="unhandled errors"):
                        await mcp_runtime.serve_stdio(SimpleNamespace(_mcp_server=native))
                else:
                    await mcp_runtime.serve_stdio(SimpleNamespace(_mcp_server=native))
            finally:
                assert os.get_blocking(source.fileno())
                assert not os.get_blocking(destination.fileno())


@pytest.mark.asyncio
async def test_nonposix_retains_sdk_transport(monkeypatch):
    from topdown import mcp_runtime

    called = []

    async def serve():
        called.append(True)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "name", "nt")
        await mcp_runtime.serve_stdio(SimpleNamespace(run_stdio_async=serve))
    assert called == [True]


@pytest.mark.asyncio
async def test_zero_length_stdout_write_fails_instead_of_spinning(monkeypatch, tmp_path):
    from topdown.mcp_runtime import _Output

    with (tmp_path / "output").open("wb") as destination:
        monkeypatch.setattr(os, "write", lambda fd, data: 0)
        with pytest.raises(BrokenPipeError, match="no progress"):
            await _Output(destination.fileno()).write("data")


@pytest.mark.skipif(os.name != "posix", reason="POSIX null device readiness")
@pytest.mark.asyncio
async def test_null_devices_do_not_enter_the_readiness_selector(monkeypatch):
    from topdown.mcp_runtime import _lines, _Output

    async def unsupported(fd):
        raise AssertionError("null devices cannot be registered with epoll/kqueue")

    monkeypatch.setattr(anyio, "wait_readable", unsupported)
    monkeypatch.setattr(anyio, "wait_writable", unsupported)
    with open(os.devnull, "r+b") as device:
        assert [line async for line in _lines(device.fileno())] == []
        await _Output(device.fileno()).write("🙂\n")


@pytest.mark.skipif(os.name != "posix", reason="POSIX null device startup")
def test_stdio_server_with_devnull_input_exits_cleanly():
    result = subprocess.run([sys.executable, "-m", "topdown.cli", "mcp-serve"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, timeout=6, cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, "TOPDOWN_AUTH_MODE": "none"})
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_stdio_preserves_universal_newlines_across_read_boundaries(tmp_path):
    from topdown.mcp_runtime import _lines

    path = tmp_path / "input"
    path.write_bytes(b"a" * 65535 + b"\r\n" + "🙂\rlast\n".encode())
    with path.open("rb") as source:
        assert [line async for line in _lines(source.fileno())] == ["a" * 65535, "🙂", "last"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe readiness")
@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
def test_real_stdio_accepts_existing_line_endings(newline):
    server = subprocess.Popen([sys.executable, "-m", "topdown.cli", "mcp-serve"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              cwd=Path(__file__).resolve().parents[1], env={**os.environ, "TOPDOWN_AUTH_MODE": "none"})
    try:
        message = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "newline-test", "version": "1"}}}
        server.stdin.write(json.dumps(message) + newline)
        server.stdin.flush()
        assert select.select([server.stdout], [], [], 6)[0], "server did not read the request"
        assert json.loads(server.stdout.readline())["id"] == 0
        server.stdin.close()
        assert server.wait(timeout=6) == 0
    finally:
        if server.poll() is None:
            server.kill()
            server.wait(timeout=3)
        if not server.stdin.closed:
            server.stdin.close()
        server.stdout.close()
        server.stderr.close()
