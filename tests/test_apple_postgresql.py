"""Real JSONB/transaction checks against an explicitly configured disposable database."""

import json
import os
import uuid

import pytest

from topdown.apple_storage import query_apple_result, store_apple_result
from topdown.collector.apple import TMA_ROOTS
from topdown.storage.models import Run, Sample


def test_jsonb_retains_native_strings_and_pages(postgres_backend):
    from tests.test_apple_json_strings import check_native_string_storage

    check_native_string_storage(postgres_backend)


@pytest.fixture
def postgres_backend():
    dsn = os.environ.get("TOPDOWN_TEST_DSN")
    if not dsn:
        pytest.skip("Set TOPDOWN_TEST_DSN to a disposable PostgreSQL database")
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from topdown.storage.postgresql_backend import PostgreSQLBackend

    schema = "apple_test_" + uuid.uuid4().hex
    admin = psycopg.connect(dsn, autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    backend = PostgreSQLBackend(dsn)
    backend._conn = psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={schema}")
    try:
        backend.initialize()
        yield backend
    finally:
        backend.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def native_file(tmp_path):
    result = {"measurement_kind": "process-accounted-fixed-counters", "tma_available": False,
              "tma": dict.fromkeys(TMA_ROOTS), "scope": {"pid": 42, "descendants": False},
              "counters": {"instructions": 2**64 - 1, "cycles": 2**53 + 1}, "command": ["/bin/true"]}
    path = tmp_path / "stat.json"
    path.write_text(json.dumps(result))
    return path, result


def test_jsonb_keeps_uint64_and_native_run_labels(postgres_backend, tmp_path):
    backend = postgres_backend
    path, original = native_file(tmp_path)
    run = store_apple_result(backend, path, labels={"test_name": "apple-jsonb"})
    assert backend.get_apple_result(run.run_id)["result"] == original
    assert query_apple_result(backend, run.run_id)["result"]["counters"]["instructions"] == 2**64 - 1
    assert backend.list_runs(labels={"test_name": "apple-jsonb"})[0].run_id == run.run_id
    assert backend.get_samples(run.run_id) == []
    assert backend.query_bottlenecks() == []
    assert backend.get_apple_result("bad uuid") is None
    assert backend.get_apple_result(str(uuid.uuid4())) is None
    backend._get_conn().execute("DELETE FROM runs WHERE run_id = %s", (run.run_id,))
    backend._get_conn().commit()
    assert backend.get_apple_result(run.run_id) is None


def test_failed_document_insert_rolls_back_run_and_connection(postgres_backend, tmp_path):
    import psycopg

    backend = postgres_backend
    backend._get_conn().execute("ALTER TABLE apple_results ADD CONSTRAINT reject_apple CHECK (false)")
    backend._get_conn().commit()
    path, _ = native_file(tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation):
        store_apple_result(backend, path)
    assert backend.list_runs() == []
    backend.insert_run(Run(process_name="connection-still-usable"))
    assert len(backend.list_runs()) == 1


def test_additive_schema_keeps_existing_linux_samples(postgres_backend, tmp_path):
    backend = postgres_backend
    linux = Run(process_name="linux-redis")
    backend.insert_run(linux)
    backend.insert_samples([Sample(run_id=linux.run_id, metric_name="Retiring", value=42)])
    backend._get_conn().execute("DROP TABLE apple_results")
    backend._get_conn().commit()
    backend.initialize()
    path, _ = native_file(tmp_path)
    native = store_apple_result(backend, path)
    assert backend.get_apple_result(native.run_id)
    assert backend.get_samples(linux.run_id)[0].value == 42
