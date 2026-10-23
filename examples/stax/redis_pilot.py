#!/usr/bin/env python3
"""Interleave baseline/Stax windows on an owned Redis and retain raw pilot evidence."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import tempfile
import threading
import time

from topdown.collector.stax import _finish_target, record_profile
from topdown.stax_storage import get_stax_hotspots, query_stax_result, store_stax_result
from topdown.storage.sqlite_backend import SQLiteBackend


class Redis:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(3)
        self.socket.connect(str(path))
        self.stream = self.socket.makefile("rb")

    def close(self):
        self.stream.close()
        self.socket.close()

    def call(self, *arguments):
        encoded = [str(arg).encode() for arg in arguments]
        self.socket.sendall(
            b"*%d\r\n" % len(encoded) + b"".join(b"$%d\r\n" % len(arg) + arg + b"\r\n" for arg in encoded)
        )
        return self._read()

    def _read(self):
        line = self.stream.readline()
        if not line:
            raise OSError("Redis closed the pilot connection")
        kind, value = line[:1], line[1:-2]
        if kind == b"-":
            raise OSError(value.decode())
        if kind == b"+":
            return value.decode()
        if kind == b":":
            return int(value)
        if kind == b"$":
            length = int(value)
            if length < 0:
                return None
            result = self.stream.read(length)
            if len(result) != length or self.stream.read(2) != b"\r\n":
                raise OSError("Truncated Redis reply")
            return result.decode()
        if kind == b"*":
            return [self._read() for _ in range(int(value))]
        raise OSError("Unexpected Redis pilot reply")


def info(text):
    return dict(line.split(":", 1) for line in text.splitlines() if line and not line.startswith("#"))


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def process_snapshot(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
        return {
            "start_ticks": int(fields[19]),
            "cpu_ticks": int(fields[11]) + int(fields[12]),
            "rss_bytes": max(0, int(fields[21])) * os.sysconf("SC_PAGE_SIZE"),
            "read_at_monotonic_ns": time.monotonic_ns(),
        }
    except (OSError, ValueError, IndexError):
        return None


def observe(path, seconds, process_ids=None):
    client = Redis(path)
    stop, latencies, errors = threading.Event(), [], []

    def ping():
        probe = None
        try:
            probe = Redis(path)
            while not stop.is_set():
                start = time.perf_counter()
                probe.call("PING")
                if not stop.is_set():
                    latencies.append((time.perf_counter() - start) * 1000)
                stop.wait(0.01)
        except OSError as exc:
            errors.append(str(exc))
        finally:
            if probe is not None:
                probe.close()

    thread = threading.Thread(target=ping)
    try:
        client.call("CONFIG", "RESETSTAT")
        started_at_ns = time.time_ns()
        started_monotonic_ns = time.monotonic_ns()
        started = time.perf_counter()
        before = {name: process_snapshot(pid) for name, pid in (process_ids or {}).items()}
        thread.start()
        time.sleep(seconds)
        client.call("MULTI")
        for section in ("stats", "commandstats", "latencystats"):
            client.call("INFO", section)
        stats, commands, latency = map(info, client.call("EXEC"))
        elapsed = time.perf_counter() - started
        resources = {}
        for name, pid in (process_ids or {}).items():
            after, first = process_snapshot(pid), before[name]
            if first is None or after is None or first["start_ticks"] != after["start_ticks"]:
                resources[name] = {"available": False, "reason": "process missing or identity changed"}
            else:
                resources[name] = {
                    "available": True,
                    "pid": pid,
                    "cpu_percent_of_one_logical_cpu": 100
                    * (after["cpu_ticks"] - first["cpu_ticks"])
                    / os.sysconf("SC_CLK_TCK")
                    / ((after["read_at_monotonic_ns"] - first["read_at_monotonic_ns"]) / 1e9),
                    "rss_before_bytes": first["rss_bytes"],
                    "rss_after_bytes": after["rss_bytes"],
                }
        ended_at_ns = time.time_ns()
        ended_monotonic_ns = time.monotonic_ns()
        stop.set()
        thread.join(4)
        return {
            "started_at_unix_ns": started_at_ns,
            "ended_at_unix_ns": ended_at_ns,
            "started_at_monotonic_ns": started_monotonic_ns,
            "ended_at_monotonic_ns": ended_monotonic_ns,
            "duration_seconds": elapsed,
            "commands_per_second": int(stats["total_commands_processed"]) / elapsed,
            "commandstats": commands,
            "server_latency_percentiles_us": latency,
            "ping_rtt_ms": {
                "samples": len(latencies),
                "p50": percentile(latencies, 0.5),
                "p99": percentile(latencies, 0.99),
            },
            "probe_errors": errors,
            "process_resources": resources,
        }
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(4)
        client.close()


def summarize(trials, budget):
    pairs = {}
    for trial in trials:
        pairs.setdefault(trial["pair"], {})[trial["mode"]] = trial
    complete = [pair for pair in pairs.values() if "baseline" in pair and "profile" in pair]
    losses, rtt_changes = [], []
    for pair in complete:
        baseline, profile = pair["baseline"], pair["profile"]
        losses.append(100 * (1 - profile["commands_per_second"] / baseline["commands_per_second"]))
        a, b = baseline["ping_rtt_ms"]["p99"], profile["ping_rtt_ms"]["p99"]
        if a and b is not None:
            rtt_changes.append(100 * (b / a - 1))
    rates = [trial["commands_per_second"] for trial in trials if trial["mode"] == "baseline"]
    if not losses:
        return {"status": "insufficient_pairs"}
    variation = 100 * (max(rates) / min(rates) - 1)
    stable = variation <= budget["max_baseline_rate_range_pct"]
    valid = all(
        trial.get("measurement_inside_capture", True)
        and trial.get("measurement_inside_observed_clock", True)
        and not trial["probe_errors"]
        for trial in trials
    )
    accepted = (
        stable
        and valid
        and statistics.median(losses) <= budget["median_throughput_loss_pct"]
        and bool(rtt_changes)
        and statistics.median(rtt_changes) <= budget["median_ping_p99_increase_pct"]
    )
    return {
        "status": "within_pilot_budget"
        if accepted
        else "inconclusive"
        if not stable or not valid
        else "exceeds_pilot_budget",
        "paired_throughput_loss_pct": losses,
        "median_throughput_loss_pct": statistics.median(losses),
        "paired_ping_p99_increase_pct": rtt_changes,
        "baseline_rate_range_pct": variation,
        "scope": "one virtualized host and this workload; PING RTT is not GET/SET end-to-end latency",
    }


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    budget = {"median_throughput_loss_pct": 10, "median_ping_p99_increase_pct": 20, "max_baseline_rate_range_pct": 10}
    evidence = {
        "schema_version": 1,
        "status": "running",
        "budget_declared_before_measurement": budget,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "adapter_sha256": hashlib.sha256(Path(inspect.getsourcefile(record_profile)).read_bytes()).hexdigest(),
        "environment": {
            "system": platform.platform(),
            "arch": platform.machine(),
            "redis": subprocess.check_output([str(args.redis_server), "--version"], text=True).strip(),
        },
        "settings": {
            "frequency_hz": args.frequency,
            "profile_seconds": args.duration + 3,
            "measurement_seconds": args.duration,
            "repeats": args.repeats,
            "clients_per_command": 16,
            "pipeline": args.pipeline,
            "payload_bytes": 128,
            "keyspace": 10000,
            "commands": ["SET", "GET"],
            "transport": "private Unix socket",
        },
        "trials": [],
    }
    backend = SQLiteBackend(output / "profiles.db")
    backend.initialize()
    processes, server = [], None
    with (
        tempfile.TemporaryDirectory(prefix="td-stax-redis-", dir="/tmp") as private,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        path = Path(private) / "redis.sock"
        try:
            with (output / "redis.log").open("w") as log:
                server = subprocess.Popen(
                    [
                        str(args.redis_server),
                        "--port",
                        "0",
                        "--unixsocket",
                        str(path),
                        "--unixsocketperm",
                        "700",
                        "--save",
                        "",
                        "--appendonly",
                        "no",
                        "--dir",
                        private,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            deadline = time.monotonic() + 5
            while not path.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Pilot Redis did not start")
                time.sleep(0.01)
            for command in ("set", "get"):
                with (output / (command + "-client.log")).open("w") as log:
                    processes.append(
                        subprocess.Popen(
                            [
                                str(args.redis_benchmark),
                                "-s",
                                str(path),
                                "-t",
                                command,
                                "-n",
                                "2147483647",
                                "-c",
                                "16",
                                "-P",
                                str(args.pipeline),
                                "-r",
                                "10000",
                                "-d",
                                "128",
                                "--csv",
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                    )
            time.sleep(2)
            (output / "pilot.json").write_text(json.dumps(evidence, indent=2) + "\n")
            for pair in range(args.repeats):
                order = ("baseline", "profile") if pair % 2 == 0 else ("profile", "baseline")
                for mode in order:
                    capture = None
                    if mode == "profile":
                        capture = pool.submit(
                            record_profile,
                            pid=server.pid,
                            output=output / f"capture-{pair}",
                            duration_seconds=args.duration + 3,
                            frequency_hz=args.frequency,
                        )
                    time.sleep(2)
                    process_ids = {
                        "redis": server.pid,
                        "set_client": processes[0].pid,
                        "get_client": processes[1].pid,
                        "pilot": os.getpid(),
                    }
                    live = output / f"capture-{pair}" / "processes.json"
                    if capture and live.exists():
                        owned = json.loads(live.read_text())
                        process_ids.update(stax_server=owned["server_pid"], stax_recorder=owned["recorder_pid"])
                    trial = {"pair": pair, "mode": mode, **observe(path, args.duration, process_ids)}
                    if capture:
                        archive, metadata = capture.result(timeout=args.duration + 70)
                        scope = metadata["run"]
                        trial["measurement_inside_capture"] = (
                            scope["started_at_unix_ns"] <= trial["started_at_unix_ns"]
                            and trial["ended_at_unix_ns"] <= scope["stopped_at_unix_ns"]
                        )
                        run = store_stax_result(
                            backend,
                            archive,
                            "redis-server",
                            {"test_name": "stax-redis-pilot", "pair": str(pair)},
                            metadata,
                        )
                        trial.update(
                            run_id=run.run_id,
                            profile=query_stax_result(backend, run.run_id),
                            hotspots=get_stax_hotspots(backend, run.run_id, limit=10),
                        )
                        observed = trial["profile"]["result"]["clock"]
                        trial["measurement_inside_observed_clock"] = (
                            observed["session_start_ns"] is not None
                            and observed["last_event_ns"] is not None
                            and observed["session_start_ns"] <= trial["started_at_monotonic_ns"]
                            and trial["ended_at_monotonic_ns"] <= observed["last_event_ns"]
                        )
                    else:
                        time.sleep(1)
                    evidence["trials"].append(trial)
                    evidence["summary"] = summarize(evidence["trials"], budget)
                    (output / "pilot.json").write_text(json.dumps(evidence, indent=2) + "\n")
                    print(
                        json.dumps(
                            {
                                "pair": pair,
                                "mode": mode,
                                "rate": trial["commands_per_second"],
                                "inside_capture": trial.get("measurement_inside_capture"),
                                "summary": evidence["summary"],
                            }
                        ),
                        flush=True,
                    )
            evidence["status"] = "completed"
        finally:
            for process in processes:
                _finish_target(process)
            _finish_target(server)
            backend.close()
            (output / "pilot.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-server", required=True, type=Path)
    parser.add_argument("--redis-benchmark", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frequency", type=int, default=99)
    parser.add_argument("--duration", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--pipeline", type=int, default=16)
    options = parser.parse_args()
    if options.duration < 1 or options.repeats < 1 or options.pipeline < 1 or not 1 <= options.frequency <= 10000:
        parser.error("duration, repeats and pipeline must be positive; frequency must be 1–10000")
    run(options)
