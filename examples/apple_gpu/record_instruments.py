"""Record a checksum-verified M1 Metal control with Instruments GPU counters."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import xml.etree.ElementTree as ET

from make_counter_template import PROFILE_NAMES, select_profile
from summarize_counters import summarize


TEMPLATE = ("Applications/Instruments.app/Contents/Packages/GPU.instrdst/Contents/Templates/"
            "Metal System Trace.tracetemplate")
REQUIRED_COUNTERS = {
    3: {"ALU Limiter": "%", "Buffer Write Limiter": "%", "GPU Read Bandwidth": "GB/s",
        "Top Performance Limiter": "%"},
    4: {"ALU Utilization": "%", "Buffer Store Utilization": "%", "GPU Read Bandwidth": "GB/s"},
}
PACKAGED_PROBE = Path(__file__).resolve().parents[2] / "topdown/collector/apple_gpu_probe.swift"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(output: Path, name: str, command: list[str], timeout: float) -> None:
    (output / f"{name}.command.json").write_text(json.dumps(command, indent=2) + "\n")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        for suffix, value in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            (output / f"{name}.{suffix}").write_text(
                value.decode(errors="replace") if isinstance(value, bytes) else value or "")
        raise ValueError(f"{name} timed out; retained artifacts in {output}") from exc
    (output / f"{name}.stdout").write_text(result.stdout)
    (output / f"{name}.stderr").write_text(result.stderr)
    if result.returncode:
        raise ValueError(f"{name} failed (exit {result.returncode}): "
                         f"{result.stderr.strip() or result.stdout.strip()}; artifacts: {output}")


def _verify(toc: Path, stdout: Path, profile: int, mode: str, passes: int,
            probe_source: str = "m1", items: int | None = None, probe_result: Path | None = None) -> None:
    text = stdout.read_text()
    identity = (f"workload={mode} passes={passes} " if probe_source == "m1"
                else f"passes={passes} items={items} ")
    if ("device=Apple M1\n" not in text or identity not in text
            or "checksum_verified=true" not in text):
        raise ValueError("M1 probe identity, arguments or checksum could not be verified")
    if probe_source == "packaged":
        try:
            result = json.loads(probe_result.read_text())
        except (AttributeError, OSError, ValueError) as exc:
            raise ValueError("Packaged M1 probe result JSON is unavailable or invalid") from exc
        commands = result.get("commands") if isinstance(result, dict) else None
        checksum = result.get("checksum") if isinstance(result, dict) else None
        expected = result.get("expected_checksum") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("device") != "Apple M1" or result.get("mode") != mode
                or result.get("passes") != passes or result.get("items") != items
                or result.get("checksum_verified") is not True
                or type(checksum) not in (int, float) or type(expected) not in (int, float)
                or not math.isfinite(checksum) or not math.isfinite(expected)
                or not math.isclose(checksum, expected, rel_tol=1e-6, abs_tol=1e-6)
                or not isinstance(commands, list) or len(commands) != passes
                or any(not isinstance(row, dict) or row.get("label") != f"topdown GPU {mode} pass {index}"
                       for index, row in enumerate(commands))):
            raise ValueError("Packaged M1 probe checksum, configuration or command labels could not be verified")
    try:
        run = ET.parse(toc).getroot().find("./run[@number='1']")
    except ET.ParseError as exc:
        raise ValueError("Invalid Instruments table of contents") from exc
    target = run.find("./info/target/process") if run is not None else None
    if target is None or target.get("return-exit-status") != "0":
        raise ValueError("Instruments target did not exit successfully")
    profiles = run.findall("./data/table[@schema='metal-gpu-counter-profile']")
    if len(profiles) != 1 or profiles[0].get("counter-profile") != str(profile):
        raise ValueError("Instruments did not select the requested GPU counter profile")
    if f"Counter Set: {PROFILE_NAMES[profile]}" not in {node.text for node in run.iter("value")}:
        raise ValueError("Instruments did not identify the requested GPU counter set")
    for schema in ("metal-gpu-counter-intervals", "metal-gpu-intervals"):
        if len(run.findall(f"./data/table[@schema='{schema}']")) != 1:
            raise ValueError(f"Instruments did not export exactly one {schema} table")


def _verify_coverage(result: dict, mode: str, passes: int, profile: int, output: Path,
                     probe_source: str = "m1") -> None:
    coverage = result["probe_coverage"]
    expected_workload = ("burn" if mode == "alu" else "stream") if probe_source == "m1" else mode
    if (coverage["workloads_observed"] != [expected_workload] or coverage["passes_observed"] != passes
            or coverage.get("pass_index_min") != 0 or coverage.get("pass_index_max") != passes - 1
            or coverage["passes_with_counter_samples"] != passes or not result["sampled_counter_means"]):
        raise ValueError(f"Incomplete M1 probe counter coverage; artifacts: {output}")
    means = result["sampled_counter_means"]
    if any(means.get(name, {}).get("unit") != unit for name, unit in REQUIRED_COUNTERS[profile].items()):
        raise ValueError(f"Missing M1 GPU metrics or unexpected units for counter profile {profile}; artifacts: {output}")
    metric_coverage = coverage.get("passes_with_counter_samples_by_metric", {})
    if any(metric_coverage.get(name) != passes for name in REQUIRED_COUNTERS[profile]):
        raise ValueError(f"Incomplete M1 probe metric coverage; artifacts: {output}")
    if profile == 3 and (not result["top_limiter_categories"]
                         or result["top_limiter_unparsed_sampled_time_ns"]):
        raise ValueError(f"Unrecognized M1 top-limiter labels; artifacts: {output}")


def _verify_probe_process(toc: Path, binary: Path, pid: int | None) -> None:
    matches = [process for process in ET.parse(toc).getroot().iter("process")
               if process.get("path") == str(binary)]
    if len(matches) != 1 or pid is None or matches[0].get("pid") != str(pid):
        raise ValueError("M1 GPU counter PID does not match the compiled probe process in the trace")


def record(output: Path, mode: str = "alu", passes: int | None = None, profile: int = 3,
           tail: float = 1.0, time_limit: int = 8, probe_source: str = "m1",
           items: int | None = None) -> dict:
    if probe_source not in ("m1", "packaged"):
        raise ValueError("Choose m1 or packaged probe source")
    if passes is None:
        passes = 8 if probe_source == "packaged" and mode in ("memory", "stream") else 100
    if probe_source == "packaged":
        if items is None:
            items = 16777216 if mode in ("memory", "stream") else 65536
    valid_mode = mode in (("alu", "stream") if probe_source == "m1" else ("compute", "alu", "memory", "stream"))
    valid_passes = type(passes) is int and 1 <= passes <= 500
    valid_items = (items is None if probe_source == "m1" else
                   type(items) is int and 1 <= items <= 16777216 and valid_passes
                   and passes * items <= 268435456
                   and (mode != "stream" or items % 64 == 0))
    if (not valid_mode or not valid_passes or not valid_items
            or profile not in PROFILE_NAMES or not math.isfinite(tail) or not 0.5 <= tail <= 5
            or type(time_limit) is not int or not 5 <= time_limit <= 60):
        raise ValueError("Choose a supported probe mode, 1–500 passes, valid items, profile 3/4, "
                         "tail 0.5–5s and time limit 5–60s")
    developer = Path(os.environ.get("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")).resolve()
    source_template = developer.parent / TEMPLATE
    source_probe = Path(__file__).with_name("probe.swift") if probe_source == "m1" else PACKAGED_PROBE
    if not source_template.is_file():
        raise ValueError(f"Full Xcode Metal System Trace template is missing at {source_template}; set DEVELOPER_DIR")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    template = output / "counters.tracetemplate"
    template.write_bytes(select_profile(source_template.read_bytes(), profile))
    binary, trace = output / "probe", output / "recording.trace"
    _run(output, "compile", ["xcrun", "swiftc", "-O", "-framework", "Metal", str(source_probe),
                             "-o", str(binary)], 90)
    probe_result = output / "probe.json" if probe_source == "packaged" else None
    arguments = ([str(binary), str(passes), mode] if probe_source == "m1" else
                 [str(binary), str(passes), "--mode", mode, "--items", str(items),
                  "--result", str(probe_result)])
    target = " ".join(map(shlex.quote, arguments)) + f" && sleep {tail}"
    _run(output, "record", ["xcrun", "xctrace", "record", "--no-prompt", "--template", str(template),
                            "--time-limit", f"{time_limit}s", "--target-stdout", str(output / "target.stdout"),
                            "--output", str(trace), "--launch", "--", "/bin/sh", "-c", target],
         time_limit + 40)
    toc = output / "toc.xml"
    _run(output, "toc", ["xcrun", "xctrace", "export", "--input", str(trace), "--toc",
                          "--output", str(toc)], 60)
    _verify(toc, output / "target.stdout", profile, mode, passes, probe_source, items, probe_result)
    paths = {}
    for name, schema in (("counters", "metal-gpu-counter-intervals"), ("gpu-intervals", "metal-gpu-intervals")):
        path = output / f"{name}.xml"
        xpath = f'/trace-toc/run[@number="1"]/data/table[@schema="{schema}"]'
        _run(output, f"export-{name}", ["xcrun", "xctrace", "export", "--input", str(trace),
                                         "--xpath", xpath, "--output", str(path)], 120)
        paths[name] = path
    result = summarize(paths["counters"], paths["gpu-intervals"])
    _verify_coverage(result, mode, passes, profile, output, probe_source)
    _verify_probe_process(toc, binary, result["probe_coverage"]["selected_pid"])
    result["capture"] = {
        "mode": mode, "passes": passes, "items": items, "probe_source": probe_source,
        "counter_profile": PROFILE_NAMES[profile],
        "checksum_verified": True, "target_exit_status": 0, "probe_process_verified": True,
        "source_sha256": _sha256(source_probe), "binary_sha256": _sha256(binary),
        "template_sha256": _sha256(template),
        "toc_sha256": _sha256(toc), "counter_xml_sha256": _sha256(paths["counters"]),
        "gpu_xml_sha256": _sha256(paths["gpu-intervals"]),
    }
    if probe_result is not None:
        result["capture"]["probe_json_sha256"] = _sha256(probe_result)
    (output / "profile.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for trace, XML and summary")
    parser.add_argument("--probe-source", choices=("m1", "packaged"), default="m1")
    parser.add_argument("--mode", choices=("compute", "alu", "memory", "stream"), default="alu")
    parser.add_argument("--passes", type=int)
    parser.add_argument("--items", type=int)
    parser.add_argument("--profile", type=int, choices=sorted(PROFILE_NAMES), default=3)
    parser.add_argument("--tail", type=float, default=1)
    parser.add_argument("--time-limit", type=int, default=8)
    args = parser.parse_args()
    try:
        result = record(args.output, args.mode, args.passes, args.profile, args.tail, args.time_limit,
                        args.probe_source, args.items)
    except (OSError, ValueError, ET.ParseError) as exc:
        parser.error(str(exc))
    print(json.dumps({"profile": str(args.output.resolve() / "profile.json"),
                      "counter_rows": result["counter_rows"], "probe_coverage": result["probe_coverage"]}, indent=2))


if __name__ == "__main__":
    main()
