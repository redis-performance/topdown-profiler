"""Native Mach remainders stay monotonic when per-core nanoseconds quantize."""

from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from topdown.apple_storage import store_apple_result, validate_result
from topdown.collector.apple import AppleProfileError
from topdown.collector.apple_stat import _UsageReader, stat_pid
from tests.test_apple_stat import fake_libraries, native_host  # noqa: F401


@pytest.fixture
def rounded_interval(tmp_path, native_host):  # noqa: F811
    libproc, libsystem = fake_libraries()
    original = libproc.proc_pid_rusage.side_effect
    calls = [0]
    def read(pid, flavor, pointer):
        status = original(pid, flavor, pointer)
        info = pointer._obj
        step = calls[0]
        info.ri_proc_start_abstime = 10
        info.ri_instructions, info.ri_cycles = 100 + step * 10, 200 + step * 20
        info.ri_pinstructions, info.ri_pcycles = info.ri_instructions, info.ri_cycles
        info.ri_user_time, info.ri_user_ptime = 3 + step, 2 + step
        info.ri_system_time, info.ri_system_ptime = 6 + step, 4 + step
        calls[0] += 1
        return status
    libproc.proc_pid_rusage.side_effect = read
    with patch("topdown.collector.apple_stat.ctypes.CDLL", side_effect=[libproc, libsystem]):
        reader = _UsageReader()
    with patch("topdown.collector.apple_stat._UsageReader", return_value=reader), \
         patch("topdown.collector.apple_stat.time.sleep"):
        return stat_pid(tmp_path / "interval", 42, .01)


def test_valid_interval_keeps_constant_native_other_times_and_rounding(rounded_interval, sqlite_backend, tmp_path):
    result = rounded_interval
    assert result["status"] == "completed", result
    before, after, delta = result["snapshot_before"], result["snapshot_after"], result["counters"]
    assert before["other_user_time_mach"] == after["other_user_time_mach"] == 1
    assert before["other_user_time_ns"] == after["other_user_time_ns"] == 41
    assert delta["user_time_ns"] == 41 and delta["performance_user_time_ns"] == 42
    assert delta["other_user_time_ns"] == delta["other_user_time_mach"] == 0
    assert delta["system_time_ns"] == 41 and delta["performance_system_time_ns"] == 42
    assert delta["other_system_time_ns"] == 0
    assert delta["user_time_mach"] == delta["performance_user_time_mach"] == 1
    validate_result(result)
    path = tmp_path / "stat.json"
    path.write_text(json.dumps(result))
    run = store_apple_result(sqlite_backend, path)
    assert sqlite_backend.get_apple_result(run.run_id)["result"] == result


@pytest.mark.parametrize("mutation", [
    lambda r: r["source"].pop("mach_timebase"),
    lambda r: r["source"]["mach_timebase"].update(numer=0),
    lambda r: r["source"]["mach_timebase"].update(denom=True),
    lambda r: r["source"]["mach_timebase"].update(numer=2**32),
    lambda r: r["counters"].pop("other_user_time_mach"),
    lambda r: r["counters"].pop("other_user_time_ns"),
    lambda r: (r["counters"].pop("performance_user_time_mach"), r["counters"].pop("other_user_time_mach")),
    lambda r: r["counters"].update(other_user_time_mach=1),
    lambda r: r["counters"].update(user_time_ns=40),
    lambda r: r["counters"].update(user_time_ns=43),
    lambda r: r["counters"].update(user_time_ns=42),
    lambda r: r["snapshot_before"].update(other_system_time_ns=84),
    lambda r: r["snapshot_after"].update(performance_user_time_mach=4),
    lambda r: r["counters"].update(other_instructions=1),
])
def test_quantization_requires_complete_exact_native_evidence(rounded_interval, mutation):
    result = deepcopy(rounded_interval)
    mutation(result)
    with pytest.raises(AppleProfileError):
        validate_result(result)


def test_v4_raw_times_and_integer_timebases_retain_their_exact_conversion(rounded_interval):
    result = deepcopy(rounded_interval)
    result["source"].update(flavor="RUSAGE_INFO_V4", flavor_id=4, mach_timebase={"numer": 1, "denom": 1})
    for counters in [result["counters"], result["snapshot_before"], result["snapshot_after"]]:
        for key in list(counters):
            if key.startswith(("performance_", "other_")):
                del counters[key]
        for stem in ("user_time", "system_time"):
            counters[stem + "_ns"] = counters[stem + "_mach"]
    validate_result(result)
    result["counters"]["user_time_ns"] += 1
    with pytest.raises(AppleProfileError, match="rounding"):
        validate_result(result)


@pytest.mark.parametrize("field", ["numer", "denom"])
def test_reader_rejects_zero_timebase_components(field):
    libproc, libsystem = fake_libraries()
    original = libsystem.mach_timebase_info.side_effect
    def timebase(pointer):
        original(pointer)
        setattr(pointer._obj, field, 0)
        return 0
    libsystem.mach_timebase_info.side_effect = timebase
    with patch("topdown.collector.apple_stat.ctypes.CDLL", side_effect=[libproc, libsystem]):
        with pytest.raises(AppleProfileError, match="Mach timebase"):
            _UsageReader()
