"""Function hotspots from exported Apple stack samples, retaining their native scope."""

from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from topdown.collector.apple import AppleProfileError, TMA_ROOTS, _validate_scope
from topdown.collector.apple_files import decode_json_object, read_regular_bytes

STACK_SCHEMAS = {"counters-profile", "cpu-profile", "time-profile"}


def summarize_hotspots(profile: dict, limit: int = 20) -> dict:
    """Count leaf observations and count each function once per sampled stack."""
    if type(limit) is not int or limit < 1:
        raise AppleProfileError("Require a positive hotspot limit")
    if (not isinstance(profile, dict) or not isinstance(profile.get("schema"), str)
            or profile["schema"] not in STACK_SCHEMAS):
        raise AppleProfileError("Hotspots require an exported fixed, M1 event, cpu or time stack profile")
    samples, scope = profile.get("samples"), profile.get("scope")
    if (not isinstance(samples, list) or not samples or type(profile.get("sample_count")) is not int
            or profile["sample_count"] != len(samples)):
        raise AppleProfileError("Require nonempty stack samples and a matching sample_count")
    if (not isinstance(scope, dict) or type(scope.get("pid")) is not int
            or type(scope.get("start_ns")) is not int
            or (scope.get("end_ns") is not None and type(scope["end_ns"]) is not int)):
        raise AppleProfileError("Require an explicit PID and valid time scope")
    _validate_scope(scope["pid"], scope["start_ns"], scope.get("end_ns"))
    for field in ["unmeasured_rows", "excluded_rows"]:
        if type(profile.get(field, 0)) is not int or profile.get(field, 0) < 0:
            raise AppleProfileError(f"Invalid {field}")

    leaves, stacks = Counter(), Counter()
    unresolved = 0
    for sample in samples:
        if (not isinstance(sample, dict) or type(sample.get("pid")) is not int or sample["pid"] != scope["pid"]
                or not isinstance(sample.get("frames"), list) or type(sample.get("time_ns")) is not int
                or sample["time_ns"] < scope["start_ns"]
                or (scope.get("end_ns") is not None and sample["time_ns"] >= scope["end_ns"])):
            raise AppleProfileError("Invalid stack sample or sample disagrees with profile PID/time scope")
        frames = []
        for frame in sample["frames"]:
            if (not isinstance(frame, dict)
                    or any(frame.get(key) is not None and not isinstance(frame[key], str)
                           for key in ["name", "address", "binary"])):
                raise AppleProfileError("Invalid stack frame")
            name = frame.get("name") or frame.get("address") or "[unresolved]"
            frames.append((frame.get("binary") or "[unknown binary]", name))
        if not frames:
            unresolved += 1
            continue
        leaves[frames[0]] += 1
        stacks.update(set(frames))
    if not stacks:
        raise AppleProfileError("No stack frames were observed; this profile cannot locate function hotspots")
    ordered = sorted(stacks, key=lambda key: (-leaves[key], -stacks[key], key))
    return {
        "schema_version": 1, "measurement_kind": "sampled-stack-hotspots", "source_schema": profile["schema"],
        "scope": dict(scope), "sample_count": len(samples), "unresolved_stack_samples": unresolved,
        "unmeasured_rows": profile.get("unmeasured_rows", 0), "excluded_rows": profile.get("excluded_rows", 0),
        "hotspot_count": len(ordered),
        "hotspots": [{"function": name, "binary": binary, "leaf_samples": leaves[binary, name],
                      "leaf_share": leaves[binary, name] / len(samples), "stack_samples": stacks[binary, name]}
                     for binary, name in ordered[:limit]],
        "tma_available": False, "tma": dict.fromkeys(TMA_ROOTS),
        "limitations": [
            "Leaf share is a fraction of recorded measured samples, including samples without stack frames.",
            "Stack counts include callers, with recursive occurrences counted once per sample.",
            "Sample frequency does not establish CPU-time percentages, instruction costs, or TMA shares.",
            "Native counter values and sample weights are not summed or used to weight these counts.",
            "Sampling loss, unmeasured rows, and missing stacks can affect hotspot rankings.",
        ],
    }


def load_hotspots(path: Path, limit: int = 20) -> dict:
    if path.is_dir():
        path = path / "profile.json"
    try:
        profile = decode_json_object(read_regular_bytes(path))
    except (OSError, ValueError) as exc:
        raise AppleProfileError(f"Cannot read Apple profile {path}: {exc}") from exc
    return summarize_hotspots(profile, limit)


def print_hotspots(report: dict) -> None:
    console = Console()
    scope = report["scope"]
    console.print(f"Apple stack hotspots — PID {scope['pid']}")
    console.print(f"Scope: {scope['start_ns']} ns to "
                  + (f"{scope['end_ns']} ns" if scope.get("end_ns") is not None else "end of capture"))
    console.print(f"Measured samples: {report['sample_count']:,} | "
                  f"Missing stacks: {report['unresolved_stack_samples']:,} | "
                  f"Unmeasured rows: {report['unmeasured_rows']:,}")
    table = Table(expand=True)
    table.add_column("Leaf samples", width=12, justify="right", no_wrap=True)
    table.add_column("Leaf share", width=10, justify="right", no_wrap=True)
    table.add_column("In stack", width=8, justify="right", no_wrap=True)
    table.add_column("Function", ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("Binary", ratio=1, no_wrap=True, overflow="ellipsis")
    for row in report["hotspots"]:
        table.add_row(str(row["leaf_samples"]), f"{row['leaf_share']:.1%}", str(row["stack_samples"]),
                      Text(row["function"]), Text(row["binary"]))
    console.print(table)
    if report["hotspot_count"] > len(report["hotspots"]):
        console.print(f"Showing {len(report['hotspots'])} of {report['hotspot_count']} functions; use --limit to show more.")
    console.print("In stack counts each function once per sample, including callers.")
    console.print("Shares describe recorded samples. CPU-time percentages and TMA shares are unavailable in this report.")
    console.print("Native counter values and sample weights are not summed.")
