"""Collect native GPU replay counters with gpudebug; keep capture resources local."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import threading
import time

from topdown.collector.apple import AppleProfileError, TMA_ROOTS
from topdown.collector import apple_runtime
from topdown.collector.apple_files import NativeJSONDecoder, decode_json_object, read_regular_bytes
from topdown.collector.apple_environment import capture_environment

GPU_KIND = "apple-gpu-replay-profile"
GPU_MODES = {"compute", "alu", "memory", "stream"}
GPU_STATE_LABELS = {"default": ("default",), "low": ("low", "min"),
                    "medium": ("medium",), "high": ("high", "max")}
TIMELINE_GROUPS = ("bandwidth", "occupancy", "shader_launch_limiter", "active_cores", "f32")


@contextmanager
def _defer_keyboard_interrupt():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {signum: handler for signum in (signal.SIGINT, signal.SIGTERM)
                if callable(handler := signal.getsignal(signum))}
    if not previous:
        yield
        return
    pending = None

    def remember(signum, frame):
        nonlocal pending
        if pending is None or signum == signal.SIGTERM:
            pending = signum, frame

    for signum in previous:
        signal.signal(signum, remember)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if pending is not None and sys.exc_info()[0] is None:
            previous[pending[0]](*pending)


def validate_options(mode="compute", passes=4, items=16384, execution="overlapping", gpu_state="default"):
    if (not isinstance(mode, str) or mode not in GPU_MODES or type(passes) is not int or not 1 <= passes <= 500
            or type(items) is not int or not 1 <= items <= 16777216 or passes * items > 268435456
            or (mode == "stream" and items % 64) or execution not in ("overlapping", "serial")
            or not isinstance(gpu_state, str) or gpu_state not in GPU_STATE_LABELS):
        raise ValueError("Choose compute/alu/memory/stream, passes 1–500, items 1–16777216, passes*items <= 268435456, "
                         "stream items divisible by 64, overlapping/serial execution, and default/low/medium/high GPU state")


def _require_host():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("GPU capture/replay requires Apple silicon macOS and Xcode; stored reports work on any OS")


def record(output: Path, mode="compute", passes=4, items=16384, execution="overlapping", gpu_state="default") -> dict:
    """Capture the packaged, checksum-verified Metal control and profile its replay."""
    validate_options(mode, passes, items, execution, gpu_state)
    _require_host()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    source = Path(__file__).with_name("apple_gpu_probe.swift")

    def run(name, command, timeout, env=None):
        (output / f"{name}.command.json").write_text(json.dumps(command))
        try:
            result = apple_runtime.run(command, timeout=timeout, env=env)
        except (subprocess.TimeoutExpired, AppleProfileError) as exc:
            (output / f"{name}.stdout").write_bytes(getattr(exc, "stdout", None) or b"")
            (output / f"{name}.stderr").write_bytes(getattr(exc, "stderr", None) or b"")
            if isinstance(exc, AppleProfileError):
                raise
            raise ValueError(f"GPU {name} timed out; artifacts: {output}") from exc
        (output / f"{name}.stdout").write_text(result.stdout)
        (output / f"{name}.stderr").write_text(result.stderr)
        if result.returncode:
            raise ValueError(f"GPU {name} failed: {result.stderr.strip() or result.stdout.strip()}; artifacts: {output}")

    binary, trace = output / "probe", output / "capture.gputrace"
    run("compile", ["xcrun", "swiftc", "-O", "-framework", "Metal", str(source), "-o", str(binary)], 90)
    run("capture", [str(binary), str(passes), "--mode", mode, "--items", str(items),
                    "--capture", str(trace), "--result", str(output / "probe.json")], 60,
        {**os.environ, "MTL_CAPTURE_ENABLED": "1"})
    probe = decode_json_object(read_regular_bytes(output / "probe.json"))
    if (not isinstance(probe, dict) or probe.get("checksum_verified") is not True
            or probe.get("mode") != mode or probe.get("passes") != passes or probe.get("items") != items):
        raise ValueError(f"GPU control failed checksum/configuration verification; artifacts: {output}")
    result = collect(trace, output / "replay", execution, gpu_state)
    result.update(started_at=started, ended_at=datetime.now(timezone.utc).isoformat(),
                  probe=probe,
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    (output / "profile.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def json_documents(text: str) -> list[dict]:
    decoder = NativeJSONDecoder()
    documents = []
    while text.strip():
        value, end = decoder.raw_decode(text.lstrip())
        if not isinstance(value, dict):
            raise ValueError("Expected gpudebug JSON objects")
        documents.append(value)
        text = text.lstrip()[end:]
    if not documents:
        raise ValueError("gpudebug returned no JSON")
    return documents


def _created_session(output):
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).replace("\r\n", "\n").replace("\r", "\n")
    sessions = set(re.findall(r"(?m)^Session ([0-9]+) created\.[ \t]*\r?$", output))
    return next(iter(sessions)) if len(sessions) == 1 else None


def collect(trace: Path, output: Path, execution: str = "overlapping", gpu_state: str = "default") -> dict:
    if (execution not in ("overlapping", "serial")
            or not isinstance(gpu_state, str) or gpu_state not in GPU_STATE_LABELS):
        raise ValueError("Choose overlapping/serial execution and default/low/medium/high replay GPU state")
    if not trace.exists() or trace.suffix != ".gputrace":
        raise ValueError("Require an existing .gputrace capture")
    output.mkdir(parents=True, exist_ok=False)
    sequence = 0
    session = None
    started = datetime.now(timezone.utc).isoformat()
    expires = time.monotonic() + 300
    environment = capture_environment()

    def run(arguments, timeout=120):
        nonlocal sequence, session
        apple_runtime.check_cancelled()
        if "--terminate" not in arguments:
            timeout = min(timeout, max(0, expires - time.monotonic()))
            if not timeout:
                raise ValueError(f"GPU replay exceeded its 300 second budget; artifacts: {output}")
        sequence += 1
        command = ["xcrun", "gpudebug", *arguments]
        (output / f"{sequence:03d}.command.json").write_text(json.dumps(command))
        with _defer_keyboard_interrupt():
            try:
                # Killing a replay client can strand its XPC profiling operation after session removal.
                with apple_runtime.uncancellable():
                    result = apple_runtime.run(command, timeout=timeout, start_new_session=True, stdin=subprocess.DEVNULL)
            except (subprocess.TimeoutExpired, AppleProfileError) as exc:
                stdout = getattr(exc, "stdout", None) or b""
                stderr = getattr(exc, "stderr", None) or b""
                (output / f"{sequence:03d}.stdout").write_bytes(stdout)
                (output / f"{sequence:03d}.stderr").write_bytes(stderr)
                if "-t" in arguments:
                    session = _created_session(stdout)
                if isinstance(exc, AppleProfileError):
                    raise
                raise ValueError(f"gpudebug timed out; partial artifacts: {output}") from exc
            (output / f"{sequence:03d}.stdout").write_text(result.stdout)
            (output / f"{sequence:03d}.stderr").write_text(result.stderr)
            if "-t" in arguments:
                session = _created_session(result.stdout)
            apple_runtime.check_cancelled()
            diagnostic = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout + "\n" + result.stderr)
            errors = [line.strip() for line in diagnostic.splitlines() if line.lstrip().lower().startswith("error:")]
            if result.returncode or errors:
                detail = "\n".join(errors) or result.stderr.strip() or result.stdout.strip()
                raise ValueError(f"gpudebug failed: {detail}; artifacts: {output}")
            return result.stdout

    def query(command):
        response = run(["-s", session, "--json", "-c", command])
        try:
            return json_documents(response)[-1]
        except ValueError as exc:
            raise ValueError(f"Unexpected gpudebug response to {command!r}: {response.strip()[:300]}; artifacts: {output}") from exc

    try:
        run(["-t", str(trace.resolve()), "--timeout", "180", "-c", "profile"])
        if session is None:
            raise ValueError(f"gpudebug did not report an unambiguous created session; artifacts: {output}")
        run(["-s", session, "-c", f"profile run --gpu-state {gpu_state} --exec {execution}"])
        status = query("profile")
        if status.get("state") != "loaded":
            raise ValueError(f"GPU replay profiling is unavailable: {status}; artifacts: {output}")
        # Xcode can report "loaded" before its asynchronous performance tree is populated.
        deadline = time.monotonic() + 30
        while True:
            try:
                query("go /performance")
                query("go /performance/encoders")
                listing = query("list --all")
                if listing.get("children"):
                    break
            except ValueError as exc:
                if "has no info" not in str(exc) and "no info handler" not in str(exc):
                    raise
            if time.monotonic() >= deadline:
                raise ValueError(f"GPU performance tables did not become ready; artifacts: {output}")
            apple_runtime.sleep(0.25)
        children = listing.get("children")
        if (not isinstance(children, list) or not children or listing.get("totalCount") != len(children)
                or any(not isinstance(row, dict) or not re.fullmatch(r"[a-z]+\d+", str(row.get("name", ""))) for row in children)):
            raise ValueError("Incomplete or unexpected GPU encoder listing")
        encoders = [{"id": row["name"], "listing": row, "counters": query(f"info /performance/encoders/{row['name']}")}
                    for row in children]
        query("go /performance/timeline/counters")
        available = query("list --all")
        groups = {}
        children = available.get("children")
        if (not isinstance(children, list) or type(available.get("totalCount")) is not int
                or available["totalCount"] != len(children)
                or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in children)
                or len({row["name"] for row in children}) != len(children)):
            raise ValueError("Incomplete or unexpected GPU timeline counter groups")
        for group in TIMELINE_GROUPS:
            if group not in {row["name"] for row in children}:
                continue
            prefix = f"/performance/timeline/counters/{group}"
            query(f"go {prefix}")
            listing = query("list --all")
            leaves = listing.get("children")
            if (not isinstance(leaves, list) or type(listing.get("totalCount")) is not int
                    or listing["totalCount"] != len(leaves)
                    or any(not isinstance(row, dict) or not re.fullmatch(r"[a-z][a-z0-9_]*", str(row.get("name", "")))
                           for row in leaves)
                    or len({row["name"] for row in leaves}) != len(leaves)):
                raise ValueError(f"Incomplete or unexpected GPU timeline counter listing: {group}")
            # Unobserved leaves can return a synthetic zero from `info`; retain their absence.
            groups[group] = {"listing": listing, "counters": {
                row["name"]: query(f"info {prefix}/{row['name']}") if row.get("values") else None for row in leaves}}
        timeline = query("info /performance/timeline")
        if timeline.get("Profiled state", gpu_state) not in GPU_STATE_LABELS[gpu_state]:
            raise ValueError("Native GPU profiled state differs from the requested replay state")
        document = {
            "schema_version": 1, "measurement_kind": "apple-gpu-replay-profile",
            "scope": {"kind": "captured-metal-workload", "trace_name": trace.name},
            "settings": {"execution": execution, "gpu_state": gpu_state, "replayed": True},
            "device": query("status").get("device"), "timeline": timeline, "encoders": encoders,
            "timeline_counter_groups": groups,
            "environment": environment,
            "tma_available": False, "tma": dict.fromkeys(TMA_ROOTS),
            "started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(),
            "limitations": [
                "Measurements describe replay of the captured Metal workload, not original application wall time.",
                "Native limiter percentages overlap and must not be summed into a pipeline-slot partition.",
                "Encoder cost is the profiler's cost attribution, not a CPU-time percentage.",
                "Replay GPU state and execution mode can change timings; compare matched settings.",
                "Timeline hardware counters may include concurrent device activity; they are not exclusive to captured encoders.",
            ],
        }
        from topdown.apple_gpu_report import summarize

        summarize(document)
        (output / "profile.json").write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
        return document
    finally:
        if session is not None:
            failing = sys.exc_info()[0] is not None
            try:
                with apple_runtime.uncancellable():
                    run(["--terminate", session], timeout=15)
            except (OSError, ValueError):
                if not failing:
                    raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new directory for native JSON and command logs")
    parser.add_argument("--execution", choices=["overlapping", "serial"], default="overlapping")
    parser.add_argument("--gpu-state", choices=list(GPU_STATE_LABELS), default="default")
    args = parser.parse_args()
    try:
        result = collect(args.trace, args.output, args.execution, args.gpu_state)
    except KeyboardInterrupt:
        parser.exit(130, f"GPU replay cancelled; artifacts retained at {args.output}\n")
    except (AppleProfileError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"profile": str(args.output / "profile.json"), "device": result["device"],
                      "encoder_count": len(result["encoders"]), "timeline": result["timeline"]}, indent=2))


if __name__ == "__main__":
    main()
