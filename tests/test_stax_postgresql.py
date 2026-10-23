"""The same Stax document and pages must survive both SQL dialects."""

import copy
import json
from pathlib import Path

import pytest

from tests.test_apple_postgresql import postgres_backend  # noqa: F401
from tests.test_stax_archive import archive_document
from topdown.stax_storage import (
    BULK_FIELDS,
    get_stax_hotspots,
    get_stax_target_spans,
    get_stax_thread_waits,
    query_stax_result,
    store_stax_result,
)
from topdown.storage.base import StorageBackend
from topdown.storage.models import Run


@pytest.fixture(params=["sqlite_backend", "postgres_backend"])
def sampling_backend(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def stored_sampling(sampling_backend, tmp_path):
    path = tmp_path / "profile.stax"
    path.write_text(json.dumps(archive_document()))
    return sampling_backend, store_stax_result(sampling_backend, path)


@pytest.mark.parametrize("section", ["summary", *sorted(BULK_FIELDS)])
def test_sql_pages_do_not_load_full_document(stored_sampling, monkeypatch, section):
    backend, run = stored_sampling
    original = backend.get_stax_result(run.run_id)
    monkeypatch.setattr(backend, "get_stax_result", lambda *a: pytest.fail("loaded full Stax document"))
    result = query_stax_result(backend, run.run_id, section, limit=1)
    assert result["metadata"] == original["metadata"]
    assert result["counts"]["samples"] == 1
    if section != "summary":
        assert len(result["rows"]) == min(1, len(original["result"][section]))
    if section == "samples":
        assert result["rows"][0]["frames"][0]["name"] == "hot"
        assert result["rows"][0]["pmc"]["cycles"] == 2**64 - 1


def test_sql_reports_use_projected_documents(stored_sampling, monkeypatch):
    backend, run = stored_sampling
    monkeypatch.setattr(backend, "get_stax_result", lambda *a: pytest.fail("loaded full Stax document"))
    assert get_stax_hotspots(backend, run.run_id)["rows"][0]["name"] == "hot"
    assert get_stax_thread_waits(backend, run.run_id)["rows"][0]["duration_ns"] == 30


def test_real_m6_metal_excerpt_round_trip(sampling_backend):
    path = Path(__file__).parent / "test_data" / "stax_v3_m6_metal_excerpt.json"
    run = store_stax_result(sampling_backend, path, labels={"host_chip": "apple-m6"})
    hotspots = get_stax_hotspots(sampling_backend, run.run_id)
    assert hotspots["denominator_samples"] == 0
    assert hotspots["rows"] == []
    targets = get_stax_target_spans(sampling_backend, run.run_id)
    assert targets["rows"][0]["span_count"] == 2
    assert targets["rows"][0]["sum_target_span_ns"] == 13583
    assert targets["rows"][0]["leaf"]["name"] == "M6 Metal add_one"
    page = query_stax_result(sampling_backend, run.run_id, "intervals", 1, 1)
    assert page["rows"][0]["start_ns"] == 192443916527292
    assert page["rows"][0]["duration_ns"] == 8333
    assert page["rows"][0]["origin_tid"] == 1526448
    assert page["rows"][0]["lane_kind"] == "Metal"


@pytest.mark.parametrize("native_text", ["zero\x00byte", "quotes'\"\\ and Ω", "\ud800legacy"])
def test_encoded_native_documents_retain_strings_and_integer_pages(stored_sampling, native_text):
    backend, original_run = stored_sampling
    document = copy.deepcopy(backend.get_stax_result(original_run.run_id))
    document["result"]["frame_table"][0]["name"] = native_text
    run = Run(labels={"collector": "stax"})
    backend.insert_stax_result(run, document)
    assert backend.get_stax_result(run.run_id) == document
    page = backend.get_stax_result_page(run.run_id, tuple(BULK_FIELDS), "samples", 0, 1)
    assert page["rows"][0]["pmc"]["cycles"] == 2**64 - 1
    frame_page = backend.get_stax_result_page(run.run_id, tuple(BULK_FIELDS), "frame_table", 0, 1)
    assert frame_page["rows"][0]["name"] == native_text


def test_backend_fallback_preserves_compact_rows(stored_sampling, monkeypatch):
    backend, run = stored_sampling
    monkeypatch.setattr(type(backend), "get_stax_result_page", StorageBackend.get_stax_result_page)
    page = query_stax_result(backend, run.run_id, "samples", 0, 1)
    assert page["rows"][0]["pmc"]["cycles"] == 2**64 - 1
    assert page["rows"][0]["frames"][0]["name"] == "hot"


def test_missing_uuid_and_empty_page(stored_sampling):
    backend, run = stored_sampling
    assert backend.get_stax_result("not-a-uuid") is None
    assert backend.get_stax_result_page("not-a-uuid", ()) is None
    assert query_stax_result(backend, run.run_id, "samples", 10**50, 2)["rows"] == []


def test_stax_insert_rollback_does_not_leave_run(sampling_backend, tmp_path):
    backend = sampling_backend
    conn = backend._get_conn()
    sqlite = backend.PH == "?"
    statement = (
        "CREATE TRIGGER reject_stax BEFORE INSERT ON stax_results BEGIN SELECT RAISE(ABORT, 'test rejection'); END"
        if sqlite
        else "ALTER TABLE stax_results ADD CONSTRAINT reject_stax CHECK (false)"
    )
    conn.execute(statement)
    conn.commit()
    path = tmp_path / "profile.stax"
    path.write_text(json.dumps(archive_document()))
    with pytest.raises(Exception, match="test rejection|reject_stax"):
        store_stax_result(backend, path)
    assert backend.list_runs() == []
    backend.insert_run(Run(process_name="connection-usable"))
