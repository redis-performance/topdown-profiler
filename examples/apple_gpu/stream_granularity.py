"""Compare equal stream work at 4/16/64/256 floats per thread with verified output."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from topdown.collector import apple_gpu  # noqa: E402
from topdown.collector.apple import AppleProfileError  # noqa: E402

ORDER = (16, 1, 4, 64, 64, 4, 1, 16)


def variant_source(base, vectors):
    if type(vectors) is not int or vectors not in ORDER:
        raise ValueError("Choose 1, 4, 16 or 64 float4 vectors per thread")
    replacements = {
        "var passes = 4": f"let streamVectors = {vectors}\nvar passes = 4",
        "for (uint j = 0; j < 16; ++j)": r"for (uint j = 0; j < \(streamVectors); ++j)",
        'mode == "stream" ? count / 64 : count': 'mode == "stream" ? count / (4 * streamVectors) : count',
        "let verified = checksum.isFinite": """let elementsVerified = (0..<count).allSatisfy {
    let actual = finalValues[$0]
    let wanted = reference[$0 % 97]
    return actual.isFinite && abs(actual - wanted) <= max(Float(0.000001), abs(wanted) * Float(0.000001))
}
let verified = elementsVerified && checksum.isFinite""",
        '"checksum_verified": verified, "commands": commands':
            '"checksum_verified": verified, "elements_verified": elementsVerified, "stream_vectors": streamVectors, "commands": commands',
    }
    for old, new in replacements.items():
        if base.count(old) != 1:
            raise ValueError("Packaged probe changed; review the stream experiment source transformation")
        base = base.replace(old, new)
    return base


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
    apple_gpu._require_host()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    base_path = Path(apple_gpu.__file__).with_name("apple_gpu_probe.swift")
    base = base_path.read_text()
    sources = {}
    for vectors in sorted(set(ORDER)):
        source = output / f"vectors-{vectors}.swift"
        source.write_text(variant_source(base, vectors))
        binary = output / f"vectors-{vectors}"
        _run(output, f"compile-{vectors}", ["xcrun", "swiftc", "-O", "-framework", "Metal", str(source), "-o", str(binary)], 90)
        sources[vectors] = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    evidence = {"base_source_sha256": hashlib.sha256(base_path.read_bytes()).hexdigest(),
                "order_floats_per_thread": [n * 4 for n in ORDER], "cases": [],
                "limitations": ["Original command and replay GPU times describe separate executions.",
                                "Algorithmic array bytes are not measured DRAM traffic.",
                                "GPU frequency and background activity are not controlled.",
                                "Two trials per variant cannot establish a portable speedup."]}
    for trial, vectors in enumerate(ORDER):
        case = output / f"trial-{trial:02d}-vectors-{vectors}"
        case.mkdir()
        _run(case, "capture", [str(output / f"vectors-{vectors}"), "4", "--mode", "stream", "--items", "16777216",
                               "--capture", str(case / "capture.gputrace"), "--result", str(case / "probe.json")], 60,
             {**os.environ, "MTL_CAPTURE_ENABLED": "1"})
        from topdown.collector.apple_files import decode_json_object, read_regular_bytes

        probe = decode_json_object(read_regular_bytes(case / "probe.json"))
        if (probe.get("checksum_verified") is not True or probe.get("elements_verified") is not True
                or probe.get("stream_vectors") != vectors or probe.get("passes") != 4
                or probe.get("items") != 16777216 or probe.get("mode") != "stream"):
            raise ValueError(f"Stream variant failed output/configuration verification; artifacts: {case}")
        profile = apple_gpu.collect(case / "capture.gputrace", case / "replay")
        evidence["cases"].append({"trial": trial, "float4_vectors_per_thread": vectors, "floats_per_thread": vectors * 4,
                                  **sources[vectors], "probe": probe, "profile": profile})
        (output / "results.json").write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n")
        print(f"{vectors * 4:3} floats/thread: command {probe['gpu_seconds'] * 1000:.4f} ms; "
              f"replay {profile['timeline']['GPU time']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for sources, traces and native profiles")
    args = parser.parse_args()
    try:
        experiment(args.output)
    except KeyboardInterrupt:
        parser.exit(130, f"Stream experiment cancelled; artifacts retained at {args.output}\n")
    except (OSError, ValueError, AppleProfileError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
