"""Summarize an xctrace metal-gpu-counter-intervals export without raw samples."""

import argparse
from bisect import bisect_left
from collections import Counter
import io
import json
import math
from pathlib import Path
import re
from statistics import median
from typing import NamedTuple
import xml.etree.ElementTree as ET


PROBE_LABEL = re.compile(r"^topdown GPU probe pass (\d+):(burn|stream) compute pass \1(?:\s|$)")
PACKAGED_PROBE_LABEL = re.compile(
    r"^topdown GPU (compute|alu|memory|stream) pass (\d+):\1 compute pass \2(?:\s|$)"
)
PASS_DISTRIBUTION_METRICS = {"ALU Limiter", "ALU Utilization", "Buffer Write Limiter",
                             "F32 Utilization", "GPU Read Bandwidth"}


class _Cell(NamedTuple):
    text: str | None
    fmt: str | None
    pid: str | None = None


def _table(source: bytes | Path, expected_schema: str):
    stream = source.open("rb") if isinstance(source, Path) else io.BytesIO(source)
    references = {}
    columns = None
    schema_count = 0
    stack = []
    with stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                stack.append(element)
                continue
            if element.tag == "schema":
                schema_count += 1
                if schema_count != 1 or element.get("name") != expected_schema:
                    raise ValueError(f"expected one {expected_schema} schema")
                columns = [column.findtext("mnemonic") for column in element.findall("col")]
                if len(columns) != len(set(columns)):
                    raise ValueError("duplicate table columns")
            elif element.tag == "row":
                if columns is None:
                    raise ValueError("row appears before its schema")
                for item in element.iter():
                    if item.get("id"):
                        if item.get("id") in references:
                            raise ValueError(f"duplicate XML id {item.get('id')}")
                        references[item.get("id")] = _Cell(item.text, item.get("fmt"))
                for item in element.iter("process"):
                    if item.get("id"):
                        child = item.find("pid")
                        if child is not None:
                            value = references.get(child.get("ref")) if child.get("ref") else _Cell(child.text, None)
                            references[item.get("id")] = _Cell(item.text, item.get("fmt"), value.text if value else None)
                cells = []
                for item in element:
                    if item.get("ref"):
                        try:
                            cells.append(references[item.get("ref")])
                        except KeyError as error:
                            raise ValueError(f"unresolved XML reference {error.args[0]}") from error
                    elif item.tag == "process":
                        child = item.find("pid")
                        value = references.get(child.get("ref")) if child is not None and child.get("ref") else None
                        cells.append(_Cell(item.text, item.get("fmt"), value.text if value else child.text if child is not None else None))
                    else:
                        cells.append(_Cell(item.text, item.get("fmt")))
                if columns and len(cells) != len(columns):
                    raise ValueError("table row does not match its schema")
                stack[-2].remove(element)
                element.clear()
                yield columns, cells
            stack.pop()
    if schema_count != 1:
        raise ValueError(f"expected one {expected_schema} schema")


def _integer(cell, field: str) -> int:
    try:
        value = int(cell.text)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {field}") from error
    if value < 0:
        raise ValueError(f"negative {field}")
    return value


def _overlap(start: int, end: int, windows: list[tuple[int, int]]) -> int:
    if not windows or end <= start:
        return 0
    index = max(0, bisect_left(windows, (start, -1)) - 1)
    total = 0
    while index < len(windows) and windows[index][0] < end:
        begin, stop = windows[index]
        total += max(0, min(end, stop) - max(start, begin))
        index += 1
    return total


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged = []
    for begin, end in sorted(windows):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    return merged


def _intersection(left: list[tuple[int, int]], right: list[tuple[int, int]]) -> list[tuple[int, int]]:
    shared = []
    left_index = right_index = 0
    while left_index < len(left) and right_index < len(right):
        start = max(left[left_index][0], right[right_index][0])
        end = min(left[left_index][1], right[right_index][1])
        if start < end:
            shared.append((start, end))
        if left[left_index][1] < right[right_index][1]:
            left_index += 1
        else:
            right_index += 1
    return shared


def _process_pid(cell) -> int | None:
    return _integer(_Cell(cell.pid, None), "probe PID") if cell.pid is not None else None


def _percentile(values: list[float], fraction: float) -> float:
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(source: bytes, gpu_intervals: bytes | None = None, pid: int | None = None) -> dict:
    if pid is not None and (type(pid) is not int or pid <= 0):
        raise ValueError("probe PID must be a positive integer")
    if pid is not None and gpu_intervals is None:
        raise ValueError("probe PID requires GPU intervals")
    names = Counter()
    samples = set()
    metric_samples = {}
    values = []
    last_metric_end = {}
    for columns, row in _table(source, "metal-gpu-counter-intervals"):
        if columns:
            if not {"start", "duration", "name"}.issubset(columns):
                raise ValueError("counter table lacks required columns")
            start_cell = row[columns.index("start")]
            duration_cell = row[columns.index("duration")]
            name_cell = row[columns.index("name")]
        else:
            if len(row) < 4:
                raise ValueError("counter row has fewer than four columns")
            start_cell, duration_cell, name_cell = row[0], row[1], row[3]
        if not start_cell.text or not name_cell.text:
            raise ValueError("counter row lacks start time or counter name")
        start = _integer(start_cell, "counter start")
        duration = _integer(duration_cell, "counter duration")
        if duration == 0:
            raise ValueError("counter duration must be positive")
        samples.add((start, start + duration))
        names[name_cell.text] += 1
        if {"name", "value", "label", "is-percentage"}.issubset(columns):
            name = row[columns.index("name")].fmt or row[columns.index("name")].text
            if start < last_metric_end.get(name, -1):
                raise ValueError(f"overlapping or out-of-order counter intervals for {name}")
            last_metric_end[name] = start + duration
            label = row[columns.index("label")].fmt or ""
            try:
                value = float(row[columns.index("value")].text)
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid counter value for {name}") from error
            if not math.isfinite(value):
                raise ValueError(f"non-finite counter value for {name}")
            percentage = row[columns.index("is-percentage")].text
            if percentage not in {"0", "1"}:
                raise ValueError(f"invalid percentage flag for {name}")
            unit = "%" if percentage == "1" else "GB/s" if " GB/s" in label else "native"
            metric_samples.setdefault(name, []).append((start, start + duration))
            values.append((start, start + duration, name, value, unit, label))
    result = {
        "schema": "metal-gpu-counter-intervals",
        "counter_scope": "device-wide",
        "counter_rows": sum(names.values()),
        "sample_intervals": len(samples),
        "counter_names": dict(sorted(names.items())),
    }
    if gpu_intervals is None:
        return result

    labeled = []
    active = []
    state_available = False
    for columns, row in _table(gpu_intervals, "metal-gpu-intervals"):
        try:
            start_cell = row[columns.index("start")]
            duration_cell = row[columns.index("duration")]
            label_cell = row[columns.index("event-label")]
        except (ValueError, IndexError) as error:
            raise ValueError("GPU interval table lacks probe fields") from error
        label = label_cell.fmt or label_cell.text or ""
        match = PROBE_LABEL.match(label)
        if match:
            workload, index = match.group(2), int(match.group(1))
        else:
            match = PACKAGED_PROBE_LABEL.match(label)
            if match:
                workload, index = match.group(1), int(match.group(2))
        process_pid = None
        if "process" in columns:
            process_pid = _process_pid(row[columns.index("process")])
            if match and process_pid is None:
                raise ValueError("labeled probe GPU interval lacks a process PID")
        if match:
            start = _integer(start_cell, "probe start")
            duration = _integer(duration_cell, "probe duration")
            if duration == 0:
                raise ValueError("probe duration must be positive")
            labeled.append((process_pid, workload, index, start, start + duration))
        if "state" in columns:
            state_available = True
            state_cell = row[columns.index("state")]
            if (state_cell.fmt or state_cell.text) == "Active":
                start = _integer(start_cell, "other GPU start")
                duration = _integer(duration_cell, "other GPU duration")
                active.append((process_pid, bool(match), start, start + duration))

    probe_pids = {process_pid for process_pid, _, _, _, _ in labeled if process_pid is not None}
    if pid is None and len(probe_pids) > 1:
        raise ValueError("multiple labeled probe PIDs; pass --pid to select one")
    selected_pid = pid if pid is not None else next(iter(probe_pids), None)
    if pid is not None and pid not in probe_pids:
        raise ValueError(f"no labeled probe intervals for PID {pid}")
    passes = {}
    for process_pid, workload, index, start, end in labeled:
        if selected_pid is None or process_pid == selected_pid:
            passes.setdefault((workload, index), []).append((start, end))
    other_active = [(start, end) for process_pid, probe, start, end in active
                    if not probe or (selected_pid is not None and process_pid != selected_pid)]

    pass_windows = {key: _merge(pieces) for key, pieces in passes.items()}
    windows = _merge([window for pieces in pass_windows.values() for window in pieces])
    sampled = _merge([sample for sample in samples if _overlap(*sample, windows)])
    other_windows = _merge(other_active)
    sampled_probe = _intersection(sampled, windows)
    gpu_work_time = sum(end - start for start, end in windows)
    sampled_gpu_time = sum(end - start for start, end in sampled_probe)
    sampled_other_time = sum(end - start for start, end in _intersection(sampled_probe, other_windows))
    pass_fractions = []
    for pieces in pass_windows.values():
        pass_time = sum(end - start for start, end in pieces)
        sampled_time = sum(end - start for start, end in _intersection(sampled, pieces))
        pass_fractions.append(sampled_time / pass_time)
    covered = sum(fraction > 0 for fraction in pass_fractions)
    metric_coverage = {}
    for name, intervals in metric_samples.items():
        metric_coverage[name] = sum(
            any(_overlap(start, end, intervals) for start, end in pieces)
            for pieces in pass_windows.values()
        )
    result["probe_coverage"] = {
        "selected_pid": selected_pid,
        "status": "no_probe_intervals" if not passes else "sampled" if covered else "no_counter_overlap",
        "workloads_observed": sorted({workload for workload, _ in passes}),
        "passes_observed": len(passes),
        "pass_index_min": min(index for _, index in passes) if passes else None,
        "pass_index_max": max(index for _, index in passes) if passes else None,
        "passes_with_counter_samples": covered,
        "passes_with_counter_samples_by_metric": dict(sorted(metric_coverage.items())),
        "sampled_pass_fraction_min": min(pass_fractions) if pass_fractions else None,
        "sampled_pass_fraction_median": median(pass_fractions) if pass_fractions else None,
        "sampled_pass_fraction_max": max(pass_fractions) if pass_fractions else None,
        "passes_below_10_percent_sampled": sum(fraction < 0.1 for fraction in pass_fractions),
        "gpu_work_time_ns": gpu_work_time,
        "sampled_gpu_work_time_ns": sampled_gpu_time,
        "sampled_gpu_work_fraction": sampled_gpu_time / gpu_work_time if gpu_work_time else None,
        "counter_sample_intervals_overlapping": sum(bool(_overlap(*sample, windows)) for sample in samples),
        "other_active_gpu_time_overlapping_ns": (
            sum(end - start for start, end in _intersection(other_windows, windows)) if state_available else None
        ),
        "other_active_gpu_time_in_counter_samples_ns": sampled_other_time if state_available else None,
        "observed_other_activity_fraction_of_sampled_gpu_work": (
            sampled_other_time / sampled_gpu_time if state_available and sampled_gpu_time else None
        ),
        "last_counter_end_minus_last_probe_end_ns": (
            max(end for _, end in samples) - max(end for _, end in windows) if samples and windows else None
        ),
    }
    means = {}
    clean_means = {}
    per_pass = {}
    clean_per_pass = {}
    top_categories = Counter()
    clean_top_categories = Counter()
    for start, end, name, value, unit, label in values:
        overlap = _overlap(start, end, windows)
        if not overlap:
            continue
        clean = state_available and not _overlap(start, end, other_windows)
        if name in PASS_DISTRIBUTION_METRICS:
            for key, pieces in pass_windows.items():
                pass_overlap = _overlap(start, end, pieces)
                if pass_overlap:
                    metric = per_pass.setdefault((key, name), [0.0, 0])
                    metric[0] += pass_overlap * value
                    metric[1] += pass_overlap
                    if clean:
                        metric = clean_per_pass.setdefault((key, name), [0.0, 0])
                        metric[0] += pass_overlap * value
                        metric[1] += pass_overlap
        buckets = [(means, top_categories)]
        if clean:
            buckets.append((clean_means, clean_top_categories))
        for bucket, categories in buckets:
            metric = bucket.setdefault(name, {"weighted_sum": 0.0, "sampled_time_ns": 0,
                                              "counter_intervals": 0, "unit": unit})
            if metric["unit"] != unit:
                raise ValueError(f"mixed units for {name}")
            metric["weighted_sum"] += overlap * value
            metric["sampled_time_ns"] += overlap
            metric["counter_intervals"] += 1
            if name == "Top Performance Limiter":
                category = re.search(r"\((-?\d+\.([^()]*)\))$", label)
                if category:
                    categories[category.group(2)] += overlap

    def summarize_means(bucket):
        return {name: {"value": round(metric["weighted_sum"] / metric["sampled_time_ns"], 6),
                       "unit": metric["unit"], "sampled_time_ns": metric["sampled_time_ns"],
                       "counter_intervals": metric["counter_intervals"]}
                for name, metric in sorted(bucket.items())}

    def summarize_categories(categories):
        total = sum(categories.values())
        return [{"name": name, "sampled_time_ns": duration,
                 "share_of_top_limiter_samples_percent": round(100 * duration / total, 4)}
                for name, duration in sorted(categories.items(), key=lambda item: (-item[1], item[0]))]

    result["sampled_counter_means"] = summarize_means(means)
    def summarize_distributions(bucket, means_bucket):
        distributions = {}
        for name in sorted(PASS_DISTRIBUTION_METRICS):
            sampled = sorted(total / duration for (key, metric), (total, duration) in bucket.items()
                             if metric == name and duration)
            if sampled:
                distributions[name] = {
                    "sampled_passes": len(sampled), "unit": means_bucket[name]["unit"],
                    "minimum": round(sampled[0], 6),
                    "p10": round(_percentile(sampled, 0.1), 6),
                    "median": round(median(sampled), 6),
                    "p90": round(_percentile(sampled, 0.9), 6),
                    "maximum": round(sampled[-1], 6),
                }
        return distributions

    result["sampled_per_pass_metric_distributions"] = summarize_distributions(per_pass, means)
    result["sampled_per_pass_metric_distributions_without_observed_other_activity"] = (
        summarize_distributions(clean_per_pass, clean_means) if state_available else None
    )
    result["top_limiter_categories"] = summarize_categories(top_categories)
    result["top_limiter_unparsed_sampled_time_ns"] = (
        means.get("Top Performance Limiter", {}).get("sampled_time_ns", 0) - sum(top_categories.values())
    )
    result["sampled_counter_means_without_observed_other_activity"] = (
        summarize_means(clean_means) if state_available else None
    )
    result["top_limiter_categories_without_observed_other_activity"] = (
        summarize_categories(clean_top_categories) if state_available else None
    )
    result["top_limiter_unparsed_clean_sampled_time_ns"] = (
        clean_means.get("Top Performance Limiter", {}).get("sampled_time_ns", 0)
        - sum(clean_top_categories.values()) if state_available else None
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--gpu-intervals", type=Path, help="exported metal-gpu-intervals XML for probe coverage")
    parser.add_argument("--pid", type=int, help="select a labeled probe PID when the trace contains multiple probes")
    args = parser.parse_args()
    try:
        result = summarize(args.source, args.gpu_intervals, args.pid)
    except (ET.ParseError, ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
