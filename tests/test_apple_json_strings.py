"""Native payload strings remain exact while run metadata stays usable in SQL."""

import json
from pathlib import Path

import pytest

from topdown.apple_storage import store_apple_result, validate_run_options
from topdown.collector.apple import AppleProfileError, TMA_ROOTS
from topdown.apple_json import TEXT_KEY, decode_document, encode_document
from topdown.storage.models import Run


@pytest.mark.parametrize("value", ["ordinary", "Δ 😀", r"literal \u0000", r"literal \ud800"])
def test_representable_strings_keep_the_existing_jsonb_document_shape(value):
    document = {"result": {"measurement_kind": "native-test", "value": value, "counter": 2**64 - 1},
                "metadata": {"empty": None, "bool": False}}
    encoded = json.loads(encode_document(document))
    assert encoded == document and TEXT_KEY not in encoded
    assert decode_document(encoded) == json.loads(json.dumps(document))


@pytest.mark.parametrize("value", ["before\x00after", "\ud800", "\udfff", "\ud83d\ude00"])
def test_jsonb_incompatible_strings_use_a_lossless_ascii_payload(value):
    document = {"result": {"measurement_kind": "native-test", "value": value,
                            "samples": [value, {value: 2**64 - 1}, None]}, "metadata": {"original": value}}
    encoded = json.loads(encode_document(document))
    assert set(encoded) == {TEXT_KEY}
    assert encoded[TEXT_KEY].isascii()
    assert decode_document(encoded) == json.loads(json.dumps(document))


def test_storage_marker_inside_a_normal_document_is_not_an_encoding_envelope():
    document = {TEXT_KEY: "ordinary metadata", "result": {"measurement_kind": "native-test"}, "metadata": {}}
    assert decode_document(json.loads(encode_document(document))) == document


@pytest.mark.parametrize("field", ["key", "value", "process_name"])
@pytest.mark.parametrize("value", ["before\x00after", "\ud800", "\udfff"])
def test_unusable_run_metadata_fails_before_artifact_reads(sqlite_backend, tmp_path, field, value):
    labels = {value: "normal"} if field == "key" else {"normal": value} if field == "value" else None
    name = value if field == "process_name" else None
    with pytest.raises(AppleProfileError, match="valid Unicode without NUL"):
        store_apple_result(sqlite_backend, tmp_path / "does-not-exist.json", name, labels)
    assert sqlite_backend.list_runs() == []


def test_valid_unicode_run_metadata_remains_supported():
    validate_run_options("process Δ 😀", {"unicode key Δ": "newline\n emoji 😀", "literal": r"\u0000"})


def test_captured_hardware_metadata_is_validated_before_insert(sqlite_backend, tmp_path):
    result = {"measurement_kind": "process-accounted-fixed-counters", "tma_available": False,
              "tma": dict.fromkeys(TMA_ROOTS), "scope": {"pid": 42},
              "counters": {"instructions": 1, "cycles": 1}, "environment": {"cpu": "chip\x00suffix"}}
    path = tmp_path / "stat.json"
    path.write_text(json.dumps(result))
    with pytest.raises(AppleProfileError, match="valid Unicode without NUL"):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []


@pytest.mark.parametrize("value", ["before\x00after", "\ud800", "\udfff"])
def test_cli_metadata_preflight_rejects_before_capture(value):
    from topdown.apple_cli import _check_storage_options

    with pytest.raises(AppleProfileError, match="valid Unicode without NUL"):
        _check_storage_options(True, ["test_name=" + value], None)


def check_native_string_storage(backend):
    for value in ["before\x00after", "\ud800", "\udfff", "Δ 😀", r"literal \u0000"]:
        result = {"measurement_kind": "native-test", "native": value,
                  "samples": [{"name": value, "native": 2**64 - 1}, value, None, 2**63 + 1],
                  "metadata_map": {value: "unchanged"}}
        document = {"schema_version": 1, "result": result, "metadata": {"original": value}}
        run = Run(process_name="native-string-test")
        backend.insert_apple_result(run, document)
        assert backend.get_apple_result(run.run_id) == document
        summary = backend.get_apple_result_page(run.run_id, ("samples",))
        assert summary == {"metadata": document["metadata"], "result": {k: v for k, v in result.items() if k != "samples"},
                           "total": None, "rows": None}
        page = backend.get_apple_result_page(run.run_id, ("samples",), "samples", 1, 2)
        assert page["rows"] == result["samples"][1:3] and page["total"] == 4
        assert backend.get_apple_result_page(run.run_id, ("samples",), "samples", 10**50, 2)["rows"] == []
        assert backend.get_apple_result_page(run.run_id, ("samples",), "native", 0, 2)["rows"] is None


def test_sqlite_retains_native_strings_and_pages(sqlite_backend):
    check_native_string_storage(sqlite_backend)


@pytest.mark.parametrize("value", ["\ud800", "\udfff", {"nested": ["value", {"\ud800": 1}]}])
def test_native_artifact_decoder_rejects_unpaired_unicode_before_storage(value, tmp_path, sqlite_backend):
    from topdown.collector.apple_files import decode_json_object

    source = json.dumps({"native": value}).encode()
    with pytest.raises(ValueError, match="Unicode scalar"):
        decode_json_object(source)
    path = tmp_path / "stat.json"
    path.write_bytes(source)
    with pytest.raises(AppleProfileError, match="Unicode scalar"):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []


def test_native_decoder_preserves_valid_surrogate_pairs_and_nul():
    from topdown.collector.apple_files import decode_json_object

    assert decode_json_object(b'{"emoji":"\\ud83d\\ude00","nul":"before\\u0000after"}') == {
        "emoji": "😀", "nul": "before\x00after"}


def test_unicode_checks_do_not_hang_on_circular_direct_python_inputs():
    from topdown.apple_storage import validate_result

    result = {"measurement_kind": "process-accounted-fixed-counters", "tma_available": False,
              "tma": dict.fromkeys(TMA_ROOTS), "scope": {"pid": 42}, "counters": {"instructions": 1, "cycles": 1}}
    result["extra"] = result
    with pytest.raises(AppleProfileError, match="Circular|circular"):
        validate_result(result)


def test_direct_validation_rejects_unpaired_scalars_before_formatting_errors():
    from topdown.apple_storage import validate_result

    with pytest.raises(AppleProfileError, match="Unicode scalar"):
        validate_result({"measurement_kind": "bad\ud800kind"})


@pytest.mark.parametrize("leaf", ["ordinary", "before\x00after"])
def test_string_validation_does_not_reduce_the_native_json_nesting_limit(leaf):
    value = leaf
    for _ in range(400):
        value = [value]
    document = {"result": {"measurement_kind": "native-test", "deep": value}, "metadata": {}}
    decoded = decode_document(json.loads(encode_document(document)))
    recovered = decoded["result"]["deep"]
    for _ in range(400):
        recovered = recovered[0]
    assert recovered == leaf


def test_duplicate_invalid_unicode_keys_produce_a_serializable_error():
    from topdown.collector.apple_files import decode_json_object

    with pytest.raises(ValueError, match="Duplicate") as error:
        decode_json_object(b'{"\\ud800":1,"\\ud800":2}')
    assert r"\ud800" in str(error.value)
    str(error.value).encode("utf-8")


@pytest.mark.parametrize("source", [b'{"value":"\\uD800"}', b'{"value":"\\uDdFF"}', '{"value":"\ud800"}'])
def test_native_unicode_fast_path_handles_escape_case_and_literal_surrogates(source):
    from topdown.collector.apple_files import decode_json_object

    with pytest.raises(ValueError, match="Unicode scalar"):
        decode_json_object(source)


def test_custom_numeric_decoders_cannot_introduce_invalid_unicode():
    from topdown.collector.apple_files import NativeJSONDecoder

    with pytest.raises(ValueError, match="Unicode scalar"):
        json.loads('{"number":1}', cls=NativeJSONDecoder, parse_int=lambda value: "\ud800")


@pytest.mark.parametrize("name", ["native-\ud800.json", "native-\udcff.json"])
def test_invalid_filesystem_filename_metadata_is_rejected_before_reads(tmp_path, sqlite_backend, monkeypatch, name):
    from topdown import apple_storage

    class LegacyPath(type(tmp_path)):
        @property
        def name(self):
            return name

    path = tmp_path / "stat.json"
    path.write_text("{}")

    def read(path):
        raise AssertionError("invalid filename reached artifact reads")

    monkeypatch.setattr(apple_storage, "_read_json", read)
    with pytest.raises(AppleProfileError, match="filename requires valid Unicode"):
        store_apple_result(sqlite_backend, LegacyPath(path))
    assert sqlite_backend.list_runs() == []


def test_valid_unicode_artifact_filename_remains_exact(tmp_path, sqlite_backend):
    result = {"measurement_kind": "process-accounted-fixed-counters", "tma_available": False,
              "tma": dict.fromkeys(TMA_ROOTS), "scope": {"pid": 42},
              "counters": {"instructions": 1, "cycles": 1}}
    path = tmp_path / "native-Δ-😀.json"
    path.write_text(json.dumps(result))
    run = store_apple_result(sqlite_backend, path)
    assert sqlite_backend.get_apple_result(run.run_id)["metadata"]["source_filename"] == path.name


def check_public_legacy_unicode_responses(backend):
    from topdown.apple_storage import query_apple_result, stored_apple_gpu_report, stored_apple_hotspots
    from topdown.collector.apple import FIXED_EVENTS, parse_profile
    from tests.test_apple_gpu_replay import native_profile

    def insert(result, metadata=None):
        document = {"schema_version": 1, "result": result, "metadata": metadata or {}}
        run = Run(process_name="legacy")
        backend.insert_apple_result(run, document)
        return run, document

    run, original = insert({"measurement_kind": "native-test", "samples": [
        {"name": "\ud800"}, {"name": "clean\x00😀", "instructions": 2**64 - 1}]})
    assert "samples" not in query_apple_result(backend, run.run_id)["result"]
    assert query_apple_result(backend, run.run_id, "samples", 1, 1)["rows"] == original["result"]["samples"][1:]
    with pytest.raises(AppleProfileError, match="Stored Apple result contains invalid Unicode") as error:
        query_apple_result(backend, run.run_id, "samples", 0, 1)
    str(error.value).encode("utf-8")
    assert backend.get_apple_result(run.run_id) == original

    run, original = insert({"measurement_kind": "native-test"}, {"source_filename": "native-\udcff.json"})
    with pytest.raises(AppleProfileError, match="Stored Apple result contains invalid Unicode"):
        query_apple_result(backend, run.run_id)
    assert backend.get_apple_result(run.run_id) == original

    gpu = native_profile()
    gpu["encoders"][0]["listing"]["values"][0]["value"] = '"legacy-\ud800-label"'
    run, original = insert(gpu)
    with pytest.raises(AppleProfileError, match="Stored Apple result contains invalid Unicode"):
        stored_apple_gpu_report(backend, run.run_id)
    assert backend.get_apple_result(run.run_id) == original

    cpu = parse_profile(Path(__file__).parent / "test_data/apple_fixed_profile_m6.xml", "counters-profile", 3362,
                        fixed_events=FIXED_EVENTS)
    cpu["measurement_kind"] = "native-fixed-counter-samples"
    cpu["samples"][0]["frames"][0]["name"] = "legacy-\ud800-frame"
    run, original = insert(cpu)
    with pytest.raises(AppleProfileError, match="Stored Apple result contains invalid Unicode"):
        stored_apple_hotspots(backend, run.run_id)
    assert backend.get_apple_result(run.run_id) == original


def test_sqlite_public_responses_reject_selected_legacy_surrogates(sqlite_backend):
    check_public_legacy_unicode_responses(sqlite_backend)
