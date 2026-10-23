"""Cancellation and process interruption must not leave half-stored native runs."""

import asyncio
from contextlib import closing
import sqlite3
import threading

import pytest

from topdown.collector.apple import AppleProfileError
from topdown.collector.apple_runtime import _STOP
from topdown.storage.models import Run


class Connection:
    def __init__(self, native, interrupt):
        self.native = native
        self.interrupt = interrupt
        self.rollbacks = 0

    def __getattr__(self, name):
        return getattr(self.native, name)

    def execute(self, sql, *args, **kwargs):
        result = self.native.execute(sql, *args, **kwargs)
        self.interrupt(sql)
        return result

    def rollback(self):
        self.rollbacks += 1
        return self.native.rollback()


def check_interrupted_transaction(backend, monkeypatch, phase, failure):
    native = backend._get_conn()
    stop = threading.Event()
    token = _STOP.set(stop)
    expected = {"cancel": AppleProfileError, "keyboard": KeyboardInterrupt, "exit": SystemExit}[failure]

    def interrupt(sql):
        table = "runs" if phase == "after-run" else "apple_results"
        if sql.strip().startswith("INSERT INTO " + table + " "):
            if failure == "cancel":
                stop.set()
            elif failure == "keyboard":
                raise KeyboardInterrupt
            else:
                raise SystemExit(143)

    proxy = Connection(native, interrupt)
    document = {"schema_version": 1, "metadata": {}, "result": {
        "measurement_kind": "process-accounted-fixed-counters", "counters": {"instructions": 2**64 - 1}}}
    cancelled = Run()
    try:
        with monkeypatch.context() as scoped:
            def connect():
                if phase == "connected":
                    stop.set()
                return proxy

            scoped.setattr(backend, "_get_conn", connect)
            if phase == "before-encode":
                stop.set()
            with pytest.raises(expected):
                backend.insert_apple_result(cancelled, document)
            assert proxy.rollbacks == (0 if phase == "before-encode" else 1)
    finally:
        _STOP.reset(token)
    native.commit()
    assert backend.get_run(cancelled.run_id) is None
    assert backend.get_apple_result(cancelled.run_id) is None
    assert native.execute("SELECT count(*) AS count FROM runs").fetchone()["count"] == 0
    assert native.execute("SELECT count(*) AS count FROM apple_results").fetchone()["count"] == 0
    completed = Run()
    assert backend.insert_apple_result(completed, document) == completed.run_id
    assert backend.get_apple_result(completed.run_id) == document


@pytest.mark.parametrize("phase,failure", [("before-encode", "cancel"), ("connected", "cancel"), ("after-run", "cancel"),
                                          ("after-document", "cancel"), ("after-run", "keyboard"),
                                          ("after-document", "keyboard"), ("after-run", "exit"),
                                          ("after-document", "exit")])
def test_sqlite_interruption_rolls_back_both_rows_and_reuses_connection(sqlite_backend, monkeypatch, phase, failure):
    check_interrupted_transaction(sqlite_backend, monkeypatch, phase, failure)


def check_committed_result_survives_late_cancellation(backend, monkeypatch):
    stop = threading.Event()
    token = _STOP.set(stop)
    native = backend._get_conn()
    proxy = Connection(native, lambda sql: None)

    def commit():
        native.commit()
        stop.set()

    document = {"result": {"measurement_kind": "process-accounted-fixed-counters"}}
    run = Run()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(backend, "_get_conn", lambda: proxy)
            scoped.setattr(proxy, "commit", commit)
            assert backend.insert_apple_result(run, document) == run.run_id
            assert stop.is_set() and proxy.rollbacks == 0
    finally:
        _STOP.reset(token)
    assert backend.get_run(run.run_id) is not None
    assert backend.get_apple_result(run.run_id) == document


def test_sqlite_committed_result_survives_late_cancellation(sqlite_backend, monkeypatch):
    check_committed_result_survives_late_cancellation(sqlite_backend, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["import_apple_result", "collect_apple_profile"])
async def test_mcp_cancellation_during_storage_waits_for_rollback(tmp_path, monkeypatch, tool):
    from tests.test_apple_postgresql import native_file
    from topdown import mcp_server
    from topdown.storage.sqlite_backend import SQLiteBackend

    path, _ = native_file(tmp_path)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    db = tmp_path / "native.db"
    observed = []

    def backend():
        storage = SQLiteBackend(db)
        storage.initialize()
        native = storage._get_conn()

        def pause(sql):
            if sql.startswith("INSERT INTO apple_results "):
                observed.append(_STOP.get())
                entered.set()
                assert release.wait(4), "test did not release native storage"

        storage._conn = Connection(native, pause)
        original_close = storage.close

        def close():
            original_close()
            closed.set()

        storage.close = close
        return storage

    monkeypatch.setattr(mcp_server, "_get_backend", backend)
    monkeypatch.setattr("topdown.apple_storage.collect_apple_result", lambda *args: path)
    args = {"path": str(path)} if tool == "import_apple_result" else {"command": ["/unused/target"]}
    task = asyncio.create_task(mcp_server.mcp.call_tool(tool, args))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert observed[0] is not None
        task.cancel()
        for _ in range(100):
            if observed[0].is_set():
                break
            await asyncio.sleep(.01)
        assert observed[0].is_set()
        assert not task.done(), "request returned before storage cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert closed.is_set()
        with closing(sqlite3.connect(db)) as conn:
            assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM apple_results").fetchone()[0] == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
