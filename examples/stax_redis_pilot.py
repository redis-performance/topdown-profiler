"""Run a reproducible, isolated Redis/Stax sampling pilot on macOS.

Stax is an external tool. This script starts its own unprivileged server and
Redis instance; the macOS staxd helper must already be running on a private socket.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
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
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        for suffix, value in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            if isinstance(value, bytes):
                value = value.decode(errors="replace")
            (output / f"{name}.{suffix}").write_text(value or "")
        raise RuntimeError(f"{name} timed out after {timeout}s; logs: {output}") from exc
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


def comparison_metrics(stdout: str, archives: list[str]) -> dict[str, int]:
    try:
        report = json.loads(stdout)
        if (report["baseline"]["path"] != archives[0]
                or report["candidate"]["path"] != archives[1]):
            raise ValueError("Stax compared different archives than requested")
        counts = report["metrics"]["pet_samples"]
        baseline, candidate = counts["baseline"], counts["candidate"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Stax compare JSON lacks the pinned archive metrics") from exc
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (baseline, candidate)):
        raise ValueError("Stax comparison has invalid sample-marker counts")
    return {"baseline_sample_markers": baseline, "candidate_sample_markers": candidate}


def archive_run(path: Path, expected_pid: int) -> dict[str, int]:
    try:
        with path.open() as source:
            document = json.load(source)
        version = document["format_version"]
        runs = document["runs"]
        if version != 3 or not isinstance(runs, list) or len(runs) != 1:
            raise ValueError("Stax archive does not have the pinned v3 single-run layout")
        run = runs[0]
        if run["target_pid"] != expected_pid:
            raise ValueError("Stax archive target PID differs from the profiled Redis")
        pet_samples = run["pet_samples"]
        off_cpu_intervals = run["off_cpu_intervals"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Stax archive has invalid run metadata: {path}") from exc
    if (any(isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (pet_samples, off_cpu_intervals)) or pet_samples == 0):
        raise ValueError("Stax archive has no positive CPU PET sample count")
    return {"format_version": version, "cpu_pet_samples": pet_samples,
            "off_cpu_intervals": off_cpu_intervals}


def memtier_metrics(path: Path) -> dict:
    document = json.loads(path.read_text())
    stats = document["ALL STATS"]
    duration_ms = stats["Runtime"]["Total duration"]
    count = stats["Totals"]["Count"]
    reported = stats["Totals"]["Ops/sec"]
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value <= 0 for value in (duration_ms, count, reported)):
        raise ValueError("memtier did not report positive duration, work, and throughput")
    computed = count * 1000 / duration_ms
    if abs(reported / computed - 1) > 0.05:
        raise ValueError("memtier JSON throughput disagrees with count/duration")
    gets = stats["Gets"]
    if (not all(isinstance(gets[key], (int, float)) and not isinstance(gets[key], bool)
                and math.isfinite(gets[key]) for key in ("Ops/sec", "Misses/sec"))
            or gets["Ops/sec"] <= 0 or gets["Misses/sec"] < 0):
        raise ValueError("memtier GET metrics are unavailable")
    miss_fraction = gets["Misses/sec"] / gets["Ops/sec"]
    if miss_fraction > 1:
        raise ValueError("memtier GET miss fraction exceeds one")
    percentiles = stats["Totals"]["Percentile Latencies"]
    if any(not isinstance(percentiles[key], (int, float)) or isinstance(percentiles[key], bool)
           or not math.isfinite(percentiles[key]) or percentiles[key] < 0 for key in ("p50.00", "p99.00")):
        raise ValueError("memtier latency percentiles are unavailable")
    return {
        "count": count,
        "duration_seconds": duration_ms / 1000,
        "operations_per_second": computed,
        "reported_operations_per_second": reported,
        "p50_latency_ms": percentiles["p50.00"],
        "p99_latency_ms": percentiles["p99.00"],
        "get_miss_fraction": miss_fraction,
    }


def prefill_redis(redis_cli: Path, redis_socket: Path, output: Path, key_count: int) -> None:
    command = [str(redis_cli), "-s", str(redis_socket), "--pipe"]
    value = b"x" * 64
    frames = bytearray()
    for index in range(1, key_count + 1):
        key = f"stax:{index}".encode()
        frames.extend(b"*3\r\n$3\r\nSET\r\n")
        for argument in (key, value):
            frames.extend(f"${len(argument)}\r\n".encode())
            frames.extend(argument + b"\r\n")
    (output / "prefill.command.json").write_text(json.dumps(command, indent=2) + "\n")
    try:
        result = subprocess.run(command, input=bytes(frames), capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        (output / "prefill.stdout").write_bytes(exc.stdout or b"")
        (output / "prefill.stderr").write_bytes(exc.stderr or b"")
        raise RuntimeError(f"Redis prefill timed out; logs: {output}") from exc
    (output / "prefill.stdout").write_bytes(result.stdout)
    (output / "prefill.stderr").write_bytes(result.stderr)
    if result.returncode or b"errors: 0" not in result.stdout:
        raise RuntimeError(f"Redis prefill failed; logs: {output}")
    size = run_command("prefill-dbsize", [str(redis_cli), "-s", str(redis_socket), "dbsize"], output)
    if size.stdout.strip() != str(key_count):
        raise RuntimeError(f"Redis prefill has {size.stdout.strip()} keys, expected {key_count}")


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
            result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        except OSError as exc:
            return f"unavailable: {exc}"
        except subprocess.TimeoutExpired:
            return "unavailable: timed out"
        return result.stdout.strip() if result.returncode == 0 else f"unavailable: {result.stderr.strip()}"

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": value(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "os_build": value(["sw_vers", "-buildVersion"]),
        "kernel_uuid": value(["sysctl", "-n", "kern.uuid"]),
        "xcode": value(["xcodebuild", "-version"]),
    }


def validate_helper_socket(path: Path) -> dict[str, int | str]:
    if not path.is_absolute():
        raise RuntimeError(f"macOS Stax helper socket path must be absolute: {path}")
    try:
        directory = path.parent.lstat()
        endpoint = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"macOS Stax helper socket is absent: {path}") from exc
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or directory.st_mode & 0o077):
        raise RuntimeError(f"macOS Stax helper socket directory must be owned by this user and private: {path.parent}")
    if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != 0:
        raise RuntimeError(f"macOS Stax helper socket must be a root-owned Unix socket: {path}")
    return {"path": str(path), "directory_mode": stat.S_IMODE(directory.st_mode),
            "directory_owner_uid": directory.st_uid, "socket_owner_uid": endpoint.st_uid}


def require_free_space(path: Path, minimum_gib: float) -> int:
    free = shutil.disk_usage(path).free
    if free < minimum_gib * 1024 ** 3:
        raise RuntimeError(f"free disk space is {free / 1024 ** 3:.2f} GiB below the "
                           f"{minimum_gib:.2f} GiB pilot reserve; output retained at {path}")
    return free


def schedule(pairs: int) -> list[bool]:
    if pairs < 1:
        raise ValueError("pairs must be positive")
    return [mode for _ in range(pairs) for mode in (False, True)] if pairs == 1 else (
        [False, True, True, False] * (pairs // 2) + ([False, True] if pairs % 2 else [])
    )


def matched_pairs(rounds: list[dict]) -> list[tuple[dict, dict, str]]:
    if len(rounds) % 2:
        raise ValueError("paired summary requires an even number of rounds")
    pairs = []
    for first, second in zip(rounds[::2], rounds[1::2]):
        if {first["mode"], second["mode"]} != {"baseline", "stax"}:
            raise ValueError("each pair must contain one baseline and one Stax round")
        baseline = first if first["mode"] == "baseline" else second
        profiled = second if first["mode"] == "baseline" else first
        pairs.append((baseline, profiled, f'{first["mode"]}/{second["mode"]}'))
    return pairs


def percent_change(baseline: float, candidate: float) -> float | None:
    return (candidate / baseline - 1) * 100 if baseline > 0 else None


def summarize(rounds: list[dict], workload: str, requests: int) -> dict:
    def distribution(values: list[float]) -> dict[str, float]:
        return {"median": statistics.median(values), "min": min(values), "max": max(values)}

    def rss() -> dict[str, dict[str, dict[str, float]]]:
        return {
            mode: {name: distribution(values) for name in ("redis", "stax_server", "stax_record")
                   if (values := [row["observed_peak_rss_kb"][name] for row in rounds
                                  if row["mode"] == mode and name in row["observed_peak_rss_kb"]])}
            for mode in ("baseline", "stax")
        }

    if workload == "memtier":
        paired = []
        for index, (baseline_row, stax_row, order) in enumerate(matched_pairs(rounds), start=1):
            baseline_metrics = baseline_row["memtier"]
            stax_metrics = stax_row["memtier"]
            paired.append({
                "pair": index,
                "order": order,
                "throughput_change_percent": percent_change(
                    baseline_metrics["operations_per_second"], stax_metrics["operations_per_second"]
                ),
                "p50_latency_change_percent": percent_change(
                    baseline_metrics["p50_latency_ms"], stax_metrics["p50_latency_ms"]
                ),
                "p99_latency_change_percent": percent_change(
                    baseline_metrics["p99_latency_ms"], stax_metrics["p99_latency_ms"]
                ),
            })
        rates = {mode: [row["memtier"]["operations_per_second"] for row in rounds if row["mode"] == mode]
                 for mode in ("baseline", "stax")}
        baseline = statistics.median(rates["baseline"])
        sampled = statistics.median(rates["stax"])
        latency = {
            percentile: {
                "baseline_median": statistics.median(row["memtier"][f"{percentile}_latency_ms"]
                                                     for row in rounds if row["mode"] == "baseline"),
                "stax_median": statistics.median(row["memtier"][f"{percentile}_latency_ms"]
                                                 for row in rounds if row["mode"] == "stax"),
            }
            for percentile in ("p50", "p99")
        }
        for values in latency.values():
            values["change_percent"] = ((values["stax_median"] / values["baseline_median"] - 1) * 100
                                        if values["baseline_median"] > 0 else None)
        p99_changes = [pair["p99_latency_change_percent"] for pair in paired
                       if pair["p99_latency_change_percent"] is not None]
        return {
            "workload": "memtier",
            "baseline_median_operations_per_second": baseline,
            "stax_median_operations_per_second": sampled,
            "observed_throughput_change_percent": (sampled / baseline - 1) * 100,
            "paired_changes_percent": paired,
            "paired_median_throughput_change_percent": statistics.median(
                pair["throughput_change_percent"] for pair in paired
            ),
            "paired_median_p99_latency_change_percent": (
                statistics.median(p99_changes) if p99_changes else None
            ),
            "throughput_operations_per_second": {mode: distribution(values) for mode, values in rates.items()},
            "latency_ms": latency,
            "observed_peak_rss_kb": rss(),
            "scope": "fixed-duration mixed SET/GET on one Redis process; includes client effects",
        }
    paired = [
        {"pair": index, "order": order,
         "wall_time_change_percent": (profiled["elapsed_seconds"] / baseline["elapsed_seconds"] - 1) * 100}
        for index, (baseline, profiled, order) in enumerate(matched_pairs(rounds), start=1)
    ]
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
        "paired_changes_percent": paired,
        "paired_median_wall_time_change_percent": statistics.median(
            pair["wall_time_change_percent"] for pair in paired
        ),
        "elapsed_seconds": {mode: distribution(values) for mode, values in elapsed.items()},
        "observed_peak_rss_kb": rss(),
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
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    initial_free = require_free_space(output, args.min_free_gib)
    names = ["stax", "stax_server", "redis_server", "redis_cli"]
    names.append("memtier" if args.workload == "memtier" else "redis_benchmark")
    paths = {name: executable(getattr(args, name)) for name in names}

    def version(name: str, path: Path) -> str:
        if name == "stax_server":
            return "unavailable: long-running service has no version command"
        try:
            result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=10, check=False)
        except subprocess.TimeoutExpired:
            return "unavailable: version probe timed out"
        if result.returncode:
            return f"unavailable: exit {result.returncode}"
        return (result.stdout or result.stderr).strip()

    metadata = {
        "stax_revision": args.stax_revision,
        "redis_revision": args.redis_revision,
        "stax_build_flags": args.stax_build_flags,
        "redis_build_flags": args.redis_build_flags,
        "stax_experimental_apple_pmu": os.environ.get("STAX_EXPERIMENTAL_APPLE_PMU") == "1",
        "memtier_revision": args.memtier_revision if args.workload == "memtier" else None,
        "disk_free_bytes_start": initial_free,
        "min_free_gib": args.min_free_gib,
        "host": read_host(),
        "executables": {name: {"path": str(path), "sha256": sha256(path), "version": version(name, path)}
                        for name, path in paths.items()},
        "settings": {"workload": args.workload,
                     "requests_per_command": args.requests if args.workload == "redis-benchmark" else None,
                     "warmup_requests": args.warmup_requests if args.workload == "redis-benchmark" else None,
                     "clients": args.clients if args.workload == "redis-benchmark" else None,
                     "pipeline": args.pipeline if args.workload == "redis-benchmark" else None,
                     "pairs": args.pairs, "record_frequency_hz": args.frequency,
                     "annotate_symbol": args.annotate_symbol,
                     "memtier_seconds": args.memtier_seconds if args.workload == "memtier" else None,
                     "memtier_warmup_seconds": args.memtier_warmup_seconds if args.workload == "memtier" else None,
                     "memtier_threads": 2, "memtier_clients_per_thread": 10,
                     "memtier_pipeline": 4, "memtier_key_count": 10_000,
                     "memtier_data_size": 64, "memtier_ratio": "1:1"},
        "rounds": [],
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    daemon_socket = Path(args.daemon_socket)
    if platform.system() == "Darwin":
        try:
            metadata["helper_socket"] = validate_helper_socket(daemon_socket)
        except RuntimeError as exc:
            metadata.update(preflight_status="failed", preflight_error=str(exc))
            (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
            raise
        metadata["preflight_status"] = "passed"
        (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    redis = server = recorder = None
    with tempfile.TemporaryDirectory(prefix="td-stax-m1-") as runtime:
        runtime_path = Path(runtime)
        redis_socket = runtime_path / "redis.sock"
        with socket.socket() as probe_socket:
            probe_socket.bind(("127.0.0.1", 0))
            redis_port = probe_socket.getsockname()[1]
        env = dict(os.environ, STAX_SERVER_WS_BIND="127.0.0.1:0")
        try:
            redis_cmd = [str(paths["redis_server"]), "--bind", "127.0.0.1", "--port", str(redis_port),
                         "--io-threads", "1", "--unixsocket", str(redis_socket),
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
                require_free_space(output, args.min_free_gib)
                label = f"round-{index:02d}"
                round_output = output / label
                round_output.mkdir()
                run_command("flush", [str(paths["redis_cli"]), "-s", str(redis_socket), "flushall"], round_output)

                def workload_command(measured: bool) -> list[str]:
                    if args.workload == "memtier":
                        seconds = args.memtier_seconds if measured else args.memtier_warmup_seconds
                        result_file = round_output / ("benchmark.json" if measured else "warmup.json")
                        return [str(paths["memtier"]), "--server=127.0.0.1", f"--port={redis_port}",
                                "--threads=2", "--clients=10", "--pipeline=4", "--ratio=1:1",
                                "--data-size=64", "--key-minimum=1", "--key-maximum=10000",
                                "--key-pattern=R:R", "--key-prefix=stax:", f"--test-time={seconds}",
                                "--hide-histogram", "--print-percentiles=50,99",
                                f"--json-out-file={result_file}"]
                    requests = args.requests if measured else args.warmup_requests
                    return [str(paths["redis_benchmark"]), "-s", str(redis_socket), "-t", "set,get",
                            "-n", str(requests), "-c", str(args.clients), "-P", str(args.pipeline), "--csv"]

                if args.workload == "memtier":
                    prefill_redis(paths["redis_cli"], redis_socket, round_output, 10_000)
                run_command("warmup", workload_command(False), round_output, timeout=120)
                recorder = None
                if profiled:
                    stax_socket = runtime_path / f"stax-{index}.sock"
                    env["STAX_SERVER_SOCKET"] = str(stax_socket)
                    with (round_output / "stax-server.log").open("w") as log:
                        server = subprocess.Popen([str(paths["stax_server"])],
                                                  stdout=log, stderr=subprocess.STDOUT, env=env)
                    wait_for(lambda: stax_socket.exists(), server, 20, "private stax-server")
                    run_command("stax-status-initial", [str(paths["stax"]), "status"], round_output, env=env)
                    record_cmd = [str(paths["stax"]), "record", "--pid", str(redis.pid),
                                  "--frequency", str(args.frequency), "--time-limit",
                                  "45" if args.workload == "memtier" else "120",
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
                    benchmark = run_command("benchmark", workload_command(True), round_output,
                                            timeout=45 if args.workload == "memtier" else 110)
                    elapsed = (time.monotonic_ns() - start) / 1e9
                finally:
                    rss_stop.set()
                    rss_thread.join(timeout=3)
                    stop_owned(recorder, interrupt=True)
                if recorder is not None and recorder.returncode:
                    raise RuntimeError(f"Stax recording failed; logs: {round_output}")
                row = {"mode": "stax" if profiled else "baseline", "elapsed_seconds": elapsed,
                       "observed_peak_rss_kb": rss_maxima, "output": str(round_output)}
                if args.workload == "memtier":
                    row["memtier"] = memtier_metrics(round_output / "benchmark.json")
                else:
                    row["aggregate_requests_per_second"] = 2 * args.requests / elapsed
                    row["rates_per_second"] = benchmark_rates(benchmark.stdout)
                    row["latency_ms"] = benchmark_latency_ms(benchmark.stdout)
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
                    if args.annotate_symbol:
                        try:
                            run_command("annotate", [str(paths["stax"]), "annotate", args.annotate_symbol],
                                        round_output, env=env)
                            views["annotate"] = ["annotate", args.annotate_symbol]
                        except RuntimeError as exc:
                            row["annotate_error"] = str(exc)
                    archive = round_output / "redis.stax"
                    require_free_space(output, args.min_free_gib)
                    run_command("save", [str(paths["stax"]), "save", str(archive)], round_output, env=env)
                    row["archive"] = str(archive)
                    row["archive_sha256"] = sha256(archive)
                    row["archive_run"] = archive_run(archive, redis.pid)
                    run_command("open", [str(paths["stax"]), "open", str(archive)], round_output, env=env)
                    row["round_trip_equal"] = {}
                    for name, command in views.items():
                        restored = run_command(f"restored-{name}", [str(paths["stax"]), *command],
                                               round_output, env=env)
                        row["round_trip_equal"][name] = restored.stdout == (
                            round_output / f"{name}.stdout").read_text()
                    stop_owned(server)
                    server = None
                row["disk_free_bytes_after"] = shutil.disk_usage(output).free
                metadata["rounds"].append(row)
                (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

            archives = [row["archive"] for row in metadata["rounds"] if row["mode"] == "stax"]
            if len(archives) >= 2:
                comparison = run_command("compare", [str(paths["stax"]), "compare", "--json", *archives[:2]],
                                         output, env=env)
                metadata["comparison"] = comparison_metrics(comparison.stdout, archives)
            metadata["summary"] = summarize(metadata["rounds"], args.workload, args.requests)
            if args.workload == "memtier":
                metadata["summary"]["valid_get_miss_fraction"] = all(
                    row["memtier"]["get_miss_fraction"] <= 0.01 for row in metadata["rounds"])
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
    parser.add_argument("--workload", choices=("redis-benchmark", "memtier"), default="redis-benchmark")
    parser.add_argument("--memtier-revision", help="upstream Git commit of the memtier binary")
    parser.add_argument("--stax-build-flags", default="unknown")
    parser.add_argument("--redis-build-flags", default="unknown")
    parser.add_argument("--stax", default="stax")
    parser.add_argument("--stax-server", default="stax-server", dest="stax_server")
    parser.add_argument("--redis-server", default="redis-server", dest="redis_server")
    parser.add_argument("--redis-benchmark", default="redis-benchmark", dest="redis_benchmark")
    parser.add_argument("--memtier", default="memtier_benchmark")
    parser.add_argument("--redis-cli", default="redis-cli", dest="redis_cli")
    parser.add_argument("--daemon-socket", required=True, help="socket of the reviewed, private Stax helper")
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--requests", type=int, default=30_000_000)
    parser.add_argument("--warmup-requests", type=int, default=1_000_000)
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--pipeline", type=int, default=16)
    parser.add_argument("--frequency", type=int, default=900)
    parser.add_argument("--min-free-gib", type=float, default=1.5,
                        help="stop before a round or archive save if disk free space is below this reserve")
    parser.add_argument("--annotate-symbol", default="processCommand")
    parser.add_argument("--memtier-seconds", type=int, default=30)
    parser.add_argument("--memtier-warmup-seconds", type=int, default=5)
    args = parser.parse_args()
    if any(getattr(args, key) <= 0 for key in (
        "pairs", "requests", "warmup_requests", "clients", "pipeline", "frequency",
        "memtier_seconds", "memtier_warmup_seconds"
    )):
        parser.error("numeric settings must be positive")
    if args.workload == "memtier" and not args.memtier_revision:
        parser.error("--memtier-revision is required for the memtier workload")
    if not math.isfinite(args.min_free_gib) or args.min_free_gib <= 0:
        parser.error("--min-free-gib must be a positive finite number")
    pilot(args)


if __name__ == "__main__":
    main()
