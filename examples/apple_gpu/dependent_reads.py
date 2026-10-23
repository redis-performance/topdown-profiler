"""Compare equal logical GPU dependent-read counts across table sizes and parallelism."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from topdown.collector.apple import AppleProfileError  # noqa: E402
from topdown.collector.apple_gpu import _require_host, collect  # noqa: E402

CONDITIONS = ((24, 64), (24, 4096), (24, 262144), (14, 262144), (14, 4096), (14, 64))


def _run(output, name, command, timeout, env=None):
    (output / f"{name}.command.json").write_text(json.dumps(command) + "\n")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired as exc:
        (output / f"{name}.stdout").write_bytes(exc.stdout or b"")
        (output / f"{name}.stderr").write_bytes(exc.stderr or b"")
        raise ValueError(f"{name} timed out; artifacts: {output}") from exc
    (output / f"{name}.stdout").write_text(result.stdout)
    (output / f"{name}.stderr").write_text(result.stderr)
    if result.returncode:
        raise ValueError(f"{name} failed: {result.stderr}; artifacts: {output}")


def experiment(output):
    _require_host()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).with_name("dependent_reads_probe.swift")
    binary = output / "probe"
    _run(output, "compile", ["xcrun", "swiftc", "-O", "-framework", "Metal", str(source), "-o", str(binary)], 90)
    order = CONDITIONS + tuple(reversed(CONDITIONS))
    evidence = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "order": order, "cases": [],
                "limitations": ["Logical reads do not establish DRAM traffic or cache-line transactions.",
                                "Table size bounds the address range; unique access coverage is not calibrated.",
                                "Changing chains also changes loop length, launch count and access overlap.",
                                "Original command and replay timings describe separate executions.",
                                "Frequency and background activity are uncontrolled.",
                                "Timeline counters can include other device activity; missing limiters remain unavailable."]}
    for trial, (bits, threads) in enumerate(order):
        case = output / f"trial-{trial:02d}-bits-{bits}-threads-{threads}"
        case.mkdir()
        _run(case, "capture", [str(binary), str(bits), str(threads), str(case)], 90,
             {**os.environ, "MTL_CAPTURE_ENABLED": "1"})
        from topdown.collector.apple_files import decode_json_object, read_regular_bytes

        probe = decode_json_object(read_regular_bytes(case / "probe.json"))
        if (probe.get("checksum_verified") is not True or probe.get("elements_verified") is not True
                or probe.get("table_bits") != bits or probe.get("threads") != threads
                or probe.get("dependent_reads") != 16777216 or type(probe.get("steps")) is not int
                or probe["steps"] * threads != 16777216):
            raise ValueError(f"Dependent-read checksum/configuration verification failed; artifacts: {case}")
        profile = collect(case / "capture.gputrace", case / "replay")
        evidence["cases"].append({"trial": trial, "probe": probe, "profile": profile})
        (output / "results.json").write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n")
        print(f"{1 << bits:>8} uint table entries, {threads:>6} chains: command {probe['gpu_seconds'] * 1000:.4f} ms; "
              f"replay {profile['timeline']['GPU time']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for native captures and results")
    args = parser.parse_args()
    try:
        experiment(args.output)
    except KeyboardInterrupt:
        parser.exit(130, f"Dependent-read experiment cancelled; artifacts retained at {args.output}\n")
    except (OSError, ValueError, AppleProfileError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
