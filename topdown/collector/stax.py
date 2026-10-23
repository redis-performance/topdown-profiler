"""Optional Stax subprocess capture with a private, owned query server."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import sys

from topdown.collector.apple import AppleProfileError
from topdown.collector.apple_runtime import _signal_owned, check_cancelled, sleep, uncancellable
from topdown.stax_archive import StaxError, capture_summary, uint


def _executable(variable, default):
    value = os.environ.get(variable, default)
    resolved = shutil.which(value)
    return str(Path(resolved).resolve()) if resolved else None


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(data)
    return digest.hexdigest()


def doctor():
    """Inspect installed tools and permission hints without installing a helper."""
    system = platform.system()
    tools = {
        "stax": _executable("TOPDOWN_STAX_PATH", "stax"),
        "stax-server": _executable("TOPDOWN_STAX_SERVER_PATH", "stax-server"),
    }
    daemon = os.environ.get("TOPDOWN_STAX_DAEMON_SOCKET", "/var/run/staxd.sock")
    helper_present = False
    helper_private = False
    helper_details = {}
    if system == "Darwin":
        try:
            endpoint = Path(daemon).lstat()
            directory = Path(daemon).parent.lstat()
            helper_present = stat.S_ISSOCK(endpoint.st_mode)
            helper_private = (
                Path(daemon).is_absolute()
                and helper_present
                and endpoint.st_uid == 0
                and stat.S_ISDIR(directory.st_mode)
                and directory.st_uid == os.getuid()
                and not directory.st_mode & 0o077
            )
            helper_details = {
                "socket_owner_uid": endpoint.st_uid,
                "directory_owner_uid": directory.st_uid,
                "directory_mode": stat.S_IMODE(directory.st_mode),
            }
        except OSError:
            pass
    paranoid = None
    if system == "Linux":
        try:
            paranoid = Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()
        except OSError:
            pass
    reasons = []
    if system not in ("Darwin", "Linux"):
        reasons.append("Stax capture requires macOS or Linux")
    reasons.extend(f"Missing optional executable: {name}" for name, path in tools.items() if path is None)
    if system == "Darwin" and not helper_present:
        reasons.append("macOS capture requires the separately installed, signed Stax privileged helper")
    elif system == "Darwin" and not helper_private:
        reasons.append("Stax requires a root-owned helper socket in a private 0700 directory owned by this user")
    return {
        "collector": "stax",
        "os": system,
        "arch": platform.machine(),
        "executables": tools,
        "capture_prerequisites_present": not reasons,
        "reasons": reasons,
        "daemon_socket": daemon,
        "helper_socket_present": helper_present,
        "helper_socket_private": helper_private,
        "helper_socket_details": helper_details,
        "perf_event_paranoid": paranoid,
        "permissions_verified": False,
        "pmu_availability": "unverified",
        "limitations": [
            "Prerequisite presence does not establish permission or counter availability.",
            "Archive import and database queries do not require Stax executables.",
        ]
        + (
            [
                "M1 and M6 require independent capture checks on the installed kernel.",
                "The pinned macOS helper requires its session teardown guard patch before use; patch status is not detected.",
            ]
            if system == "Darwin"
            else []
        ),
    }


def _finish(process):
    if process is None or process.poll() is not None:
        return
    _signal_owned(process, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _signal_owned(process, signal.SIGKILL)
        process.wait(timeout=2)


def _finish_target(process):
    if process is None or process.returncode is not None:
        return
    from topdown.collector.apple_stat import _peek_exit

    # Retain the unreaped leader as the process-group identity until descendant cleanup is done.
    _signal_target_group(process, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while _peek_exit(process.pid) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    _signal_target_group(process, signal.SIGKILL)
    process.wait(timeout=2)


def _signal_target_group(process, sig):
    try:
        _signal_owned(process, sig)
    except PermissionError:
        from topdown.collector.apple_stat import _peek_exit

        if sys.platform != "darwin" or _peek_exit(process.pid) is None:
            raise
        # Darwin returns EPERM for groups containing only zombies; keep the leader unreaped while checking.
        result = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=3)
        if result.returncode or any(
            len(parts := line.split()) >= 2 and parts[0] == str(process.pid) and not parts[1].startswith("Z")
            for line in result.stdout.splitlines()
        ):
            raise


def _run(command, env, timeout):
    process = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StaxError(f"Stax command timed out after {timeout}s")
            try:
                out, err = process.communicate(timeout=min(0.1, remaining))
                if process.returncode:
                    raise StaxError(f"Stax command failed ({process.returncode}): {(err or out)[-4000:].strip()}")
                return out
            except subprocess.TimeoutExpired:
                continue
    finally:
        _finish(process)


def record_profile(*, output, pid=None, command=None, duration_seconds=5, frequency_hz=99):
    """Attach or launch one target and retain a directory archive plus capture logs.

    Never reuses STAX_SERVER_SOCKET or stops a shared server's recording.
    Attached processes are never signaled. Launched targets inherit the private socket
    and are stopped after recording; startup before the attachment can be missed.
    """
    if (pid is None) == (command is None):
        raise StaxError("Choose exactly one existing PID or executable argv list")
    if pid is not None:
        uint(pid, "PID", 32)
        if not 0 < pid < 2**31:
            raise StaxError("PID must be a positive signed 32-bit process ID")
    if command is not None and (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
        or not command[0].strip()
    ):
        raise StaxError("Command must be a nonempty executable argv list without NUL characters")
    if type(duration_seconds) is not int or not 1 <= duration_seconds <= 3600:
        raise StaxError("Duration must be a whole number between 1 and 3600 seconds")
    if type(frequency_hz) is not int or not 1 <= frequency_hz <= 10000:
        raise StaxError("Frequency must be between 1 and 10000 Hz")
    capabilities = doctor()
    if not capabilities["capture_prerequisites_present"]:
        raise StaxError("; ".join(capabilities["reasons"]))
    check_cancelled()
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    tools = capabilities["executables"]
    metadata = {
        "collector": "stax",
        "pid": pid,
        "duration_seconds": duration_seconds,
        "command": command,
        "frequency_hz": frequency_hz,
        "capabilities": capabilities,
        "target_ownership": "launched" if command is not None else "attached",
    }
    server, recorder, target = None, None, None
    try:
        metadata["binary_sha256"] = {name: _digest(path) for name, path in tools.items()}
        with tempfile.TemporaryDirectory(prefix="td-stax-", dir="/tmp") as private, ExitStack() as files:
            env = {
                **os.environ,
                "STAX_SERVER_SOCKET": str(Path(private) / "server.sock"),
                "STAX_SERVER_WS_BIND": "127.0.0.1:0",
            }
            metadata["stax_version"] = _run([tools["stax"], "--version"], env, 3).strip()
            server_log = files.enter_context((output / "server.log").open("w"))
            record_log = files.enter_context((output / "record.log").open("w"))
            server = subprocess.Popen(
                [tools["stax-server"]],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    check_cancelled()
                    if server.poll() is not None:
                        raise StaxError("Private Stax server exited during startup")
                    if Path(env["STAX_SERVER_SOCKET"]).exists():
                        _run([tools["stax"], "status"], env, 3)
                        break
                    if time.monotonic() >= deadline:
                        raise StaxError("Private Stax server did not become ready within 10 seconds")
                    sleep(0.05)
                if command is not None:
                    target_log = files.enter_context((output / "target.log").open("w"))
                    target = subprocess.Popen(
                        command,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=target_log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    pid = target.pid
                    metadata["pid"] = pid
                    metadata["startup_coverage"] = "target starts before attachment; early initialization can be missed"
                arguments = [
                    tools["stax"],
                    "record",
                    "--pid",
                    str(pid),
                    "--time-limit",
                    str(duration_seconds),
                    "--frequency",
                    str(frequency_hz),
                ]
                if platform.system() == "Darwin":
                    arguments.extend(["--daemon-socket", capabilities["daemon_socket"]])
                recorder = subprocess.Popen(
                    arguments,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=record_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                deadline = time.monotonic() + duration_seconds + 30
                target_exited = False
                while recorder.poll() is None:
                    check_cancelled()
                    if server.poll() is not None:
                        raise StaxError("Private Stax server exited during recording")
                    if time.monotonic() >= deadline:
                        raise StaxError("Stax recorder exceeded its capture deadline")
                    if target is not None and not target_exited:
                        from topdown.collector.apple_stat import _peek_exit

                        target_exited = _peek_exit(target.pid) is not None
                        if target_exited:
                            metadata["target_exited_before_limit"] = True
                            try:
                                _run([tools["stax"], "stop"], env, 3)
                            except StaxError as exc:
                                try:
                                    recorder.wait(timeout=1)
                                except subprocess.TimeoutExpired:
                                    raise exc
                    sleep(0.05)
                if recorder.returncode:
                    with (output / "record.log").open("rb") as log:
                        log.seek(max(0, log.seek(0, 2) - 4000))
                        detail = log.read().decode("utf-8", errors="replace").strip()
                    raise StaxError(f"Stax recorder failed ({recorder.returncode}): {detail}")
                _finish_target(target)
                if target is not None:
                    metadata["target_exit_code"] = target.returncode
                _run([tools["stax"], "save", str(output / "archive")], env, 30)
                metadata["run"] = capture_summary(output / "archive", pid)
                metadata["status"] = "completed"
            finally:
                with uncancellable():
                    interrupted = sys.exc_info()[1] is not None
                    if recorder is not None and recorder.poll() is None and server.poll() is None:
                        try:
                            _run([tools["stax"], "stop"], env, 3)
                            recorder.wait(timeout=3)
                        except (OSError, StaxError, subprocess.TimeoutExpired) as exc:
                            metadata["partial_save_error"] = str(exc)
                    if interrupted and recorder is not None and recorder.poll() == 0 and server.poll() is None:
                        try:
                            partial = output / "partial-archive"
                            _run([tools["stax"], "save", str(partial)], env, 10)
                            metadata["partial_run"] = capture_summary(partial, pid)
                            metadata["partial_archive"] = str(partial)
                        except (OSError, StaxError, subprocess.TimeoutExpired) as exc:
                            metadata["partial_save_error"] = str(exc)
                    try:
                        _finish(recorder)
                    finally:
                        try:
                            _finish_target(target)
                        finally:
                            _finish(server)
    except BaseException as exc:
        interrupted = (
            isinstance(exc, KeyboardInterrupt) or isinstance(exc, AppleProfileError) and exc.code == "interrupted"
        )
        metadata.update(status="interrupted" if interrupted else "failed", error=str(exc))
        if isinstance(exc, AppleProfileError):
            detail = "Stax capture cancelled" if interrupted else str(exc)
            raise StaxError(f"{detail}; artifacts retained at {output}") from exc
        if isinstance(exc, (OSError, StaxError)):
            raise StaxError(f"{exc}; artifacts retained at {output}") from exc
        raise
    finally:
        failure = sys.exc_info()[1]
        try:
            (output / "capture.json").write_text(json.dumps(metadata, indent=2) + "\n")
        except OSError as exc:
            if failure is None:
                raise StaxError(f"Could not write capture metadata; artifacts retained at {output}: {exc}") from exc
            if hasattr(failure, "add_note"):
                failure.add_note(f"Could not write capture metadata at {output}: {exc}")
    return output / "archive", metadata
