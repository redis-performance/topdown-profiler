"""Real JSONB/transaction checks against an explicitly configured disposable database."""

import json
import os
import uuid
from pathlib import Path

import pytest

from topdown.apple_storage import query_apple_result, store_apple_result
from topdown.collector.apple import TMA_ROOTS
from topdown.storage.models import Run, Sample


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


def test_gpu_jsonb_preserves_native_limiters_and_encoder_pagination(postgres_backend, tmp_path):
    from tests.test_apple_gpu_integration import check_round_trip

    check_round_trip(postgres_backend, tmp_path)


def test_jsonb_measured_m6_profiles_keep_counters_and_coverage(postgres_backend, tmp_path):
    from tests.test_apple_gpu_integration import check_measured_profiles

    check_measured_profiles(postgres_backend, tmp_path)


def test_jsonb_measured_m6_mixed_encoder_profile(postgres_backend, tmp_path):
    from tests.test_apple_gpu_integration import check_mixed_profile

    check_mixed_profile(postgres_backend, tmp_path)


def test_jsonb_preserves_measured_gpu_timeline_units_and_absence(postgres_backend, tmp_path):
    from tests.test_apple_gpu_timeline_metrics import check_native_timeline_evidence

    check_native_timeline_evidence(postgres_backend, tmp_path)


def test_jsonb_preserves_measured_fixed_instruction_snapshots(postgres_backend, tmp_path):
    from tests.test_apple_fixed_instruction_validation import check_measured_fixed_instruction_storage

    check_measured_fixed_instruction_storage(postgres_backend, tmp_path)


def test_jsonb_preserves_measured_dependent_read_gpu_profiles(postgres_backend, tmp_path):
    from tests.test_apple_gpu_dependent_reads import check_measured_dependent_read_storage

    check_measured_dependent_read_storage(postgres_backend, tmp_path)


@pytest.mark.parametrize("failure", ["components", "duplicate-fields", "metadata", "timestamps"])
def test_inconsistent_apple_imports_fail_before_jsonb_insert(postgres_backend, tmp_path, failure):
    from topdown.collector.apple import AppleProfileError

    path, original = native_file(tmp_path)
    if failure == "components":
        original["counters"].update(performance_instructions=2**64 - 1, other_instructions=1)
        path.write_text(json.dumps(original))
    elif failure == "duplicate-fields":
        path.write_text(path.read_text().replace('"cycles":', '"cycles": 0, "cycles":'))
    elif failure == "metadata":
        path = path.rename(tmp_path / "profile.json")
        (tmp_path / "capture.json").write_text('{"environment":{"invalid":1e999}}')
    else:
        original.update(started_at="", ended_at="2026-10-23T10:00:00Z")
        path.write_text(json.dumps(original))
    with pytest.raises(AppleProfileError):
        store_apple_result(postgres_backend, path)
    assert postgres_backend.list_runs() == []
    valid, original = native_file(tmp_path)
    run = store_apple_result(postgres_backend, valid)
    assert postgres_backend.get_apple_result(run.run_id)["result"] == original


def test_trace_context_validation_has_the_same_jsonb_contract(postgres_backend, tmp_path):
    from topdown.collector.apple import AppleProfileError
    from topdown.collector.apple_schedule import annotate_scheduling
    from topdown.collector.apple_trace import parse_trace_profile

    data = Path(__file__).parent / "test_data"
    result = parse_trace_profile(data / "apple_processor_intervals_m6.xml", data / "apple_processor_gaps_m6.xml", 42)
    result["measurement_kind"] = "hardware-traced-execution-intervals"
    annotate_scheduling(result, data / "apple_thread_states_m6.xml")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(result))
    run = store_apple_result(postgres_backend, path)
    assert postgres_backend.get_apple_result(run.run_id)["result"] == result
    result["intervals"][2]["scheduling"]["single_running_core"]["core"] = 99
    path.write_text(json.dumps(result))
    with pytest.raises(AppleProfileError):
        store_apple_result(postgres_backend, path)
    assert len(postgres_backend.list_runs()) == 1


def test_jsonb_preserves_capture_host_labels_across_imports(postgres_backend, tmp_path, monkeypatch):
    from tests.test_apple_environment import check_import_environment

    check_import_environment(postgres_backend, tmp_path, monkeypatch)
