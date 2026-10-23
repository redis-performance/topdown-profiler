"""Run native MCP work outside the event loop with cooperative cancellation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
import os
import signal
import subprocess
import threading
import time

import anyio

from topdown.collector.apple import AppleProfileError

_STOP: ContextVar[threading.Event | None] = ContextVar("apple_request_cancellation", default=None)
_CAPTURES = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apple-capture")
_READERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="apple-reader")


def check_cancelled():
    stop = _STOP.get()
    if stop is not None and stop.is_set():
        raise AppleProfileError("Apple request cancelled; local artifacts are retained", code="interrupted")


@contextmanager
def uncancellable():
    token = _STOP.set(None)
    try:
        yield
    finally:
        _STOP.reset(token)


def sleep(seconds):
    stop = _STOP.get()
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        check_cancelled()


def worker(function, *, capture=False):
    @wraps(function)
    async def invoke(*args, **kwargs):
        stop = threading.Event()
        token = _STOP.set(stop)
        try:
            context = copy_context()
            def call():
                check_cancelled()
                return function(*args, **kwargs)
            submitted = (_CAPTURES if capture else _READERS).submit(context.run, call)
            future = asyncio.wrap_future(submitted)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                stop.set()
                submitted.cancel()
                with anyio.CancelScope(shield=True):
                    while True:
                        try:
                            await asyncio.shield(future)
                            break
                        except asyncio.CancelledError:
                            if future.cancelled():
                                break
                        except Exception:
                            break
                raise
        finally:
            _STOP.reset(token)
    return invoke


def _signal_owned(process, sig):
    if process.returncode is not None:
        return
    if os.name == "posix":
        from topdown.collector.apple_stat import _peek_exit

        try:
            _peek_exit(process.pid)
        except ChildProcessError as exc:
            raise AppleProfileError("Native tool ownership was lost; no cleanup signal sent") from exc
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    elif sig == signal.SIGTERM:
        process.terminate()
    else:
        process.kill()


def run(command, *, timeout, check=False, env=None, start_new_session=False, stdin=None):
    if _STOP.get() is None:
        options = {"start_new_session": True} if start_new_session else {}
        if stdin is not None:
            options["stdin"] = stdin
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=check, env=env, **options)
    check_cancelled()
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    output, error = b"", b""
    try:
        while True:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout, output=output, stderr=error)
            try:
                output, error = process.communicate(timeout=min(.1, remaining))
                break
            except subprocess.TimeoutExpired as exc:
                output, error = exc.output or b"", exc.stderr or b""
        check_cancelled()
        result = subprocess.CompletedProcess(command, process.returncode, output, error)
        if check:
            result.check_returncode()
        return result
    except BaseException as exc:
        try:
            _signal_owned(process, signal.SIGTERM)
            try:
                output, error = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                _signal_owned(process, signal.SIGKILL)
                output, error = process.communicate(timeout=2)
        except (OSError, subprocess.SubprocessError, AppleProfileError) as cleanup:
            exc.cleanup_error = str(cleanup)
            if hasattr(exc, "add_note"):
                exc.add_note(f"Native tool cleanup failed: {cleanup}")
        if isinstance(exc, (AppleProfileError, subprocess.TimeoutExpired)):
            exc.stdout = output.encode() if isinstance(output, str) else output
            exc.stderr = error.encode() if isinstance(error, str) else error
        raise
