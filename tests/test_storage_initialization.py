"""First-use schema initialization must work across independent MCP backends."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from tests.test_apple_postgresql import postgres_backend  # noqa: F401
from topdown.storage import get_backend
from topdown.storage.models import Run


@pytest.mark.parametrize("kind", ["sqlite", "postgresql"])
def test_factory_closes_backend_when_initialization_fails(monkeypatch, kind):
    closed = []

    def fail():
        raise RuntimeError("schema initialization failed")

    backend = SimpleNamespace(initialize=fail, close=lambda: closed.append(True))
    if kind == "postgresql":
        pytest.importorskip("psycopg")
        monkeypatch.setattr("topdown.storage.postgresql_backend.PostgreSQLBackend", lambda **kwargs: backend)
    else:
        monkeypatch.setattr("topdown.storage.SQLiteBackend", lambda **kwargs: backend)
    config = SimpleNamespace(backend=kind, dsn="test-dsn", db_path="unused")
    with pytest.raises(RuntimeError, match="schema initialization failed"):
        get_backend(config)
    assert closed == [True]


def test_concurrent_first_use_initializes_one_schema(request):
    backend = request.getfixturevalue("postgres_backend")
    from psycopg import sql
    from topdown.storage.postgresql_backend import PostgreSQLBackend

    connection = backend._get_conn()
    schema = connection.execute("SELECT current_schema()").fetchone()["current_schema"]
    connection.execute("DROP TABLE apple_results, stax_results, samples, runs CASCADE")
    connection.commit()
    ready = threading.Barrier(4)

    def initialize():
        worker = PostgreSQLBackend(backend.dsn)
        try:
            worker._get_conn().execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            ready.wait(timeout=10)
            worker.initialize()
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(initialize) for _ in range(4)]
        for future in futures:
            future.result(timeout=15)
    run = Run(process_name="usable-after-concurrent-initialization")
    backend.insert_run(run)
    assert backend.get_run(run.run_id).process_name == run.process_name


def test_failed_initialization_rolls_back_the_connection(request, monkeypatch):
    backend = request.getfixturevalue("postgres_backend")
    import psycopg

    with monkeypatch.context() as patch:
        patch.setattr("topdown.storage.postgresql_backend.POSTGRESQL_SCHEMA", "SELECT missing_schema_init_function()")
        with pytest.raises(psycopg.Error):
            backend.initialize()
    backend.initialize()
    run = Run(process_name="usable-after-initialization-failure")
    backend.insert_run(run)
    assert backend.get_run(run.run_id).process_name == run.process_name
