"""Validate Apple fixed instruction accounting with a three-instruction ARM loop."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import select
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from topdown.apple_storage import validate_result  # noqa: E402
from topdown.collector.apple import AppleProfileError, TMA_ROOTS  # noqa: E402
from topdown.collector.apple_environment import capture_environment  # noqa: E402
from topdown.collector.apple_stat import _require_host, _UsageReader, stat_process  # noqa: E402

ORDER = (0, 1_000_000, 10_000_000, 100_000_000, 100_000_000, 10_000_000, 1_000_000, 0)


def _line(process):
    if not select.select([process.stdout], [], [], 5)[0]:
        raise ValueError("Counted-loop child did not answer within five seconds")
    line = process.stdout.readline()
    if not line:
        raise ValueError("Counted-loop child exited before its protocol completed")
    return line


def _interval(binary, iterations, qos, reader):
    process = subprocess.Popen([str(binary), str(iterations), qos, "--barrier"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        if _line(process).strip() != "ready":
            raise ValueError("Counted-loop child did not reach its startup barrier")
        started_at = datetime.now(timezone.utc).isoformat()
        before = reader.read(process.pid)
        started = time.monotonic_ns()
        process.stdin.write("s")
        process.stdin.flush()
        probe = json.loads(_line(process))
        after = reader.read(process.pid)
        ended = time.monotonic_ns()
        if (set(before) != set(after) or before["process_start_mach"] != after["process_start_mach"]
                or before["process_exit_mach"] or after["process_exit_mach"]):
            raise ValueError("Native child identity or counter layout changed during the interval")
        counters = {key: after[key] - value for key, value in before.items()
                    if key not in ("process_start_mach", "process_exit_mach")}
        result = {"measurement_kind": "process-accounted-fixed-counters", "tma_available": False,
                  "started_at": started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
                  "tma": dict.fromkeys(TMA_ROOTS),
                  "source": {"api": "proc_pid_rusage", "flavor": f"RUSAGE_INFO_V{reader.flavor_id}",
                             "flavor_id": reader.flavor_id,
                             "mach_timebase": {"numer": reader.timebase.numer, "denom": reader.timebase.denom}},
                  "scope": {"pid": process.pid, "window": "observed-interval", "descendants": False,
                            "threads": "all", "execution_levels": "user-and-kernel"},
                  "snapshot_before": before, "snapshot_after": after, "counters": counters,
                  "observed_duration_ns": ended - started}
        validate_result(result)
        process.stdin.write("e")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=5)
        if process.returncode or stdout or stderr:
            raise ValueError(f"Counted-loop child failed: {process.returncode}, {stderr}")
        return result, probe
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def validate(output, repeats=3):
    _require_host()
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("Choose 1–10 repetitions")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).with_name("apple-counted-loop.c")
    binary = output / "counted-loop"
    command = ["xcrun", "clang", "-O2", "-arch", "arm64", str(source), "-o", str(binary)]
    (output / "compile.command.json").write_text(json.dumps(command) + "\n")
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    (output / "compile.stdout").write_text(compiled.stdout)
    (output / "compile.stderr").write_text(compiled.stderr)
    compiled.check_returncode()
    disassembly = subprocess.run(["otool", "-tvV", str(binary)], capture_output=True, text=True,
                                 timeout=10, check=True, stdin=subprocess.DEVNULL)
    (output / "disassembly.txt").write_text(disassembly.stdout)
    evidence = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "validation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "environment": capture_environment(), "order": list(ORDER) * repeats, "cases": [],
                "limitations": ["The loop executes three user-mode instructions per iteration.",
                                "Native counters include kernel and measurement-protocol work.",
                                "Process lifetime includes executable startup and shutdown.",
                                "QoS requests influence scheduling and do not pin a core kind.",
                                "Frequency, scheduling and background activity are not controlled.",
                                "This checks fixed counters; it cannot establish programmable-event TMA."]}
    reader = _UsageReader()
    for trial, iterations in enumerate(evidence["order"]):
        for qos in (("default", "background") if trial % 2 == 0 else ("background", "default")):
            case = output / f"trial-{trial:02d}-{qos}-{iterations}"
            lifetime = stat_process(case, [str(binary), str(iterations), qos], timeout=5)
            if lifetime["status"] != "completed" or lifetime["exit_code"]:
                raise ValueError(f"Counted-loop lifetime capture failed; artifacts: {case}")
            validate_result(lifetime)
            probe = json.loads((case / "target.stdout").read_text())
            interval, verified = _interval(binary, iterations, qos, reader)
            if (probe != verified or probe.get("iterations") != iterations or probe.get("checksum") != iterations
                    or probe.get("loop_instructions") != iterations * 3):
                raise ValueError(f"Counted-loop checksum/configuration mismatch; artifacts: {case}")
            interval["environment"] = evidence["environment"]
            (case / "interval.json").write_text(json.dumps(interval, indent=2, allow_nan=False) + "\n")
            evidence["cases"].append({"trial": trial, "iterations": iterations, "qos": qos,
                                      "probe": probe, "stat": lifetime, "interval": interval})
            (output / "results.json").write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n")
            print(f"{qos:10} {iterations:>9} iterations: lifetime {lifetime['counters']['instructions']} instructions; "
                  f"interval {interval['counters']['instructions']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for native results and disassembly")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    try:
        validate(args.output, args.repeats)
    except KeyboardInterrupt:
        parser.exit(130, f"Fixed-instruction validation cancelled; artifacts retained at {args.output}\n")
    except (OSError, ValueError, AppleProfileError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
