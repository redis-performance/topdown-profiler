"""Compare an M1 calibration trace's raw counters with Xcode guided ratios."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
from statistics import median
import xml.etree.ElementTree as ET
from pathlib import Path

from topdown.collector.apple import _resolve_ids, parse_apple_bottlenecks


CATEGORIES = ("useful", "processing", "discarded", "delivery")


def _core_kind(core: int) -> str:
    return "E" if core < 4 else "P"


def _candidate(values: list[int], core: int) -> dict[str, float]:
    active = values[2]
    width = 4 if core < 4 else 8
    retired, rewind, stall = values[7], values[3], values[4]
    mapped = values[5] + values[6] + values[8]
    estimate = {
        "useful": retired / (width * active),
        "processing": stall / active,
        "discarded": max(0, mapped - retired + width * rewind) / (width * active),
    }
    estimate["delivery"] = max(values[9] / active, 1 - sum(estimate.values()))
    return estimate


def _aggregate_windows(raw_by_scope: dict, guided_samples: list[dict], guided_cycles: int) -> dict:
    guided_by_scope = defaultdict(list)
    for row in guided_samples:
        guided_by_scope[(row["tid"], row["core"])].append(row)
    counts = Counter()
    kind_counts = defaultdict(Counter)
    maximum = dict.fromkeys(CATEGORIES, 0.0)
    aligned_cycles = 0
    aligned_by_kind = Counter()
    guided_by_kind = Counter()
    for row in guided_samples:
        guided_by_kind[_core_kind(row["core"])] += row["cycles"]
    for (tid, core), samples in raw_by_scope.items():
        kind = _core_kind(core)
        samples.sort()
        rows = sorted(guided_by_scope[(tid, core)], key=lambda row: row["time_ns"] + row["duration_ns"])
        ends = [row["time_ns"] + row["duration_ns"] for row in rows]
        for (previous, _), (time, values) in zip(samples, samples[1:]):
            window = rows[bisect_right(ends, previous):bisect_right(ends, time)]
            if not window:
                continue
            counts["windows_with_guided_intervals"] += 1
            if any(row["time_ns"] < previous for row in window):
                counts["windows_with_boundary_straddle"] += 1
                continue
            if any(later["time_ns"] < earlier["time_ns"] + earlier["duration_ns"]
                   for earlier, later in zip(window, window[1:])):
                counts["windows_with_overlapping_guided_intervals"] += 1
                continue
            active = values[2]
            if sum(row["cycles"] for row in window) != active:
                counts["windows_with_cycle_mismatch"] += 1
                continue
            counts["cycle_aligned_windows"] += 1
            counts["cycle_aligned_multi_interval_windows"] += len(window) > 1
            kind_counts[kind]["cycle_aligned_windows"] += 1
            aligned_cycles += active
            aligned_by_kind[kind] += active
            estimate = _candidate(values, core)
            differences = {category: abs(estimate[category] - sum(
                row["shares"][category] * row["cycles"] for row in window) / active) * 100
                for category in CATEGORIES}
            for category, difference in differences.items():
                maximum[category] = max(maximum[category], difference)
            matched = max(differences.values()) < 1e-7
            counts["windows_matching_exported_rounding"] += matched
            kind_counts[kind]["windows_matching_exported_rounding"] += matched
    return {
        **{name: counts[name] for name in (
            "windows_with_guided_intervals", "windows_with_boundary_straddle",
            "windows_with_overlapping_guided_intervals",
            "windows_with_cycle_mismatch", "cycle_aligned_windows",
            "cycle_aligned_multi_interval_windows", "windows_matching_exported_rounding")},
        "guided_cycles_in_cycle_aligned_windows": aligned_cycles,
        "guided_cycle_fraction_in_cycle_aligned_windows": aligned_cycles / guided_cycles,
        "by_core_kind": {kind: {
            "guided_cycles": guided_by_kind[kind],
            "guided_cycles_in_cycle_aligned_windows": aligned_by_kind[kind],
            "guided_cycle_fraction_in_cycle_aligned_windows": aligned_by_kind[kind] / guided_by_kind[kind],
            "cycle_aligned_windows": kind_counts[kind]["cycle_aligned_windows"],
            "windows_matching_exported_rounding": kind_counts[kind]["windows_matching_exported_rounding"],
        } for kind in sorted(guided_by_kind)},
        "max_absolute_difference_percentage_points": maximum,
        "matches_exported_rounding": (counts["cycle_aligned_windows"] > 0
                                      and counts["windows_matching_exported_rounding"] == counts["cycle_aligned_windows"]),
    }


def validate(counters: Path, metrics: Path, pid: int) -> dict:
    guided = parse_apple_bottlenecks(metrics, pid)
    by_end = {(row["time_ns"] + row["duration_ns"], row["tid"], row["core"], row["cycles"]): row
              for row in guided["samples"]}
    if len(by_end) != len(guided["samples"]):
        raise ValueError("Guided intervals have duplicate end-time, thread, core, and cycle keys")

    root = ET.parse(counters).getroot()
    tables = [node for node in root.iter("node") if node.find("schema") is not None]
    if len(tables) != 1 or tables[0].find("schema").get("name") != "counters-profile":
        raise ValueError("Expected one counters-profile table")
    columns = [col.findtext("mnemonic") for col in tables[0].find("schema").findall("col")]
    required = {"time", "thread", "process", "core", "counters-array"}
    if not required.issubset(columns) or len(set(columns)) != len(columns):
        raise ValueError("Invalid counters-profile columns")
    resolve = _resolve_ids(root)
    raw_count = matched_count = matched_cycles = 0
    seen_raw = set()
    raw_times = set()
    raw_scopes = set()
    raw_by_scope = defaultdict(list)
    matched_keys = set()
    guided_by_kind = defaultdict(Counter)
    matched_by_kind = defaultdict(Counter)
    alternative_width_useful_errors = defaultdict(list)
    thread_kinds = defaultdict(set)
    thread_samples = defaultdict(list)
    for row in guided["samples"]:
        if not 0 <= row["core"] <= 7:
            raise ValueError("Expected M1 guided core number 0 through 7")
        kind = _core_kind(row["core"])
        guided_by_kind[kind]["intervals"] += 1
        guided_by_kind[kind]["cycles"] += row["cycles"]
        thread_kinds[row["tid"]].add(kind)
        thread_samples[row["tid"]].append((row["time_ns"], kind))
    maximum = dict.fromkeys(CATEGORIES, 0.0)
    for row in tables[0].findall("row"):
        if len(row) != len(columns):
            raise ValueError("Counter row does not match its schema")
        cells = {name: resolve(cell) for name, cell in zip(columns, row)}
        process = cells["process"]
        if process.tag != "process" or int(resolve(process.find("pid")).text) != pid:
            continue
        vector = cells["counters-array"]
        if vector.tag == "sentinel":
            continue
        values = [int(value) for value in vector.text.split()]
        if len(values) != 12 or values[10:] != [0, 0]:
            raise ValueError("Unexpected combined guided/custom vector layout")
        if any(value < 0 for value in values):
            raise ValueError("Negative M1 counter value")
        core = int(cells["core"].text)
        if not 0 <= core <= 7:
            raise ValueError("Expected M1 core number 0 through 7")
        time = int(cells["time"].text)
        tid = int(resolve(cells["thread"].find("tid")).text)
        raw_times.add(time)
        raw_scopes.add((time, tid, core))
        active = values[2]
        if active == 0:
            continue
        raw_count += 1
        scope = (time, tid, core)
        if scope in seen_raw:
            raise ValueError("Raw counter samples have duplicate end-time, thread, and core scope")
        seen_raw.add(scope)
        key = (*scope, active)
        raw_by_scope[(tid, core)].append((time, values))
        interval = by_end.get(key)
        if interval is None:
            continue
        matched_keys.add(key)
        matched_count += 1
        matched_cycles += interval["cycles"]
        kind = _core_kind(core)
        matched_by_kind[kind]["intervals"] += 1
        matched_by_kind[kind]["cycles"] += interval["cycles"]
        estimate = _candidate(values, core)
        other_width = 8 if kind == "E" else 4
        alternative_useful = values[7] / (other_width * active)
        alternative_width_useful_errors[kind].append(
            abs(alternative_useful - interval["shares"]["useful"]) * 100
        )
        for category in CATEGORIES:
            difference = abs(estimate[category] - interval["shares"][category]) * 100
            maximum[category] = max(maximum[category], difference)
    guided_cycles = sum(row["cycles"] for row in guided["samples"])
    aggregated = _aggregate_windows(raw_by_scope, guided["samples"], guided_cycles)
    if not matched_count and not aggregated["cycle_aligned_windows"]:
        raise ValueError("No guided interval matches a raw counter sample or aggregate window")
    unmatched_intervals = Counter()
    unmatched_cycles = Counter()
    for row in guided["samples"]:
        time = row["time_ns"] + row["duration_ns"]
        if (time, row["tid"], row["core"], row["cycles"]) in matched_keys:
            continue
        reason = ("active_cycle_count_mismatch" if (time, row["tid"], row["core"]) in raw_scopes
                  else "raw_counter_scope_mismatch" if time in raw_times
                  else "no_raw_counter_sample_at_end_time")
        unmatched_intervals[reason] += 1
        unmatched_cycles[reason] += row["cycles"]
    return {"raw_samples_with_active_cycles": raw_count, "matched_intervals": matched_count,
            "unmatched_raw_samples": raw_count - matched_count,
            "guided_intervals": len(guided["samples"]),
            "unmatched_guided_intervals": len(guided["samples"]) - matched_count,
            "guided_sampled_cycles": guided_cycles, "matched_guided_cycles": matched_cycles,
            "matched_guided_cycle_fraction": matched_cycles / guided_cycles,
            "guided_threads_observed_on_both_core_kinds": sum(len(kinds) == 2 for kinds in thread_kinds.values()),
            "guided_core_kind_transitions_observed": sum(
                first[1] != second[1]
                for ordered in (sorted(samples) for samples in thread_samples.values())
                for first, second in zip(ordered, ordered[1:])
            ),
            "by_core_kind": {kind: {
                "guided_intervals": counts["intervals"],
                "guided_cycles": counts["cycles"],
                "matched_intervals": matched_by_kind[kind]["intervals"],
                "matched_guided_cycles": matched_by_kind[kind]["cycles"],
                "matched_guided_cycle_fraction": matched_by_kind[kind]["cycles"] / counts["cycles"],
            } for kind, counts in sorted(guided_by_kind.items())},
            "alternative_width_useful_error_by_core_kind": {
                kind: {"assumed_width": 8 if kind == "E" else 4,
                       "matched_intervals": len(errors),
                       "median_absolute_difference_percentage_points": median(errors),
                       "minimum_absolute_difference_percentage_points": min(errors)}
                for kind, errors in sorted(alternative_width_useful_errors.items())
            },
            "unmatched_guided_intervals_by_reason": dict(sorted(unmatched_intervals.items())),
            "unmatched_guided_cycles_by_reason": dict(sorted(unmatched_cycles.items())),
            "aggregated_raw_windows": aggregated,
            "max_absolute_difference_percentage_points": maximum,
            "matches_exported_rounding": matched_count > 0 and max(maximum.values()) < 1e-7}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counters", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    result = validate(args.counters, args.metrics, args.pid)
    print(json.dumps(result, indent=2))
    if not (result["matches_exported_rounding"] if result["matched_intervals"]
            else result["aggregated_raw_windows"]["matches_exported_rounding"]):
        raise SystemExit(1)
