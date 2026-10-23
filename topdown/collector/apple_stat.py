"""Process-accounted Apple fixed counters, independent of Instruments/KPC setup."""

import ctypes
import errno
import os
import platform
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from topdown.collector.apple import AppleProfileError, TMA_ROOTS, _sha256, _write_json
from topdown.collector.apple_environment import capture_environment
from topdown.collector.apple_runtime import check_cancelled, sleep, uncancellable


def _validate_pid(pid):
    if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
        raise AppleProfileError("Require a positive PID (integer 1–2147483647; native signed pid_t)")


class _RusageInfoV4(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64) for name in """
        ri_user_time ri_system_time ri_pkg_idle_wkups ri_interrupt_wkups ri_pageins
        ri_wired_size ri_resident_size ri_phys_footprint ri_proc_start_abstime ri_proc_exit_abstime
        ri_child_user_time ri_child_system_time ri_child_pkg_idle_wkups ri_child_interrupt_wkups
        ri_child_pageins ri_child_elapsed_abstime ri_diskio_bytesread ri_diskio_byteswritten
        ri_cpu_time_qos_default ri_cpu_time_qos_maintenance ri_cpu_time_qos_background
        ri_cpu_time_qos_utility ri_cpu_time_qos_legacy ri_cpu_time_qos_user_initiated
        ri_cpu_time_qos_user_interactive ri_billed_system_time ri_serviced_system_time
        ri_logical_writes ri_lifetime_max_phys_footprint ri_instructions ri_cycles
        ri_billed_energy ri_serviced_energy ri_interval_max_phys_footprint ri_runnable_time
        """.split()
    ]


class _RusageInfoV6(ctypes.Structure):
    _fields_ = _RusageInfoV4._fields_ + [
        (name, ctypes.c_uint64) for name in """
        ri_flags ri_user_ptime ri_system_ptime ri_pinstructions ri_pcycles ri_energy_nj
        ri_penergy_nj ri_secure_time_in_system ri_secure_ptime_in_system ri_neural_footprint
        ri_lifetime_max_neural_footprint ri_interval_max_neural_footprint ri_conclave_footprint
        ri_page_wait_time_mach ri_page_cache_hits
        """.split()
    ] + [("ri_reserved", ctypes.c_uint64 * 6)]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class _DarwinSiginfo(ctypes.Structure):
    _fields_ = [("si_signo", ctypes.c_int), ("si_errno", ctypes.c_int), ("si_code", ctypes.c_int),
                ("si_pid", ctypes.c_int), ("si_uid", ctypes.c_uint), ("si_status", ctypes.c_int),
                ("si_addr", ctypes.c_void_p), ("si_value", ctypes.c_void_p),
                ("si_band", ctypes.c_long), ("reserved", ctypes.c_ulong * 7)]


class _UsageReader:
    def __init__(self):
        self.libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self.libproc.proc_pid_rusage.restype = ctypes.c_int
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libsystem.mach_timebase_info.argtypes = [ctypes.POINTER(_Timebase)]
        libsystem.mach_timebase_info.restype = ctypes.c_int
        self.timebase = _Timebase()
        if libsystem.mach_timebase_info(ctypes.byref(self.timebase)) or not self.timebase.numer or not self.timebase.denom:
            raise AppleProfileError("Cannot determine Mach timebase")
        self.flavor_id = 6

    def read(self, pid):
        _validate_pid(pid)
        info = _RusageInfoV6() if self.flavor_id == 6 else _RusageInfoV4()
        ctypes.set_errno(0)
        status = self.libproc.proc_pid_rusage(pid, self.flavor_id, ctypes.byref(info))
        if status and self.flavor_id == 6 and ctypes.get_errno() in (errno.EINVAL, errno.ENOTSUP):
            self.flavor_id = 4
            info = _RusageInfoV4()
            ctypes.set_errno(0)
            status = self.libproc.proc_pid_rusage(pid, 4, ctypes.byref(info))
        if status:
            error = ctypes.get_errno()
            raise AppleProfileError(f"proc_pid_rusage({pid}) failed: {os.strerror(error)}")
        numer, denom = self.timebase.numer, self.timebase.denom
        result = {
            "instructions": info.ri_instructions,
            "cycles": info.ri_cycles,
            "user_time_ns": info.ri_user_time * numer // denom,
            "system_time_ns": info.ri_system_time * numer // denom,
            "user_time_mach": info.ri_user_time,
            "system_time_mach": info.ri_system_time,
            "process_start_mach": info.ri_proc_start_abstime,
            "process_exit_mach": info.ri_proc_exit_abstime,
        }
        if self.flavor_id == 6:
            if (info.ri_pinstructions > info.ri_instructions or info.ri_pcycles > info.ri_cycles
                    or info.ri_user_ptime > info.ri_user_time or info.ri_system_ptime > info.ri_system_time):
                raise AppleProfileError("Performance-core usage exceeds aggregate usage")
            result.update(performance_instructions=info.ri_pinstructions, performance_cycles=info.ri_pcycles,
                          other_instructions=info.ri_instructions - info.ri_pinstructions,
                          other_cycles=info.ri_cycles - info.ri_pcycles,
                          performance_user_time_ns=info.ri_user_ptime * numer // denom,
                          performance_system_time_ns=info.ri_system_ptime * numer // denom,
                          performance_user_time_mach=info.ri_user_ptime,
                          performance_system_time_mach=info.ri_system_ptime,
                          other_user_time_mach=info.ri_user_time - info.ri_user_ptime,
                          other_system_time_mach=info.ri_system_time - info.ri_system_ptime,
                          # Scaling each native remainder keeps an idle core group monotonic across snapshots.
                          other_user_time_ns=(info.ri_user_time - info.ri_user_ptime) * numer // denom,
                          other_system_time_ns=(info.ri_system_time - info.ri_system_ptime) * numer // denom)
        return result


def _peek_exit(pid):
    if hasattr(os, "waitid"):
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    # Python exposed macOS waitid only in 3.13; call the same libc API on 3.10–3.12.
    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libsystem.waitid.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_DarwinSiginfo), ctypes.c_int]
    libsystem.waitid.restype = ctypes.c_int
    info = _DarwinSiginfo()
    ctypes.set_errno(0)
    if libsystem.waitid(1, pid, ctypes.byref(info), 0x4 | 0x20 | 0x1):
        error = ctypes.get_errno()
        if error == errno.ECHILD:
            raise ChildProcessError(error, os.strerror(error))
        raise OSError(error, os.strerror(error))
    return info if info.si_pid else None


def _wait_for_exit(pid, deadline):
    while True:
        check_cancelled()
        # Keep the child as a zombie until libproc reads its final counters; wait()/poll() would reap it.
        observed = _peek_exit(pid)
        if observed is not None:
            return observed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.01, remaining))


@uncancellable()
def _terminate(process):
    try:
        if _wait_for_exit(process.pid, time.monotonic()) is not None:
            return
    except ChildProcessError as exc:
        raise AppleProfileError("Target child ownership was lost; no cleanup signal sent") from exc
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    observed = _wait_for_exit(process.pid, time.monotonic() + 2)
    if observed is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        observed = _wait_for_exit(process.pid, time.monotonic() + 2)
    if observed is None:
        raise AppleProfileError("Target exit remains unverified after SIGKILL")


def _report(reader):
    flavor_id = reader.flavor_id if isinstance(getattr(reader, "flavor_id", None), int) else 4
    return {
        "schema_version": 1, "measurement_kind": "process-accounted-fixed-counters",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "collector_sha256": _sha256(Path(__file__)),
        "environment": capture_environment(),
        "source": {"api": "proc_pid_rusage", "flavor": f"RUSAGE_INFO_V{flavor_id}", "flavor_id": flavor_id,
                   "mach_timebase": {"numer": reader.timebase.numer, "denom": reader.timebase.denom}},
        "status": "failed", "exit_code": 1, "counters": None,
        "tma_available": False, "tma": dict.fromkeys(TMA_ROOTS),
        "limitations": [
            "All threads and user/kernel execution of one PID, excluding descendants.",
            "V6 separates performance-core usage from the remainder; per-core slots and measurement bias remain unvalidated.",
            "When V6 is available, 'other' core counts are aggregate totals minus performance-core-only totals.",
            "Fixed cycles/instructions do not provide programmable events, stacks or a TMA decomposition.",
            "Each native Mach CPU-time counter is floored to nanoseconds independently; split sums can differ by 1 ns.",
        ],
    }


def _require_host():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise AppleProfileError("Native Apple counters require macOS on Apple silicon (arm64).")


def stat_pid(output: Path, pid: int, duration: float = 5) -> dict:
    """Read a bounded interval of one existing PID without controlling its lifetime."""
    _validate_pid(pid)
    _require_host()
    if type(duration) not in (int, float) or not 0 < duration <= threading.TIMEOUT_MAX:
        raise AppleProfileError("Require a positive PID and a positive, finite duration")
    reader = _UsageReader()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = _report(reader)
    result.update(duration_seconds=duration, scope={"pid": pid, "threads": "all", "descendants": False,
                                                  "window": "observed-interval", "execution_levels": "user-and-kernel"})
    try:
        before = reader.read(pid)
        result["snapshot_before"] = before
        if before["process_exit_mach"]:
            raise AppleProfileError("Target process has already exited")
        started = time.monotonic_ns()
        interrupted = False
        try:
            sleep(duration)
        except AppleProfileError as exc:
            if exc.code != "interrupted":
                raise
            interrupted = True
        except KeyboardInterrupt:
            interrupted = True
        after = reader.read(pid)
        result["observed_duration_ns"] = time.monotonic_ns() - started
        result["snapshot_after"] = after
        if not before["process_start_mach"] or before["process_start_mach"] != after["process_start_mach"]:
            raise AppleProfileError("Process identity changed or could not be established; no interval counters reported")
        if set(before) != set(after):
            raise AppleProfileError("Counter flavor changed during observation; no interval counters reported")
        fields = tuple(key for key in before if key not in ("process_start_mach", "process_exit_mach"))
        if any(after[field] < before[field] for field in fields):
            raise AppleProfileError("Process counters decreased; no interval counters reported")
        result["counters"] = {field: after[field] - before[field] for field in fields}
        result["target_exited"] = bool(after["process_exit_mach"])
        available = after["instructions"] > 0 and after["cycles"] > 0
        result["counters_available"] = available
        if interrupted:
            result.update(status="interrupted", exit_code=130)
        else:
            result.update(status="completed" if available else "counters_unavailable", exit_code=0 if available else 3)
    except (OSError, AppleProfileError) as exc:
        result["error"] = str(exc)
    finally:
        if isinstance(getattr(reader, "flavor_id", None), int):
            result["source"].update(flavor=f"RUSAGE_INFO_V{reader.flavor_id}", flavor_id=reader.flavor_id)
        result["ended_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(output / "stat.json", result)
    return result


def stat_process(output: Path, command: list[str], timeout: float = 30) -> dict:
    """Launch one PID and read its full-lifetime counters before reaping it.

    Counts include all threads and user/kernel execution, exclude descendants,
    and include executable startup/shutdown. No programmable events are enabled.
    """
    _require_host()
    if type(timeout) not in (int, float) or not 0 < timeout <= threading.TIMEOUT_MAX:
        raise AppleProfileError("Require a positive, finite timeout")
    if not command or not Path(command[0]).is_file() or not os.access(command[0], os.X_OK):
        raise AppleProfileError("Supply an executable path after '--'")
    reader = _UsageReader()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = _report(reader)
    result.update(command=command, executable_sha256=_sha256(Path(command[0])), timeout_seconds=timeout,
                  target_completed=False, target_exit_code=None)
    result["limitations"].append("Process lifetime includes executable startup and shutdown.")
    _write_json(output / "command.json", command)
    process = None
    reaped = False
    try:
        with (output / "target.stdout").open("w") as stdout, (output / "target.stderr").open("w") as stderr:
            check_cancelled()
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            result["scope"] = {"pid": process.pid, "threads": "all", "descendants": False,
                               "window": "process-lifetime", "execution_levels": "user-and-kernel"}
            interrupted, timed_out = False, False
            try:
                timed_out = _wait_for_exit(process.pid, time.monotonic() + timeout) is None
            except AppleProfileError as exc:
                if exc.code != "interrupted":
                    raise
                interrupted = True
            except KeyboardInterrupt:
                interrupted = True
            if timed_out or interrupted:
                _terminate(process)
            result["counters"] = reader.read(process.pid)
            target_exit = process.wait(timeout=2)
            reaped = True
            result.update(target_exit_code=target_exit,
                          target_completed=target_exit == 0 and not timed_out and not interrupted)
            counters = result["counters"]
            available = counters["instructions"] > 0 and counters["cycles"] > 0
            result["counters_available"] = available
            if interrupted:
                result.update(status="interrupted", exit_code=130)
            elif timed_out:
                result.update(status="timeout", exit_code=5)
            elif not available:
                result.update(status="counters_unavailable", exit_code=3)
            else:
                result.update(status="completed", exit_code=target_exit if target_exit >= 0 else 128 - target_exit)
    except (OSError, AppleProfileError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        if isinstance(exc, AppleProfileError) and exc.code == "interrupted":
            result.update(status="interrupted", exit_code=130)
    finally:
        if process is not None and not reaped:
            try:
                _terminate(process)
                result["target_exit_code"] = process.wait(timeout=2)
            except (OSError, AppleProfileError, subprocess.TimeoutExpired) as exc:
                result["cleanup_error"] = str(exc)
        if isinstance(getattr(reader, "flavor_id", None), int):
            result["source"].update(flavor=f"RUSAGE_INFO_V{reader.flavor_id}", flavor_id=reader.flavor_id)
        result["ended_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(output / "stat.json", result)
    return result
