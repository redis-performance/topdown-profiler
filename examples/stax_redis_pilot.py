"""Run a reproducible, isolated Redis/Stax sampling pilot on macOS.

Stax is an external tool. This script starts its own unprivileged server and
Redis instance; the macOS staxd helper must already be installed by the user.
"""

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import stat
import statistics
import subprocess
import tempfile
import threading
import time


def executable(value: str) -> Path:
    resolved = shutil.which(value)
    if resolved is None:
        raise ValueError(f"executable not found: {value}")
    return Path(resolved).resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_command(name: str, command: list[str], output: Path, *, env: dict[str, str] | None = None,
                timeout: float = 60) -> subprocess.CompletedProcess[str]:
    (output / f"{name}.command.json").write_text(json.dumps(command, indent=2) + "\n")
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout, check=False)
    (output / f"{name}.stdout").write_text(result.stdout)
    (output / f"{name}.stderr").write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(f"{name} exited {result.returncode}; logs: {output}")
    return result


def benchmark_rates(stdout: str) -> dict[str, float]:
    rows = list(csv.reader(io.StringIO(stdout)))
    rates = {}
    for row in rows:
        if len(row) >= 2 and row[0] in {"SET", "GET"}:
            rates[row[0]] = float(row[1])
    if set(rates) != {"SET", "GET"} or any(rate <= 0 for rate in rates.values()):
        raise ValueError("Redis benchmark did not report positive SET and GET rates")
    return rates


def benchmark_latency_ms(stdout: str) -> dict[str, dict[str, float]]:
    reader = csv.DictReader(io.StringIO(stdout))
    if not reader.fieldnames or not {"test", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms"}.issubset(
        reader.fieldnames
    ):
        return {}
    return {
        row["test"]: {percentile: float(row[f"{percentile}_latency_ms"]) for percentile in ("p50", "p95", "p99")}
        for row in reader if row.get("test") in {"SET", "GET"}
    }


def wait_for(predicate, process: subprocess.Popen | None, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"{label} exited early with {process.returncode}")
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {label}")


def stop_owned(process: subprocess.Popen | None, *, interrupt: bool = False) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT if interrupt else signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def read_host() -> dict[str, str]:
    def value(command: list[str]) -> str:
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            return f"unavailable: {exc}"
        return result.stdout.strip() if result.returncode == 0 else f"unavailable: {result.stderr.strip()}"

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": value(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "os_build": value(["sw_vers", "-buildVersion"]),
        "kernel_uuid": value(["sysctl", "-n", "kern.uuid"]),
        "xcode": value(["xcodebuild", "-version"]),
    }


def schedule(pairs: int) -> list[bool]:
    if pairs < 1:
        raise ValueError("pairs must be positive")
    return [mode for _ in range(pairs) for mode in (False, True)] if pairs == 1 else (
        [False, True, True, False] * (pairs // 2) + ([False, True] if pairs % 2 else [])
    )


def summarize(rounds: list[dict], requests: int) -> dict:
    elapsed = {"baseline": [], "stax": []}
    for row in rounds:
        elapsed[row["mode"]].append(row["elapsed_seconds"])
    baseline = statistics.median(elapsed["baseline"])
    sampled = statistics.median(elapsed["stax"])
    return {
        "requests_per_command": requests,
        "benchmark_commands": ["SET", "GET"],
        "baseline_median_seconds": baseline,
        "stax_median_seconds": sampled,
        "observed_wall_time_change_percent": (sampled / baseline - 1) * 100,
        "scope": "same Redis process, fixed request count; includes benchmark-client effects",
    }


def monitor_rss(processes: dict[str, subprocess.Popen], stop: threading.Event,
                maxima: dict[str, int]) -> None:
    names = {process.pid: name for name, process in processes.items() if process is not None}
    if not names:
        return
    while not stop.is_set():
        result = subprocess.run(["ps", "-o", "pid=,rss=", "-p", ",".join(map(str, names))],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    name = names.get(int(parts[0]))
                    if name:
                        maxima[name] = max(maxima.get(name, 0), int(parts[1]))
        stop.wait(1)


def pilot(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    paths = {name: executable(getattr(args, name)) for name in (
        "stax", "stax_server", "redis_server", "redis_benchmark", "redis_cli")}

    def version(path: Path) -> str:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode:
            return f"unavailable: exit {result.returncode}"
        return (result.stdout or result.stderr).strip()

    metadata = {
        "stax_revision": args.stax_revision,
        "redis_revision": args.redis_revision,
        "stax_build_flags": args.stax_build_flags,
        "redis_build_flags": args.redis_build_flags,
        "host": read_host(),
        "executables": {name: {"path": str(path), "sha256": sha256(path), "version": version(path)}
                        for name, path in paths.items()},
        "settings": {"requests_per_command": args.requests, "warmup_requests": args.warmup_requests,
                     "clients": args.clients, "pipeline": args.pipeline, "pairs": args.pairs,
                     "record_frequency_hz": args.frequency},
        "rounds": [],
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    daemon_socket = Path(args.daemon_socket)
    if platform.system() == "Darwin" and not (
        daemon_socket.exists() and stat.S_ISSOCK(daemon_socket.stat().st_mode)
    ):
        raise RuntimeError(f"macOS Stax helper socket is absent: {daemon_socket}; logs: {output}")

    redis = server = recorder = None
    with tempfile.TemporaryDirectory(prefix="td-stax-m1-") as runtime:
        runtime_path = Path(runtime)
        redis_socket = runtime_path / "redis.sock"
        stax_socket = runtime_path / "stax.sock"
        env = dict(os.environ, STAX_SERVER_SOCKET=str(stax_socket), STAX_SERVER_WS_BIND="127.0.0.1:0")
        try:
            with (output / "stax-server.log").open("w") as log:
                server = subprocess.Popen([str(paths["stax_server"])], stdout=log, stderr=subprocess.STDOUT, env=env)
            wait_for(lambda: stax_socket.exists(), server, 20, "private stax-server")
            run_command("stax-status-initial", [str(paths["stax"]), "status"], output, env=env)

            redis_cmd = [str(paths["redis_server"]), "--port", "0", "--unixsocket", str(redis_socket),
                         "--unixsocketperm", "700", "--save", "", "--appendonly", "no",
                         "--dir", str(runtime_path)]
            (output / "redis-server.command.json").write_text(json.dumps(redis_cmd, indent=2) + "\n")
            with (output / "redis-server.log").open("w") as log:
                redis = subprocess.Popen(redis_cmd, stdout=log, stderr=subprocess.STDOUT)

            def redis_ready() -> bool:
                if not redis_socket.exists():
                    return False
                result = subprocess.run([str(paths["redis_cli"]), "-s", str(redis_socket), "ping"],
                                        capture_output=True, text=True, check=False)
                return result.returncode == 0 and result.stdout.strip() == "PONG"

            wait_for(redis_ready, redis, 20, "private Redis")
            for index, profiled in enumerate(schedule(args.pairs), start=1):
                label = f"round-{index:02d}"
                round_output = output / label
                round_output.mkdir()
                run_command("flush", [str(paths["redis_cli"]), "-s", str(redis_socket), "flushall"], round_output)

                def benchmark_command(requests: int) -> list[str]:
                    return [str(paths["redis_benchmark"]), "-s", str(redis_socket), "-t", "set,get",
                            "-n", str(requests), "-c", str(args.clients), "-P", str(args.pipeline), "--csv"]

                run_command("warmup", benchmark_command(args.warmup_requests), round_output, timeout=120)
                recorder = None
                if profiled:
                    record_cmd = [str(paths["stax"]), "record", "--pid", str(redis.pid),
                                  "--frequency", str(args.frequency), "--time-limit", "120",
                                  "--daemon-socket", str(daemon_socket)]
                    (round_output / "record.command.json").write_text(json.dumps(record_cmd, indent=2) + "\n")
                    with (round_output / "record.stdout").open("w") as stdout, (
                        round_output / "record.stderr").open("w") as stderr:
                        recorder = subprocess.Popen(record_cmd, stdout=stdout, stderr=stderr, env=env)
                    wait_for(lambda: "stax: started run " in (round_output / "record.stderr").read_text(),
                             recorder, 20, "Stax recording")
                time.sleep(0.5)
                try:
                    rss_stop = threading.Event()
                    rss_maxima: dict[str, int] = {}
                    rss_thread = threading.Thread(target=monitor_rss,
                                                  args=({"redis": redis, "stax_server": server,
                                                         "stax_record": recorder}, rss_stop, rss_maxima),
                                                  daemon=True)
                    rss_thread.start()
                    start = time.monotonic_ns()
                    benchmark = run_command("benchmark", benchmark_command(args.requests), round_output,
                                            timeout=110)
                    elapsed = (time.monotonic_ns() - start) / 1e9
                finally:
                    rss_stop.set()
                    rss_thread.join(timeout=3)
                    stop_owned(recorder, interrupt=True)
                if recorder is not None and recorder.returncode:
                    raise RuntimeError(f"Stax recording failed; logs: {round_output}")
                row = {"mode": "stax" if profiled else "baseline", "elapsed_seconds": elapsed,
                       "aggregate_requests_per_second": 2 * args.requests / elapsed,
                       "rates_per_second": benchmark_rates(benchmark.stdout),
                       "latency_ms": benchmark_latency_ms(benchmark.stdout),
                       "observed_peak_rss_kb": rss_maxima, "output": str(round_output)}
                if profiled:
                    views = {
                        "threads": ["threads", "-n", "0"],
                        "top-self": ["top", "-n", "20", "--sort", "self"],
                        "top-total": ["top", "-n", "20", "--sort", "total"],
                        "flame": ["flame", "-d", "6"],
                        "diagnose": ["diagnose"],
                    }
                    for name, command in views.items():
                        run_command(name, [str(paths["stax"]), *command], round_output, env=env)
                    archive = round_output / "redis.stax"
                    run_command("save", [str(paths["stax"]), "save", str(archive)], round_output, env=env)
                    row["archive"] = str(archive)
                    row["archive_sha256"] = sha256(archive)
                    run_command("open", [str(paths["stax"]), "open", str(archive)], round_output, env=env)
                    row["round_trip_equal"] = {}
                    for name, command in views.items():
                        restored = run_command(f"restored-{name}", [str(paths["stax"]), *command],
                                               round_output, env=env)
                        row["round_trip_equal"][name] = restored.stdout == (
                            round_output / f"{name}.stdout").read_text()
                metadata["rounds"].append(row)
                (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

            archives = [row["archive"] for row in metadata["rounds"] if row["mode"] == "stax"]
            if len(archives) >= 2:
                run_command("compare", [str(paths["stax"]), "compare", "--json", *archives[:2]],
                            output, env=env)
            metadata["summary"] = summarize(metadata["rounds"], args.requests)
            (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        finally:
            stop_owned(recorder, interrupt=True)
            if redis is not None and redis.poll() is None:
                subprocess.run([str(paths["redis_cli"]), "-s", str(redis_socket), "shutdown", "nosave"],
                               capture_output=True, timeout=5, check=False)
            stop_owned(redis)
            stop_owned(server)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stax-revision", required=True, help="full upstream Git commit of the Stax binaries")
    parser.add_argument("--redis-revision", required=True, help="full upstream Git commit of the Redis binaries")
    parser.add_argument("--stax-build-flags", default="unknown")
    parser.add_argument("--redis-build-flags", default="unknown")
    parser.add_argument("--stax", default="stax")
    parser.add_argument("--stax-server", default="stax-server", dest="stax_server")
    parser.add_argument("--redis-server", default="redis-server", dest="redis_server")
    parser.add_argument("--redis-benchmark", default="redis-benchmark", dest="redis_benchmark")
    parser.add_argument("--redis-cli", default="redis-cli", dest="redis_cli")
    parser.add_argument("--daemon-socket", default="/var/run/staxd.sock")
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--requests", type=int, default=30_000_000)
    parser.add_argument("--warmup-requests", type=int, default=1_000_000)
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--pipeline", type=int, default=16)
    parser.add_argument("--frequency", type=int, default=900)
    args = parser.parse_args()
    if any(getattr(args, key) <= 0 for key in (
        "pairs", "requests", "warmup_requests", "clients", "pipeline", "frequency"
    )):
        parser.error("numeric settings must be positive")
    pilot(args)


if __name__ == "__main__":
    main()
