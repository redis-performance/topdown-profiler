"""Native Processor Trace intervals and gap markers, independent of slot TMA."""

from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

from topdown.collector.apple import AppleProfileError, TMA_ROOTS, _native_uint, _resolve_ids, _validate_scope, _xml

INTERVAL_TYPES = {
    "start": "start-time", "duration": "duration", "process": "process", "thread": "thread",
    "depth": "layout-id", "name": "text-location", "self-instructions": "uint64",
    "self-cycles": "uint64", "self-duration": "duration", "backtrace": "text-backtrace",
}
GAP_TYPES = {"timestamp": "start-time", "process": "process", "thread": "thread", "description": "string"}


def _table(path, schema, required):
    root = _xml(path)
    resolve = _resolve_ids(root)
    tables = [node for node in root.iter("node") if node.find("schema") is not None]
    if len(tables) != 1 or tables[0].find("schema").get("name") != schema:
        raise AppleProfileError(f"Expected exactly one {schema} table")
    metadata = tables[0].find("schema")
    columns = [col.findtext("mnemonic") for col in metadata.findall("col")]
    types = {col.findtext("mnemonic"): col.findtext("engineering-type") for col in metadata.findall("col")}
    if len(set(columns)) != len(columns) or any(types.get(key) != value for key, value in required.items()):
        raise AppleProfileError(f"Missing, duplicate or unknown {schema} column types")
    rows = []
    for row in tables[0].findall("row"):
        if len(row) != len(columns):
            raise AppleProfileError(f"Wrong {schema} row width")
        rows.append({key: resolve(value) for key, value in zip(columns, row)})
    return rows, resolve


def _uint(node, tag):
    text = node.text if node is not None else None
    if node is None or node.tag != tag:
        raise AppleProfileError(f"Require a native {tag} uint64 integer")
    return _native_uint(text, tag)


def _identity(cells, resolve):
    process, thread = cells["process"], cells["thread"]
    if process.tag != "process" or thread.tag != "thread":
        raise AppleProfileError("Require native process/thread identities")
    pid = _uint(resolve(process.find("pid")), "pid")
    tid = _uint(resolve(thread.find("tid")), "tid")
    parent = resolve(thread.find("process"))
    if not pid or not tid or parent is None or _uint(resolve(parent.find("pid")), "pid") != pid:
        raise AppleProfileError("Thread and process PID disagree or identity is absent")
    return pid, tid


def parse_trace_profile(interval_path: Path, gap_path: Path, pid: int,
                        start_ns: int = 0, end_ns: int | None = None) -> dict:
    """Keep complete in-window intervals; never prorate counters at a boundary."""
    _validate_scope(pid, start_ns, end_ns)
    if pid is None:
        raise AppleProfileError("Require an explicit PID for Processor Trace parsing")
    gap_rows, resolve = _table(gap_path, "processor-trace-gaps", GAP_TYPES)
    gaps, gap_times = [], defaultdict(list)
    target_gap_count = 0
    for cells in gap_rows:
        row_pid, tid = _identity(cells, resolve)
        if row_pid != pid:
            continue
        timestamp = _uint(cells["timestamp"], "start-time")
        target_gap_count += 1
        if timestamp < start_ns or (end_ns is not None and timestamp >= end_ns):
            continue
        if cells["description"].tag != "string":
            raise AppleProfileError("Unknown Processor Trace gap description")
        gaps.append({"time_ns": timestamp, "pid": pid, "tid": tid, "description": cells["description"].text})
        gap_times[tid].append(timestamp)
    for times in gap_times.values():
        times.sort()

    rows, resolve = _table(interval_path, "processor-trace-intervals", INTERVAL_TYPES)
    intervals, excluded, boundaries = [], 0, 0
    for cells in rows:
        row_pid, tid = _identity(cells, resolve)
        if row_pid != pid:
            excluded += 1
            continue
        timestamp = _uint(cells["start"], "start-time")
        duration = _uint(cells["duration"], "duration")
        finish = timestamp + duration
        if finish >= 2**64:
            raise AppleProfileError("Processor Trace interval end exceeds uint64")
        if (finish <= start_ns and duration > 0) or (end_ns is not None and timestamp >= end_ns):
            excluded += 1
            continue
        if timestamp < start_ns or (end_ns is not None and finish > end_ns):
            boundaries += 1
            continue
        name, backtrace, own_duration = cells["name"], cells["backtrace"], cells["self-duration"]
        if name.tag != "text-location" or backtrace.tag != "text-backtrace":
            raise AppleProfileError("Unknown Processor Trace code-location/backtrace representation")
        if own_duration.tag == "sentinel":
            self_duration = None
        else:
            self_duration = _uint(own_duration, "duration")
            if self_duration > duration:
                raise AppleProfileError("Self duration exceeds interval duration")
        frames = []
        for entry in backtrace.findall("frame"):
            frame = resolve(entry)
            binary = resolve(frame.find("binary"))
            frames.append({"name": frame.get("name"), "address": frame.get("addr"),
                           "binary": binary.get("name") if binary is not None else None,
                           "binary_uuid": binary.get("UUID") if binary is not None else None,
                           "load_address": binary.get("load-addr") if binary is not None else None})
        offset = resolve(name.find("mach-o-vm-offset"))
        times = gap_times[tid]
        intervals.append({
            "start_ns": timestamp, "duration_ns": duration, "pid": pid, "tid": tid,
            "depth": _uint(cells["depth"], "layout-id"), "native_label": name.get("fmt"),
            "binary_offset": _uint(offset, "mach-o-vm-offset") if offset is not None else None,
            "self_instructions": _uint(cells["self-instructions"], "uint64"),
            "self_cycles": _uint(cells["self-cycles"], "uint64"), "self_duration_ns": self_duration,
            "gap_markers_inside": bisect_left(times, finish) - bisect_left(times, timestamp), "frames": frames,
        })
    if not intervals:
        raise AppleProfileError("No Processor Trace intervals in the selected PID/window")
    return {
        "schema": "processor-trace-intervals", "scope": {"pid": pid, "start_ns": start_ns, "end_ns": end_ns},
        "interval_count": len(intervals), "excluded_rows": excluded, "boundary_rows_excluded": boundaries,
        "intervals": intervals, "gaps": gaps, "gap_count": len(gaps), "target_gap_count_in_run": target_gap_count,
        "coverage_complete": False, "tma_available": False, "tma": dict.fromkeys(TMA_ROOTS),
        "limitations": [
            "Decoded execution intervals, not statistical samples or a complete execution guarantee.",
            "Gap markers are points; their count and absence do not quantify missing work or prove completeness.",
            "Native self counts remain separate from interval durations and process-accounted counters.",
            "Overlapping call intervals must not be summed as elapsed time; missing self duration remains null.",
            "Boundary-crossing intervals are excluded whole; their counts are never prorated.",
            "No programmable events, stall-cause percentages, IPC or TMA shares are derived.",
        ],
    }
