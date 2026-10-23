"""Native query pages must avoid full document loads without changing JSON values."""

import sqlite3
from unittest.mock import Mock

import pytest

from topdown.apple_storage import BULK_FIELDS, query_apple_result
from topdown.collector.apple import AppleProfileError
from topdown.storage.base import StorageBackend
from topdown.storage.models import Run
from tests.test_apple_postgresql import postgres_backend  # noqa: F401


@pytest.fixture(params=["sqlite_backend", "postgres_backend"])
def native_backend(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def native_document():
    rows = [{"index": i, "counter": 2**64 - 1, "nested": [2**63 + 1, True, None],
             "label": 'native "quotes" \\ and Ω; DROP TABLE apple_results;'} for i in range(23)]
    result = {"measurement_kind": "process-accounted-fixed-counters", "scope": {"pid": 42},
              "counters": {"instructions": 2**64 - 1, "cycles": 2**53 + 1}, "derived_total": 2**70,
              **{field: rows for field in BULK_FIELDS}}
    return {"schema_version": 1, "result": result,
            "metadata": {"time_basis": "import_time", "capture": {"native": "保留", "uint64": 2**64 - 1}}}


def store(backend, document):
    run = Run(process_name="native-page-test", labels={"collector": "apple", "test_name": "bounded-native-query"})
    backend.insert_apple_result(run, document)
    return run.run_id


@pytest.mark.parametrize("section", ["summary", "samples", "intervals", "gaps", "channels", "leaf_sample_counts"])
def test_queries_project_native_summary_and_one_ordered_page(native_backend, native_document, monkeypatch, section):
    run_id = store(native_backend, native_document)
    monkeypatch.setattr(native_backend, "get_apple_result", lambda *a: pytest.fail("loaded full native document"))
    view = query_apple_result(native_backend, run_id, section, 9, 3)
    assert view["metadata"] == native_document["metadata"]
    assert view["result"] == {k: v for k, v in native_document["result"].items() if k not in BULK_FIELDS}
    assert view["result"]["derived_total"] == 2**70
    if section != "summary":
        assert view["rows"] == native_document["result"][section][9:12]
        assert view["total"] == 23 and view["offset"] == 9 and view["limit"] == 3
        assert view["rows"][0]["counter"] == 2**64 - 1


@pytest.mark.parametrize("offset", [0, 1, 3, 6, 10, 12, 2**100])
def test_scalar_and_nested_array_pages_keep_exact_json_types(native_backend, native_document, offset):
    rows = [None, True, False, 2**64 - 1, 2**63 + 1, -(2**80), 3.125, 'quoted " value Ω',
            [], {"nested": [2**64 - 1, True]}, [2**64 - 1, False], "last"]
    native_document["result"]["leaf_sample_counts"] = rows
    run_id = store(native_backend, native_document)
    response = query_apple_result(native_backend, run_id, "leaf_sample_counts", offset, 3)
    assert response["rows"] == rows[offset:offset + 3]
    assert response["total"] == len(rows) and response["offset"] == offset
    assert native_backend.get_apple_result(run_id) == native_document


@pytest.mark.parametrize("value", [None, False, 12, {}, "not an array"])
def test_non_array_sections_keep_the_existing_native_query_error(native_backend, native_document, value):
    native_document["result"]["channels"] = value
    run_id = store(native_backend, native_document)
    with pytest.raises(AppleProfileError, match="no channels section"):
        query_apple_result(native_backend, run_id, "channels")


def test_empty_and_missing_sections_have_distinct_meanings(native_backend, native_document):
    native_document["result"]["samples"] = []
    del native_document["result"]["gaps"]
    run_id = store(native_backend, native_document)
    assert query_apple_result(native_backend, run_id, "samples")["rows"] == []
    assert query_apple_result(native_backend, run_id, "samples")["total"] == 0
    with pytest.raises(AppleProfileError, match="no gaps section"):
        query_apple_result(native_backend, run_id, "gaps")
    assert native_backend.get_apple_result_page(Run().run_id, tuple(BULK_FIELDS)) is None
    assert native_backend.get_apple_result_page("not-a-uuid", tuple(BULK_FIELDS)) is None
    with pytest.raises(AppleProfileError, match="No stored"):
        query_apple_result(native_backend, Run().run_id)


def test_default_backend_projection_retains_compatibility(sqlite_backend, native_document, monkeypatch):
    run_id = store(sqlite_backend, native_document)
    monkeypatch.setattr(type(sqlite_backend), "get_apple_result_page", StorageBackend.get_apple_result_page)
    response = query_apple_result(sqlite_backend, run_id, "samples", 1, 2)
    assert response["rows"] == native_document["result"]["samples"][1:3]
    assert response["total"] == 23
    assert native_document["result"]["samples"] and "samples" not in response["result"]
    with pytest.raises(AppleProfileError, match="No stored"):
        query_apple_result(sqlite_backend, Run().run_id)


@pytest.mark.parametrize("message", ["no such function: json_remove", "database is locked"])
def test_sqlite_only_falls_back_when_json_functions_are_unavailable(sqlite_backend, native_document, monkeypatch, message):
    run_id = store(sqlite_backend, native_document)
    connection = sqlite_backend._get_conn()
    proxy = Mock(wraps=connection)
    def execute(sql, *args):
        if "json_remove" in sql:
            raise sqlite3.OperationalError(message)
        return connection.execute(sql, *args)
    proxy.execute.side_effect = execute
    monkeypatch.setattr(sqlite_backend, "_get_conn", lambda: proxy)
    if "no such function" in message:
        assert query_apple_result(sqlite_backend, run_id, "samples", 1, 2)["rows"] == native_document["result"]["samples"][1:3]
    else:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            query_apple_result(sqlite_backend, run_id)


def test_omitting_no_fields_retains_the_native_document(native_backend, native_document):
    run_id = store(native_backend, native_document)
    view = native_backend.get_apple_result_page(run_id, (), "samples", 1, 2)
    assert view["result"] == native_document["result"]
    assert view["rows"] == native_document["result"]["samples"][1:3]
    assert native_backend.get_apple_result(run_id) == native_document
