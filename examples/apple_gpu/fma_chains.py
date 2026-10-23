"""Compare equal logical GPU FMA counts across dependency chains and thread counts."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from topdown.collector.apple import AppleProfileError  # noqa: E402
from topdown.collector.apple_gpu import _require_host, collect  # noqa: E402

CONDITIONS = ((64, 1), (64, 4), (64, 16), (4096, 16), (4096, 4), (4096, 1))
TOTAL_FMAS = 268435456


def _verify_probe(probe, threads, chains):
    expected = {"threads": threads, "chains_per_thread": chains, "elements": threads * chains,
                "steps": TOTAL_FMAS // (threads * chains), "logical_fmas": TOTAL_FMAS,
                "input_output_bytes": threads * chains * 8, "multiplier_bits": 0x3f800001}
    addends = [struct.unpack("<I", struct.pack("<f", (index + 1) * 2**-24))[0] for index in range(chains)]
    if (not isinstance(probe, dict)
            or any(type(probe.get(key)) is not int or probe[key] != value for key, value in expected.items())
            or probe.get("checksum_verified") is not True or probe.get("elements_bitwise_verified") is not True
            or probe.get("fast_math_enabled") is not False or not isinstance(probe.get("addend_bits"), list)
            or any(type(value) is not int for value in probe["addend_bits"]) or probe["addend_bits"] != addends
            or type(probe.get("checksum")) is not int or type(probe.get("expected_checksum")) is not int
            or not 0 < probe["checksum"] == probe["expected_checksum"] < 2**64
            or not isinstance(probe.get("device"), str) or not probe["device"].strip()
            or type(probe.get("gpu_seconds")) not in (int, float)
            or not 0 < probe["gpu_seconds"] <= 90):
        raise ValueError("FMA checksum/configuration verification failed")


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
    source = Path(__file__).with_name("fma_chains_probe.swift")
    source_bytes = source.read_bytes()
    frozen_source = output / "probe.swift"
    frozen_source.write_bytes(source_bytes)
    runner_bytes = Path(__file__).read_bytes()
    (output / "runner.py").write_bytes(runner_bytes)
    binary = output / "probe"
    _run(output, "compile", ["xcrun", "swiftc", "-O", "-framework", "Metal", str(frozen_source), "-o", str(binary)], 90)
    order = CONDITIONS + tuple(reversed(CONDITIONS))
    evidence = {"source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "runner_sha256": hashlib.sha256(runner_bytes).hexdigest(),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "order": order, "cases": [],
                "limitations": ["Logical FMA counts do not establish issued hardware instructions or peak FLOP/s.",
                                "Changing chains changes registers, loop iterations and coefficient reuse.",
                                "Changing threads also changes scheduling, active cores and per-thread work.",
                                "Original command and replay timings describe separate executions.",
                                "Frequency and background activity are uncontrolled.",
                                "Timeline counters can include other device activity; missing limiters remain unavailable."]}
    for trial, (threads, chains) in enumerate(order):
        case = output / f"trial-{trial:02d}-threads-{threads}-chains-{chains}"
        case.mkdir()
        _run(case, "capture", [str(binary), str(threads), str(chains), str(case)], 90,
             {**os.environ, "MTL_CAPTURE_ENABLED": "1"})
        from topdown.collector.apple_files import decode_json_object, read_regular_bytes

        probe = decode_json_object(read_regular_bytes(case / "probe.json"))
        _verify_probe(probe, threads, chains)
        profile = collect(case / "capture.gputrace", case / "replay")
        evidence["cases"].append({"trial": trial, "probe": probe, "profile": profile})
        (output / "results.json").write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n")
        print(f"{threads:>6} threads, {chains:>2} FMA chains/thread: command {probe['gpu_seconds'] * 1000:.4f} ms; "
              f"replay {profile['timeline']['GPU time']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for native captures and results")
    args = parser.parse_args()
    try:
        experiment(args.output)
    except KeyboardInterrupt:
        parser.exit(130, f"FMA-chain experiment cancelled; artifacts retained at {args.output}\n")
    except (OSError, ValueError, AppleProfileError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
