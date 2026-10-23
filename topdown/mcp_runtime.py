"""Give stdio requests time to release their native resources on SIGTERM."""

import asyncio
from contextlib import ExitStack
import os
import signal
import stat
import sys
import threading

import anyio


async def _lines(fd):
    pending = []
    regular = stat.S_ISREG(os.fstat(fd).st_mode)
    while True:
        if not regular:
            await anyio.wait_readable(fd)
        else:
            await anyio.lowlevel.checkpoint()
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if not chunk:
            if pending:
                yield b"".join(pending).decode("utf-8", errors="replace")
            return
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            pending.append(part)
            yield b"".join(pending).decode("utf-8", errors="replace")
            pending.clear()
        if parts[-1]:
            pending.append(parts[-1])


class _Output:
    def __init__(self, fd):
        self.fd = fd
        self.regular = stat.S_ISREG(os.fstat(fd).st_mode)

    async def write(self, text):
        remaining = memoryview(text.encode("utf-8"))
        while remaining:
            if not self.regular:
                await anyio.wait_writable(self.fd)
            else:
                await anyio.lowlevel.checkpoint()
            try:
                written = os.write(self.fd, remaining)
            except BlockingIOError:
                continue
            if written == 0:
                raise BrokenPipeError("MCP stdout made no progress")
            remaining = remaining[written:]

    async def flush(self):
        pass


async def serve_stdio(server):
    if os.name != "posix":
        await server.run_stdio_async()
        return
    from mcp.server.stdio import stdio_server

    # The SDK's threaded stdin read cannot be cancelled while a client keeps its pipe open.
    with ExitStack() as stack:
        source, destination = sys.stdin.fileno(), sys.stdout.fileno()
        for fd in (source, destination):
            blocking = os.get_blocking(fd)
            os.set_blocking(fd, False)
            stack.callback(os.set_blocking, fd, blocking)
        async with stdio_server(stdin=_lines(source), stdout=_Output(destination)) as (reader, writer):
            native = server._mcp_server
            await native.run(reader, writer, native.create_initialization_options())


def run_stdio(serve):
    async def run():
        if threading.current_thread() is not threading.main_thread():
            await serve()
            return
        terminated = False
        loop = asyncio.get_running_loop()
        with anyio.CancelScope() as shutdown:
            def terminate(signum, frame):
                nonlocal terminated
                terminated = True
                loop.call_soon_threadsafe(shutdown.cancel)

            previous = signal.signal(signal.SIGTERM, terminate)
            try:
                await serve()
            finally:
                signal.signal(signal.SIGTERM, previous)
        if terminated:
            raise SystemExit(128 + signal.SIGTERM)

    anyio.run(run)
