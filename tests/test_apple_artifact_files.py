"""Native artifacts remain regular files even when an import path changes."""

import errno
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from topdown.apple_report import load_hotspots
from topdown.apple_storage import store_apple_result
from topdown.collector.apple import AppleProfileError, TMA_ROOTS
from topdown.collector.apple_files import read_regular_bytes
from tests.test_apple import xml_table
from topdown.collector.apple import parse_profile


@pytest.fixture
def native_json(tmp_path):
    path = tmp_path / "stat.json"
    value = {"measurement_kind": "process-accounted-fixed-counters", "scope": {"pid": 42},
             "counters": {"instructions": 2**64 - 1, "cycles": 2**53 + 1},
             "tma_available": False, "tma": dict.fromkeys(TMA_ROOTS)}
    path.write_text(json.dumps(value))
    return path, value


def test_regular_artifacts_and_symlinks_keep_exact_bytes(sqlite_backend, tmp_path, native_json):
    path, original = native_json
    link = tmp_path / "latest.json"
    link.symlink_to(path)
    assert read_regular_bytes(link) == path.read_bytes()
    run = store_apple_result(sqlite_backend, link)
    assert sqlite_backend.get_apple_result(run.run_id)["result"] == original


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_import_and_report_reject_named_pipes_without_waiting(tmp_path):
    fifo = tmp_path / "profile.json"
    os.mkfifo(fifo)
    code = '''import signal,sys
from pathlib import Path
from topdown.apple_storage import _read_json
from topdown.apple_report import load_hotspots
from topdown.collector.apple import AppleProfileError
def timeout(*args): raise AssertionError("blocked reading a named pipe")
signal.signal(signal.SIGALRM,timeout)
for reader in [_read_json,load_hotspots]:
    signal.alarm(2)
    try:
        reader(Path(sys.argv[1]))
    except AppleProfileError as exc:
        assert 'regular Apple JSON artifact' in str(exc),str(exc)
    else: raise AssertionError("accepted a named pipe")
    finally: signal.alarm(0)
'''
    result = subprocess.run([sys.executable, "-c", code, str(fifo)], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_replaced_file_is_checked_after_open_and_its_descriptor_is_closed(tmp_path, monkeypatch):
    path = tmp_path / "changing.json"
    path.write_text('{}')
    original_open = os.open
    descriptors = []
    def replace_then_open(target, flags):
        path.unlink()
        os.mkfifo(path)
        descriptor = original_open(target, flags)
        descriptors.append(descriptor)
        return descriptor
    monkeypatch.setattr(os, "open", replace_then_open)
    def deadline(*args):
        raise AssertionError("blocked opening a replaced named pipe")
    previous = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(2)
    try:
        with pytest.raises(OSError, match="regular Apple JSON artifact"):
            read_regular_bytes(path)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    with pytest.raises(OSError) as error:
        os.fstat(descriptors[0])
    assert error.value.errno == errno.EBADF


def test_descriptors_close_when_a_regular_read_fails(native_json):
    path, _ = native_json
    original_open = os.open
    descriptors = []
    def capture_open(*args):
        descriptor = original_open(*args)
        descriptors.append(descriptor)
        return descriptor
    with patch("os.open", side_effect=capture_open), patch("os.fdopen", side_effect=OSError("read unavailable")):
        with pytest.raises(OSError, match="read unavailable"):
            read_regular_bytes(path)
    with pytest.raises(OSError) as error:
        os.fstat(descriptors[0])
    assert error.value.errno == errno.EBADF


def test_devices_and_missing_files_fail_without_an_import(sqlite_backend, tmp_path):
    with pytest.raises(AppleProfileError):
        store_apple_result(sqlite_backend, Path(os.devnull))
    with pytest.raises(FileNotFoundError):
        read_regular_bytes(tmp_path / "missing.json")
    assert sqlite_backend.list_runs() == []


def test_hotspot_reports_continue_to_accept_normal_native_json(tmp_path):
    xml = tmp_path / "profile.xml"
    xml.write_text(xml_table())
    profile = parse_profile(xml, "time-profile", 42)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    assert load_hotspots(path)["sample_count"] == 2


@pytest.mark.parametrize('payload,reason', [
    (b'{"scope":{},"scope":{}}', 'Duplicate'),
    (b'{"nested":{"value":0,"value":1}}', 'Duplicate'),
    (b'{"metadata":NaN}', 'Non-finite'), (b'{"metadata":Infinity}', 'Non-finite'),
    (b'{"metadata":1e999}', 'Non-finite'), (b'[]', 'JSON object'),
    (b'{"nested":'+b'['*20000+b'0'+b']'*20000+b'}', 'nesting'),
], ids=['duplicate-root','duplicate-nested','nan','infinity','overflowing-float','nonobject','nesting'])
def test_import_and_local_reports_share_strict_native_json(tmp_path,sqlite_backend,payload,reason):
    from topdown.apple_storage import _read_json
    from topdown.collector.apple_files import decode_json_object

    path=tmp_path/'profile.json'
    path.write_bytes(payload)
    with pytest.raises(ValueError,match=reason):
        decode_json_object(payload)
    for reader in [_read_json,load_hotspots]:
        with pytest.raises(AppleProfileError,match=reason):
            reader(path)
    with pytest.raises(AppleProfileError,match=reason):
        store_apple_result(sqlite_backend,path)
    assert sqlite_backend.list_runs()==[]


def test_strict_native_json_preserves_large_integers_unicode_and_decimals():
    from topdown.collector.apple_files import decode_json_object

    payload='{"counter":18446744073709551615,"name":"µs","ratio":0.25}'
    assert decode_json_object(payload.encode())=={'counter':2**64-1,'name':'µs','ratio':.25}


def test_native_json_decoder_preserves_stream_offsets_and_integer_precision():
    from topdown.collector.apple_files import NativeJSONDecoder

    source = '{"value":0.25} {"counter":18446744073709551615}'
    decoder = NativeJSONDecoder()
    first, end = decoder.raw_decode(source)
    second, final = decoder.raw_decode(source, end + 1)
    assert first == {"value":.25} and second == {"counter":2**64 - 1}
    assert final == len(source)


@pytest.mark.parametrize("source", ['{"Cost":"0%","Cost":"100%"}', '{"value":NaN}', '{"value":1e999}'])
def test_native_json_stream_decoder_rejects_ambiguous_values(source):
    from topdown.collector.apple_files import NativeJSONDecoder

    with pytest.raises(ValueError):
        NativeJSONDecoder().raw_decode(source)
