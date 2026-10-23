"""Native host provenance is captured without subprocesses and retained on import."""

import ctypes
import errno
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from topdown.collector import apple_environment
from topdown.collector.apple import TMA_ROOTS
from topdown.apple_storage import store_apple_result


@pytest.fixture
def native_host(monkeypatch):
    monkeypatch.setattr(apple_environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(apple_environment.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(apple_environment.platform, "mac_ver", lambda: ("27.0.1", (), ""))


@pytest.fixture
def native_metadata(native_host, monkeypatch):
    values = {b"machdep.cpu.brand_string": b"Apple M6\0", b"kern.osversion": b"26A434\0",
              b"kern.uuid": b"NATIVE-KERNEL-UUID\0"}
    def query(name, buffer, length, new, new_length):
        assert new is None and new_length == 0
        value = values[name]
        if isinstance(value, Exception):
            raise value
        assert len(value) <= length._obj.value
        ctypes.memmove(buffer, value, len(value))
        length._obj.value = len(value)
        return 0
    read = Mock(side_effect=query)
    monkeypatch.setattr(apple_environment.ctypes, "CDLL", Mock(return_value=SimpleNamespace(sysctlbyname=read)))
    return values, read


def test_capture_reads_native_model_build_and_kernel_without_a_child(native_metadata, monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("metadata launched a child")))
    assert apple_environment.capture_environment() == {
        "system": "Darwin", "arch": "arm64", "os_version": "27.0.1", "cpu": "Apple M6",
        "os_build": "26A434", "kernel_uuid": "NATIVE-KERNEL-UUID",
    }
    assert native_metadata[1].call_count == 3
    assert native_metadata[1].restype is ctypes.c_int


@pytest.mark.parametrize("failure", [OSError("native metadata unavailable"), AttributeError("sysctl missing")])
def test_binding_failure_preserves_basic_environment(native_host, monkeypatch, failure):
    monkeypatch.setattr(apple_environment.ctypes, "CDLL", Mock(side_effect=failure))
    result = apple_environment.capture_environment()
    assert result["arch"] == "arm64" and result["os_version"] == "27.0.1"
    assert result["metadata_errors"]["sysctl"] and "cpu" not in result


@pytest.mark.parametrize("value", [OSError("kernel metadata unavailable"), b"", b"\0", b"unterminated",
                                   b"bad\0tail\0", b"\xff\0"])
def test_bad_native_field_does_not_hide_other_measured_fields(native_metadata, value):
    native_metadata[0][b"kern.uuid"] = value
    result = apple_environment.capture_environment()
    assert result["cpu"] == "Apple M6" and result["os_build"] == "26A434"
    assert result["metadata_errors"]["kernel_uuid"] and "kernel_uuid" not in result


@pytest.mark.parametrize("length", [0, 4097])
def test_native_metadata_length_is_bounded(length):
    def query(name, buffer, size, new, new_length):
        size._obj.value = length
        return 0
    with pytest.raises(ValueError, match="length"):
        apple_environment._sysctl_text(query, b"kern.uuid")


def test_native_errno_is_retained():
    def query(*args):
        ctypes.set_errno(errno.ENOENT)
        return -1
    with pytest.raises(OSError) as error:
        apple_environment._sysctl_text(query, b"kern.uuid")
    assert error.value.errno == errno.ENOENT


def test_offline_environment_never_loads_native_libraries(monkeypatch):
    monkeypatch.setattr(apple_environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(apple_environment.ctypes, "CDLL", Mock(side_effect=AssertionError("native library used offline")))
    assert apple_environment.capture_environment()["system"] == "Linux"


def check_import_environment(backend, tmp_path, monkeypatch):
    path = tmp_path / "stat.json"
    source = {"measurement_kind": "process-accounted-fixed-counters", "scope": {"pid": 42},
              "counters": {"instructions": 100, "cycles": 200}, "tma_available": False,
              "tma": dict.fromkeys(TMA_ROOTS), "environment": {"cpu": "Apple M1", "arch": "arm64",
              "os_version": "26.0", "os_build": "SOURCE-BUILD", "kernel_uuid": "SOURCE-KERNEL"}}
    path.write_text(json.dumps(source))
    monkeypatch.setattr(apple_environment, "capture_environment", Mock(side_effect=AssertionError("queried import host")))
    run = store_apple_result(backend, path, labels={"cpu": "Apple M6", "arch": "x86_64",
        "os_version": "fake", "os_build": "fake", "kernel_uuid": "fake", "test_name": "cross-host"})
    assert all(run.labels[key] == value for key, value in source["environment"].items())
    assert run.labels["test_name"] == "cross-host"
    assert backend.get_apple_result(run.run_id)["result"] == source
    del source["environment"]
    path.write_text(json.dumps(source))
    legacy = store_apple_result(backend, path, labels={"cpu": "user-provided-for-legacy"})
    assert legacy.labels["cpu"] == "user-provided-for-legacy"


def test_capture_metadata_is_authoritative_and_import_host_is_never_substituted(sqlite_backend, tmp_path, monkeypatch):
    check_import_environment(sqlite_backend, tmp_path, monkeypatch)


def test_stat_report_retains_native_host_provenance(native_metadata):
    from topdown.collector.apple_stat import _report

    reader = SimpleNamespace(flavor_id=6, timebase=SimpleNamespace(numer=125, denom=3))
    report = _report(reader)
    assert report["environment"]["cpu"] == "Apple M6"
    assert report["environment"]["kernel_uuid"] == "NATIVE-KERNEL-UUID"


@pytest.mark.parametrize("missing_kernel", [False, True])
def test_soc_capture_retains_provenance_and_survives_partial_metadata_failure(
        native_metadata, tmp_path, monkeypatch, missing_kernel):
    from tests.test_apple_soc import payload
    from topdown.collector import apple_soc

    if missing_kernel:
        native_metadata[0][b"kern.uuid"] = OSError("kernel UUID unavailable")
    def run(command, output, name, timeout):
        (output / f"{name}.stdout").write_text(json.dumps(payload()))
    monkeypatch.setattr(apple_soc, "_run", run)
    report = apple_soc.sample_soc(tmp_path / "capture", .01)
    assert report["status"] == "completed" and report["exit_code"] == 0
    assert report["environment"]["cpu"] == "Apple M6"
    assert ("metadata_errors" in report["environment"]) is missing_kernel
