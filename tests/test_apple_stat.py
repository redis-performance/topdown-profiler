"""Final child counter accounting and bounded process lifecycle."""

import ctypes
import errno
import json
import os
import platform
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from topdown.cli import app
from topdown.collector.apple import AppleProfileError
from topdown.collector.apple_stat import _peek_exit, _terminate, _UsageReader, _wait_for_exit, stat_pid, stat_process


@pytest.fixture
def native_host():
    with patch("topdown.collector.apple_stat.platform.system", return_value="Darwin"), \
         patch("topdown.collector.apple_stat.platform.machine", return_value="arm64"):
        yield


@pytest.fixture
def capture(native_host):
    reader = MagicMock(timebase=SimpleNamespace(numer=125, denom=3))
    reader.read.return_value = {"instructions": 9007199254740993, "cycles": 123,
                              "user_time_ns": 1000, "system_time_ns": 500,
                              "process_start_mach": 10, "process_exit_mach": 100}
    process = MagicMock(pid=42)
    process.wait.return_value = 0
    with patch("topdown.collector.apple_stat._UsageReader", return_value=reader), \
         patch("topdown.collector.apple_stat.subprocess.Popen", return_value=process) as launch, \
         patch("topdown.collector.apple_stat._wait_for_exit", return_value=object()) as wait:
        yield SimpleNamespace(reader=reader, process=process, launch=launch, wait=wait)


def test_final_counters_are_read_before_reaping_and_preserve_uint64(tmp_path, capture):
    order = []
    counters = capture.reader.read.return_value
    capture.reader.read.side_effect = lambda pid: (order.append(("read", pid)), counters)[1]
    capture.process.wait.side_effect = lambda **kwargs: (order.append(("reap", 42)), 0)[1]
    output = tmp_path / "stat"
    result = stat_process(output, [sys.executable])
    assert order == [("read", 42), ("reap", 42)]
    assert result["status"] == "completed"
    assert result["target_completed"] is True
    assert result["counters_available"] is True
    assert result["counters"]["instructions"] == 9007199254740993
    assert result["scope"]["descendants"] is False
    assert result["tma_available"] is False
    assert set(result["tma"].values()) == {None}
    assert json.loads((output / "stat.json").read_text()) == result
    assert capture.launch.call_args.kwargs["start_new_session"] is True
    capture.process.poll.assert_not_called()


@pytest.mark.parametrize("target_exit,expected", [(7, 7), (-signal.SIGTERM, 143)])
def test_target_failure_preserves_counters_and_propagates_exit(tmp_path, capture, target_exit, expected):
    capture.process.wait.return_value = target_exit
    result = stat_process(tmp_path / "stat", [sys.executable])
    assert result["exit_code"] == expected
    assert result["target_exit_code"] == target_exit
    assert result["target_completed"] is False
    assert result["counters_available"] is True


@pytest.mark.parametrize("field", ["instructions", "cycles"])
def test_zero_counters_do_not_establish_counter_access(tmp_path, capture, field):
    capture.reader.read.return_value[field] = 0
    result = stat_process(tmp_path / "stat", [sys.executable])
    assert result["status"] == "counters_unavailable"
    assert result["exit_code"] == 3
    assert result["counters_available"] is False
    assert result["tma_available"] is False


@pytest.mark.parametrize("interrupt,status,exit_code", [(False, "timeout", 5), (True, "interrupted", 130)])
def test_timeout_and_interrupt_retain_partial_counters(tmp_path, capture, interrupt, status, exit_code):
    if interrupt:
        capture.wait.side_effect = KeyboardInterrupt
    else:
        capture.wait.return_value = None
    capture.process.wait.return_value = -signal.SIGTERM
    with patch("topdown.collector.apple_stat._terminate") as terminate:
        result = stat_process(tmp_path / "stat", [sys.executable], timeout=0.01)
    terminate.assert_called_once_with(capture.process)
    assert result["status"] == status
    assert result["exit_code"] == exit_code
    assert result["target_completed"] is False
    assert result["counters"]["instructions"] > 0


def test_counter_read_failure_still_reaps_target_and_saves_error(tmp_path, capture):
    capture.reader.read.side_effect = AppleProfileError("read denied")
    with patch("topdown.collector.apple_stat._terminate"):
        result = stat_process(tmp_path / "stat", [sys.executable])
    assert result["status"] == "failed"
    assert result["error"] == "read denied"
    capture.process.wait.assert_called_once()
    assert (tmp_path / "stat/stat.json").exists()


def test_launch_error_is_retained_without_counter_claim(tmp_path, capture):
    capture.launch.side_effect = PermissionError("launch denied")
    result = stat_process(tmp_path / "stat", [sys.executable])
    assert result["status"] == "failed"
    assert result["counters"] is None
    assert "launch denied" in result["error"]
    capture.reader.read.assert_not_called()


def test_cleanup_failure_is_visible_in_saved_result(tmp_path, capture):
    capture.reader.read.side_effect = AppleProfileError("read failed")
    with patch("topdown.collector.apple_stat._terminate", side_effect=AppleProfileError("exit unverified")):
        result = stat_process(tmp_path / "stat", [sys.executable])
    assert result["cleanup_error"] == "exit unverified"
    assert result["target_completed"] is False


def test_wait_failure_is_structured_and_cleanup_is_attempted(tmp_path, capture):
    capture.process.wait.side_effect = subprocess.TimeoutExpired("target", 2)
    with patch("topdown.collector.apple_stat._terminate") as terminate:
        result = stat_process(tmp_path / "stat", [sys.executable])
    terminate.assert_called_once()
    assert result["status"] == "failed"
    assert "cleanup_error" in result


def test_existing_output_is_never_overwritten(tmp_path, capture):
    sentinel = tmp_path / "stat.json"
    sentinel.write_text("existing evidence")
    with pytest.raises(FileExistsError):
        stat_process(tmp_path, [sys.executable])
    assert sentinel.read_text() == "existing evidence"
    capture.launch.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_never_launches(tmp_path, capture, timeout):
    with pytest.raises(AppleProfileError, match="finite timeout"):
        stat_process(tmp_path / "stat", [sys.executable], timeout)
    capture.launch.assert_not_called()
    assert not (tmp_path / "stat").exists()


@pytest.mark.parametrize("command", [[], ["/nonexistent/owned-target"]])
def test_invalid_command_never_launches(tmp_path, capture, command):
    with pytest.raises(AppleProfileError, match="executable path"):
        stat_process(tmp_path / "stat", command)
    capture.launch.assert_not_called()


def test_nonexecutable_target_never_launches(tmp_path, capture):
    path = tmp_path / "source"
    path.write_text("not executable")
    with pytest.raises(AppleProfileError, match="executable path"):
        stat_process(tmp_path / "stat", [str(path)])
    capture.launch.assert_not_called()


@pytest.mark.parametrize("system,arch", [("Linux", "arm64"), ("Darwin", "x86_64")])
def test_unsupported_host_does_not_create_artifacts(tmp_path, system, arch):
    with patch("topdown.collector.apple_stat.platform.system", return_value=system), \
         patch("topdown.collector.apple_stat.platform.machine", return_value=arch), \
         patch("topdown.collector.apple_stat._UsageReader") as reader:
        with pytest.raises(AppleProfileError, match="Apple silicon"):
            stat_process(tmp_path / "stat", [sys.executable])
    reader.assert_not_called()


def test_waitid_keeps_zombie_and_uses_bounded_polling():
    exited = object()
    with patch("topdown.collector.apple_stat._peek_exit", side_effect=[None, exited]) as peek, \
         patch("topdown.collector.apple_stat.time.monotonic", return_value=1), \
         patch("topdown.collector.apple_stat.time.sleep") as sleep:
        assert _wait_for_exit(42, 2) is exited
    assert peek.call_args.args == (42,)
    sleep.assert_called_once_with(0.01)


def test_waitid_deadline_does_not_sleep_or_reap():
    with patch("topdown.collector.apple_stat._peek_exit", return_value=None), \
         patch("topdown.collector.apple_stat.time.monotonic", return_value=2), \
         patch("topdown.collector.apple_stat.time.sleep") as sleep:
        assert _wait_for_exit(42, 1) is None
    sleep.assert_not_called()


def test_builtin_waitid_retains_the_child_for_the_final_read():
    exited = object()
    native_os = SimpleNamespace(waitid=MagicMock(return_value=exited), P_PID=1,
                                WEXITED=4, WNOWAIT=32, WNOHANG=1)
    with patch("topdown.collector.apple_stat.os", native_os):
        assert _peek_exit(42) is exited
    native_os.waitid.assert_called_once_with(1, 42, 0x25)


@pytest.mark.parametrize("exited", [False, True])
def test_darwin_libc_waitid_for_python_before_313(exited):
    libsystem = MagicMock()

    def waitid(kind, pid, pointer, flags):
        assert (kind, pid, flags) == (1, 42, 0x25)
        pointer._obj.si_pid = pid if exited else 0
        return 0

    libsystem.waitid.side_effect = waitid
    with patch("topdown.collector.apple_stat.ctypes.CDLL", return_value=libsystem), \
         patch("topdown.collector.apple_stat.os") as mock_os:
        del mock_os.waitid
        result = _peek_exit(42)
    assert (result is not None) is exited
    if exited:
        assert result.si_pid == 42


@pytest.mark.parametrize("error,exception", [(errno.ECHILD, ChildProcessError), (errno.EPERM, OSError)])
def test_darwin_libc_waitid_preserves_ownership_and_other_errors(error, exception):
    libsystem = MagicMock()

    def waitid(*args):
        ctypes.set_errno(error)
        return -1

    libsystem.waitid.side_effect = waitid
    with patch("topdown.collector.apple_stat.ctypes.CDLL", return_value=libsystem), \
         patch("topdown.collector.apple_stat.os") as mock_os:
        del mock_os.waitid
        with pytest.raises(exception):
            _peek_exit(42)


def test_termination_escalates_only_for_owned_process_group():
    with patch("topdown.collector.apple_stat.os.killpg") as kill, \
         patch("topdown.collector.apple_stat._wait_for_exit", side_effect=[None, None, object()]):
        _terminate(SimpleNamespace(pid=42))
    assert [call.args for call in kill.call_args_list] == [(42, signal.SIGTERM), (42, signal.SIGKILL)]


def test_already_exited_target_does_not_make_cleanup_fail():
    with patch("topdown.collector.apple_stat.os.killpg") as kill, \
         patch("topdown.collector.apple_stat._wait_for_exit", return_value=object()):
        _terminate(SimpleNamespace(pid=42))
    kill.assert_not_called()


def test_lost_child_ownership_never_signals_a_potentially_reused_pid():
    with patch("topdown.collector.apple_stat.os.killpg") as kill, \
         patch("topdown.collector.apple_stat._wait_for_exit", side_effect=ChildProcessError):
        with pytest.raises(AppleProfileError, match="ownership was lost"):
            _terminate(SimpleNamespace(pid=42))
    kill.assert_not_called()


def test_target_exiting_during_termination_is_not_an_error():
    with patch("topdown.collector.apple_stat.os.killpg", side_effect=ProcessLookupError), \
         patch("topdown.collector.apple_stat._wait_for_exit", side_effect=[None, object()]):
        _terminate(SimpleNamespace(pid=42))


def test_unverified_sigkill_is_reported():
    with patch("topdown.collector.apple_stat.os.killpg", side_effect=ProcessLookupError), \
         patch("topdown.collector.apple_stat._wait_for_exit", return_value=None):
        with pytest.raises(AppleProfileError, match="unverified after SIGKILL"):
            _terminate(SimpleNamespace(pid=42))


def fake_libraries(error=False, timebase_error=False, v6_unsupported=False, invalid_p=False):
    libproc, libsystem = MagicMock(), MagicMock()

    def timebase(pointer):
        pointer._obj.numer, pointer._obj.denom = 125, 3
        return 1 if timebase_error else 0

    def read(pid, flavor, pointer):
        if error:
            ctypes.set_errno(1)
            return -1
        if v6_unsupported and flavor == 6:
            ctypes.set_errno(errno.EINVAL)
            return -1
        info = pointer._obj
        info.ri_instructions, info.ri_cycles = 9007199254740993, 9
        info.ri_user_time, info.ri_system_time = 3, 6
        if flavor == 6:
            info.ri_pinstructions = info.ri_instructions + 1 if invalid_p else 9007199254740000
            info.ri_pcycles = 7
            info.ri_user_ptime, info.ri_system_ptime = 2, 4
        return 0

    libproc.proc_pid_rusage.side_effect = read
    libsystem.mach_timebase_info.side_effect = timebase
    return [libproc, libsystem]


def test_ffi_retains_integer_precision_and_converts_mach_time():
    with patch("topdown.collector.apple_stat.ctypes.CDLL", side_effect=fake_libraries()):
        reader = _UsageReader()
        result = reader.read(42)
    assert reader.flavor_id == 6
    assert result["instructions"] == 9007199254740993
    assert result["user_time_ns"] == 125
    assert result["system_time_ns"] == 250
    assert result["performance_instructions"] == 9007199254740000
    assert result["other_instructions"] == 993
    assert result["performance_cycles"] == 7
    assert result["other_cycles"] == 2
    assert result["performance_user_time_ns"] == 83
    assert result["other_user_time_ns"] == 41
    assert result["performance_system_time_ns"] == 166
    assert result["other_system_time_ns"] == 83
    assert result["user_time_mach"] == 3 and result["performance_user_time_mach"] == 2
    assert result["other_user_time_mach"] == 1 and result["other_system_time_mach"] == 2


def test_ffi_falls_back_to_v4_when_v6_is_unavailable():
    libproc, libsystem = fake_libraries(v6_unsupported=True)
    with patch("topdown.collector.apple_stat.ctypes.CDLL", side_effect=[libproc, libsystem]):
        reader = _UsageReader()
        result = reader.read(42)
    assert reader.flavor_id == 4
    assert "performance_instructions" not in result
    assert [call.args[1] for call in libproc.proc_pid_rusage.call_args_list] == [6, 4]


def test_ffi_rejects_performance_counts_above_aggregate():
    with patch("topdown.collector.apple_stat.ctypes.CDLL", side_effect=fake_libraries(invalid_p=True)):
        with pytest.raises(AppleProfileError, match="exceeds aggregate"):
            _UsageReader().read(42)


def test_ffi_rejects_performance_time_above_aggregate():
    libproc, libsystem = fake_libraries()
    original = libproc.proc_pid_rusage.side_effect

    def read(pid, flavor, pointer):
        status = original(pid, flavor, pointer)
        if flavor == 6:
            pointer._obj.ri_user_ptime = 4
        return status

    libproc.proc_pid_rusage.side_effect = read
    with patch("topdown.collector.apple_stat.ctypes.CDLL", side_effect=[libproc, libsystem]):
        with pytest.raises(AppleProfileError, match="exceeds aggregate"):
            _UsageReader().read(42)


def test_ffi_error_does_not_return_zero_measurements():
    with patch("topdown.collector.apple_stat.ctypes.CDLL", side_effect=fake_libraries(error=True)):
        with pytest.raises(AppleProfileError, match="proc_pid_rusage.*failed"):
            _UsageReader().read(42)


def test_invalid_timebase_is_rejected():
    with patch("topdown.collector.apple_stat.ctypes.CDLL", side_effect=fake_libraries(timebase_error=True)):
        with pytest.raises(AppleProfileError, match="Mach timebase"):
            _UsageReader()


def test_stat_cli_emits_only_json_and_propagates_target_status(tmp_path):
    report = {"status": "completed", "exit_code": 7, "counters": {"cycles": 123}}
    with patch("topdown.collector.apple_stat.stat_process", return_value=report) as stat:
        result = CliRunner().invoke(app, ["apple", "stat", "--output", str(tmp_path / "stat"),
                                          "--timeout", "2", "--", "/owned/target", "--some-flag"])
    assert result.exit_code == 7
    assert json.loads(result.output) == report
    assert stat.call_args.args[1:] == (["/owned/target", "--some-flag"], 2)


def test_stat_cli_reports_preflight_error(tmp_path):
    with patch("topdown.collector.apple_stat.stat_process", side_effect=AppleProfileError("unsupported")):
        result = CliRunner().invoke(app, ["apple", "stat", "--output", str(tmp_path / "stat")])
    assert result.exit_code == 1
    assert "unsupported" in result.output


def interval_snapshots(capture):
    before = dict(capture.reader.read.return_value, instructions=100, cycles=200,
                  user_time_ns=1000, system_time_ns=1000, process_exit_mach=0)
    after = dict(before, instructions=300, cycles=500, user_time_ns=2000, system_time_ns=1500)
    capture.reader.read.side_effect = [before, after]
    return before, after


def test_existing_pid_interval_uses_deltas_without_launching_or_signaling(tmp_path, capture):
    interval_snapshots(capture)
    with patch("topdown.collector.apple_stat.time.sleep") as sleep, \
         patch("topdown.collector.apple_stat.time.monotonic_ns", side_effect=[1000, 2000]), \
         patch("topdown.collector.apple_stat.os.killpg") as kill:
        result = stat_pid(tmp_path / "stat", 42, duration=0.5)
    assert result["status"] == "completed"
    assert result["counters"] == {"instructions": 200, "cycles": 300, "user_time_ns": 1000, "system_time_ns": 500}
    assert result["observed_duration_ns"] == 1000
    assert result["target_exited"] is False
    assert result["scope"]["window"] == "observed-interval"
    sleep.assert_called_once_with(0.5)
    kill.assert_not_called()
    capture.launch.assert_not_called()
    capture.process.wait.assert_not_called()


def test_existing_pid_interval_preserves_core_kind_deltas(tmp_path, capture):
    before, after = interval_snapshots(capture)
    before.update(performance_instructions=80, performance_cycles=150, other_instructions=20, other_cycles=50,
                  performance_user_time_ns=700, performance_system_time_ns=600,
                  other_user_time_ns=300, other_system_time_ns=400)
    after.update(performance_instructions=260, performance_cycles=430, other_instructions=40, other_cycles=70,
                 performance_user_time_ns=1600, performance_system_time_ns=900,
                 other_user_time_ns=400, other_system_time_ns=600)
    with patch("topdown.collector.apple_stat.time.sleep"):
        result = stat_pid(tmp_path / "stat", 42)
    assert result["counters"]["performance_instructions"] == 180
    assert result["counters"]["other_instructions"] == 20
    assert result["counters"]["performance_cycles"] == 280
    assert result["counters"]["other_cycles"] == 20
    assert result["counters"]["instructions"] == 200
    assert result["counters"]["cycles"] == 300
    assert result["counters"]["performance_user_time_ns"] == 900
    assert result["counters"]["other_user_time_ns"] == 100
    assert result["counters"]["performance_system_time_ns"] == 300
    assert result["counters"]["other_system_time_ns"] == 200


def test_existing_pid_rejects_counter_flavor_change(tmp_path, capture):
    before, after = interval_snapshots(capture)
    before["performance_instructions"] = 80
    with patch("topdown.collector.apple_stat.time.sleep"):
        result = stat_pid(tmp_path / "stat", 42)
    assert result["status"] == "failed"
    assert "Counter flavor changed" in result["error"]
    assert result["counters"] is None


@pytest.mark.parametrize("change", ["reused", "unknown", "decreased"])
def test_existing_pid_rejects_identity_or_counter_changes(tmp_path, capture, change):
    before, after = interval_snapshots(capture)
    if change == "reused":
        after["process_start_mach"] += 1
    elif change == "unknown":
        before["process_start_mach"] = after["process_start_mach"] = 0
    else:
        after["instructions"] = before["instructions"] - 1
    with patch("topdown.collector.apple_stat.time.sleep"):
        result = stat_pid(tmp_path / "stat", 42)
    assert result["status"] == "failed"
    assert result["counters"] is None
    assert result["snapshot_after"] == after


def test_existing_pid_exit_with_unavailable_final_snapshot_fails_closed(tmp_path, capture):
    before, _ = interval_snapshots(capture)
    capture.reader.read.side_effect = [before, AppleProfileError("No such process")]
    with patch("topdown.collector.apple_stat.time.sleep"):
        result = stat_pid(tmp_path / "stat", 42)
    assert result["status"] == "failed"
    assert result["counters"] is None
    assert result["snapshot_before"] == before
    assert "No such process" in result["error"]


def test_existing_pid_interrupt_keeps_observed_interval_and_leaves_target_alone(tmp_path, capture):
    interval_snapshots(capture)
    with patch("topdown.collector.apple_stat.time.sleep", side_effect=KeyboardInterrupt), \
         patch("topdown.collector.apple_stat.os.killpg") as kill:
        result = stat_pid(tmp_path / "stat", 42)
    assert result["status"] == "interrupted"
    assert result["exit_code"] == 130
    assert result["counters"]["instructions"] == 200
    kill.assert_not_called()


def test_existing_pid_idle_interval_keeps_valid_zero_deltas(tmp_path, capture):
    before, after = interval_snapshots(capture)
    after.update(before)
    with patch("topdown.collector.apple_stat.time.sleep"):
        result = stat_pid(tmp_path / "stat", 42)
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert result["counters_available"] is True
    assert result["counters"]["instructions"] == result["counters"]["cycles"] == 0


def test_existing_pid_without_any_native_counts_does_not_establish_access(tmp_path, capture):
    before, after = interval_snapshots(capture)
    before.update(instructions=0, cycles=0)
    after.update(before)
    with patch("topdown.collector.apple_stat.time.sleep"):
        result = stat_pid(tmp_path / "stat", 42)
    assert result["status"] == "counters_unavailable"
    assert result["exit_code"] == 3
    assert result["counters_available"] is False


def test_existing_pid_already_exited_is_rejected_before_waiting(tmp_path, capture):
    before, _ = interval_snapshots(capture)
    before["process_exit_mach"] = 50
    with patch("topdown.collector.apple_stat.time.sleep") as sleep:
        result = stat_pid(tmp_path / "stat", 42)
    assert result["status"] == "failed"
    assert "already exited" in result["error"]
    assert result["counters"] is None
    sleep.assert_not_called()


def test_existing_pid_final_zombie_snapshot_is_marked_as_exited(tmp_path, capture):
    _, after = interval_snapshots(capture)
    after["process_exit_mach"] = 1234
    with patch("topdown.collector.apple_stat.time.sleep"):
        result = stat_pid(tmp_path / "stat", 42)
    assert result["status"] == "completed"
    assert result["target_exited"] is True


@pytest.mark.parametrize("pid,duration", [
    (0, 1), (-1, 1), (True, 1), (42.0, 1), ("42", 1), (2**31, 1), (2**32 + 42, 1),
    (42, 0), (42, True), (42, "1"), (42, float("nan")), (42, float("inf")),
])
def test_invalid_pid_interval_does_not_create_output(tmp_path, native_host, pid, duration):
    with pytest.raises(AppleProfileError, match="positive PID"):
        stat_pid(tmp_path / "stat", pid, duration)
    assert not (tmp_path / "stat").exists()


def test_cli_existing_pid_is_mutually_exclusive_with_command(tmp_path):
    with patch("topdown.collector.apple_stat.stat_process") as launch, \
         patch("topdown.collector.apple_stat.stat_pid") as attach:
        result = CliRunner().invoke(app, ["apple", "stat", "--output", str(tmp_path / "stat"),
                                          "--pid", "42", "--", "/owned/target"])
    assert result.exit_code == 1
    assert "either --pid or a command" in result.output
    launch.assert_not_called()
    attach.assert_not_called()


def test_cli_existing_pid_forwards_duration(tmp_path):
    report = {"status": "completed", "exit_code": 0}
    with patch("topdown.collector.apple_stat.stat_pid", return_value=report) as attach:
        result = CliRunner().invoke(app, ["apple", "stat", "--output", str(tmp_path / "stat"),
                                          "--pid", "42", "--duration", "0.5"])
    assert result.exit_code == 0
    assert attach.call_args.args[1:] == (42, 0.5)


@pytest.mark.parametrize("options", [["--pid", "42", "--timeout", "30"],
                                    ["--duration", "5", "--", "/owned/target"]])
def test_cli_does_not_ignore_a_time_limit_for_the_wrong_mode(tmp_path, options):
    with patch("topdown.collector.apple_stat.stat_process") as launch, \
         patch("topdown.collector.apple_stat.stat_pid") as attach:
        result = CliRunner().invoke(app, ["apple", "stat", "--output", str(tmp_path / "stat")] + options)
    assert result.exit_code == 1
    assert "--duration" in result.output and "--timeout" in result.output
    launch.assert_not_called()
    attach.assert_not_called()


@pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64", reason="Native Apple ABI")
@pytest.mark.parametrize("legacy_waitid", [False, True])
def test_native_zombie_read_includes_short_target_and_keeps_target_output(tmp_path, legacy_waitid):
    command = [sys.executable, "-c", "import sys; print('owned-target'); print('stderr', file=sys.stderr)"]
    if legacy_waitid:
        older_os = SimpleNamespace(**{name: value for name, value in vars(os).items() if name != "waitid"})
        with patch("topdown.collector.apple_stat.os", older_os):
            result = stat_process(tmp_path / "stat", command)
    else:
        result = stat_process(tmp_path / "stat", command)
    assert result["status"] == "completed"
    assert result["target_completed"] is True
    assert result["counters"]["instructions"] > 0
    assert result["counters"]["cycles"] > 0
    assert result["counters"]["process_exit_mach"] > result["counters"]["process_start_mach"]
    assert (tmp_path / "stat/target.stdout").read_text() == "owned-target\n"
    assert (tmp_path / "stat/target.stderr").read_text() == "stderr\n"


@pytest.mark.parametrize("code,expected", [("interrupted", "interrupted"), ("tool_error", "failed")])
def test_worker_cancellation_is_distinct_from_other_wait_failures(tmp_path, capture, code, expected):
    capture.wait.side_effect = AppleProfileError("worker stopped", code=code)
    with patch("topdown.collector.apple_stat._terminate") as terminate:
        result = stat_process(tmp_path / "stat", [sys.executable])
    assert result["status"] == expected
    terminate.assert_called_once_with(capture.process)
    if code == "interrupted":
        assert result["exit_code"] == 130 and result["counters"] is not None
    else:
        assert result["counters"] is None and "worker stopped" in result["error"]


@pytest.mark.parametrize("code,expected", [("interrupted", "interrupted"), ("tool_error", "failed")])
def test_pid_worker_cancellation_retains_partial_data_without_signaling(tmp_path, capture, code, expected):
    interval_snapshots(capture)
    with patch("topdown.collector.apple_stat.sleep", side_effect=AppleProfileError("worker stopped", code=code)), \
         patch("topdown.collector.apple_stat._terminate") as terminate:
        result = stat_pid(tmp_path / "stat", 42, 30)
    assert result["status"] == expected
    terminate.assert_not_called()
    capture.launch.assert_not_called()
    if code == "interrupted":
        assert result["exit_code"] == 130 and result["counters"]["instructions"] == 200
    else:
        assert result["counters"] is None


@pytest.mark.parametrize('pid', [True, False, -1, 0, 42.0, '42', 2**31, 2**32 + 42, 10**100])
def test_native_usage_boundary_rejects_pid_aliases_before_calling_libproc(pid):
    from topdown.collector.apple_stat import _UsageReader

    reader = _UsageReader.__new__(_UsageReader)
    with pytest.raises(AppleProfileError, match='native signed pid_t'):
        reader.read(pid)


def test_native_pid_upper_bound_retains_signed_identity():
    from topdown.collector.apple_stat import _validate_pid

    _validate_pid(1)
    _validate_pid(2**31 - 1)


@pytest.mark.parametrize('options', [{'pid':2**32 + 42}, {'duration_seconds':10**1000}])
def test_mcp_collection_rejects_native_bounds_before_any_capture(tmp_path, options):
    from topdown.apple_storage import collect_apple_result

    arguments = {'mode':'stat', 'command':None, 'pid':42, 'duration_seconds':1, **options}
    output = tmp_path / 'unused'
    with pytest.raises(AppleProfileError):
        collect_apple_result(output, **arguments)
    assert not output.exists()


@pytest.mark.parametrize('seconds', [True, '1', 10**1000, 1e100, float('inf'), float('nan'), 0, -1])
def test_stat_rejects_unsupported_waits_before_opening_native_readers(tmp_path, native_host, seconds):
    with patch('topdown.collector.apple_stat._UsageReader') as reader:
        with pytest.raises(AppleProfileError):
            stat_pid(tmp_path / 'pid', 42, seconds)
        with pytest.raises(AppleProfileError):
            stat_process(tmp_path / 'command', [sys.executable], seconds)
        reader.assert_not_called()
    assert not (tmp_path / 'pid').exists() and not (tmp_path / 'command').exists()


@pytest.mark.asyncio
async def test_cancelled_setup_never_launches_the_owned_target(tmp_path, capture, monkeypatch):
    import asyncio
    import threading
    from topdown.collector import apple_stat
    from topdown.collector.apple_runtime import worker

    started, release = threading.Event(), threading.Event()
    report = apple_stat._report
    def paused_report(reader):
        result = report(reader)
        started.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(apple_stat, "_report", paused_report)
    output = tmp_path / "cancelled-setup"
    task = asyncio.create_task(worker(lambda: stat_process(output, [sys.executable]), capture=True)())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    capture.launch.assert_not_called()
    capture.process.wait.assert_not_called()
    retained = json.loads((output / "stat.json").read_text())
    assert retained["status"] == "interrupted" and retained["exit_code"] == 130
    assert retained["counters"] is None
