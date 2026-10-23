"""Store native Apple artifacts without converting their values into Linux TMA samples."""

import hashlib
import json
import math
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from topdown.collector.apple import (APPLE_BOTTLENECKS, FIXED_EVENTS, M1_EVENTS, M1_FETCH_EVENTS,
                                     M1_MAP_EVENTS, MODES, TMA_ROOTS, AppleProfileError)
from topdown.storage.models import Run
from topdown.collector.apple_files import decode_json_object, read_regular_bytes

PROFILE_SCHEMAS = {kind: schema for _, schema, kind in MODES.values()}
PROFILE_SCHEMAS["apple-guided-bottleneck-intervals"] = "MetricTable"
STAT_KIND = "process-accounted-fixed-counters"
SOC_KIND = "native-soc-firmware-state-observations"
BULK_FIELDS = {"samples", "intervals", "gaps", "channels", "leaf_sample_counts", "encoders"}
EVENT_GROUPS = {MODES[mode][2]: events for mode, events in
                [("fixed", FIXED_EVENTS), ("m1", M1_EVENTS), ("m1-map", M1_MAP_EVENTS), ("m1-fetch", M1_FETCH_EVENTS)]}


def _uint(value):
    return type(value) is int and 0 <= value < 2**64


def _nonnegative(value):
    return type(value) is int and value >= 0


def _shares_valid(shares):
    return (isinstance(shares, dict) and set(shares) == set(APPLE_BOTTLENECKS)
            and all(type(v) in (int, float) and 0 <= v <= 1 and math.isfinite(v) for v in shares.values())
            and abs(sum(shares.values()) - 1) <= 0.01)


def _validate_frames(frames):
    if (not isinstance(frames, list) or any(not isinstance(frame, dict)
            or any(frame.get(key) is not None and not isinstance(frame[key], str)
                   for key in ["name", "address", "binary", "binary_uuid", "load_address"]) for frame in frames)):
        raise AppleProfileError("Require native stack frames with string or null code locations")


def _validate_observation(row, scope, time_field):
    if (not isinstance(row, dict) or type(row.get("pid")) is not int or row["pid"] != scope["pid"]
            or not _uint(row.get("tid")) or not _uint(row.get(time_field))
            or row[time_field] < scope["start_ns"]
            or (scope["end_ns"] is not None and row[time_field] >= scope["end_ns"])):
        raise AppleProfileError("Native observation disagrees with profile PID/time scope")


def _validate_guided_summary(summary, rows):
    cycles = sum(row["cycles"] for row in rows)
    if (not isinstance(summary, dict) or not _shares_valid(summary.get("shares"))
            or not _nonnegative(summary.get("cycles")) or summary["cycles"] != cycles
            or type(summary.get("sample_count")) is not int or summary["sample_count"] != len(rows)
            or summary.get("weighting") != "sampled_core_cycles"):
        raise AppleProfileError("Guided bottleneck summary must match its native samples")
    for event in APPLE_BOTTLENECKS:
        expected = sum(row["cycles"] * row["shares"][event] for row in rows) / cycles
        if not math.isclose(summary["shares"][event], expected, rel_tol=1e-12, abs_tol=1e-12):
            raise AppleProfileError("Guided bottleneck shares must match their cycle-weighted samples")


def _validate_guided_coverage(result, rows):
    if "apple_bottlenecks_available" in result and result["apple_bottlenecks_available"] is not True:
        raise AppleProfileError("Guided bottleneck availability must agree with its valid native samples")
    fields = {"invalid_cycles_with_known_count", "invalid_intervals_without_cycle_count",
              "valid_cycle_fraction_of_scoped_intervals"}
    if not fields.intersection(result):
        return
    if not fields.issubset(result):
        raise AppleProfileError("Guided cycle coverage requires complete native invalid-interval accounting")
    invalid_cycles, unknown = result["invalid_cycles_with_known_count"], result["invalid_intervals_without_cycle_count"]
    invalid = result.get("invalid_intervals", 0)
    if (not _nonnegative(invalid_cycles) or not _nonnegative(unknown) or unknown > invalid
            or invalid_cycles > (invalid - unknown) * (2**64 - 1)):
        raise AppleProfileError("Guided cycle coverage counts disagree with its invalid native intervals")
    fraction = result["valid_cycle_fraction_of_scoped_intervals"]
    if unknown:
        valid = fraction is None
    else:
        cycles = sum(row["cycles"] for row in rows)
        valid = (type(fraction) in (int, float) and 0 <= fraction <= 1
                 and math.isclose(fraction, cycles / (cycles + invalid_cycles), rel_tol=1e-12, abs_tol=1e-12))
    if not valid:
        raise AppleProfileError("Guided cycle coverage must match native cycles; unknown cycle counts require null coverage")


def _validate_stat_counters(counters, timebase=None, interval=False):
    if (not isinstance(counters, dict) or not {"instructions", "cycles"}.issubset(counters)
            or not all(_uint(value) for value in counters.values())):
        raise AppleProfileError("Require observed unsigned process counters")
    for total in ["instructions", "cycles", "user_time_ns", "system_time_ns", "user_time_mach", "system_time_mach"]:
        performance, other = "performance_" + total, "other_" + total
        if performance in counters or other in counters:
            if performance not in counters or other not in counters or total not in counters:
                raise AppleProfileError("Performance/other core pairs require their complete aggregate total")
            difference = counters[total] - counters[performance] - counters[other]
            quantized = (total.endswith("_ns") and total[:-3] + "_mach" in counters
                         and (-1 if interval else 0) <= difference <= 1)
            if difference and not quantized:
                raise AppleProfileError("Performance/other core pairs must sum exactly to their aggregate total")
    for stem in ("user_time", "system_time"):
        if not any(prefix + stem + "_mach" in counters for prefix in ("", "performance_", "other_")):
            continue
        if (not isinstance(timebase, dict)
                or any(type(timebase.get(key)) is not int or not 1 <= timebase[key] < 2**32 for key in ("numer", "denom"))):
            raise AppleProfileError("Native Mach CPU times require a positive uint32 timebase")
        for prefix in ("", "performance_", "other_"):
            raw, ns = prefix + stem + "_mach", prefix + stem + "_ns"
            if raw not in counters and ns not in counters:
                continue
            if raw not in counters or ns not in counters:
                raise AppleProfileError("Require matching native Mach and converted nanosecond CPU times")
            scaled = counters[raw] * timebase["numer"]
            floor, remainder = divmod(scaled, timebase["denom"])
            if not floor <= counters[ns] <= floor + (1 if interval and remainder else 0):
                raise AppleProfileError("Converted CPU times must match native Mach counters and their rounding")


def _validate_stat(result):
    counters, scope = result.get("counters"), result.get("scope")
    source = result.get("source")
    timebase = source.get("mach_timebase") if isinstance(source, dict) else None
    interval = isinstance(scope, dict) and scope.get("window") == "observed-interval"
    _validate_stat_counters(counters, timebase, interval)
    if not isinstance(scope, dict):
        raise AppleProfileError("Process counters require an explicit positive PID")
    from topdown.collector.apple_stat import _validate_pid

    _validate_pid(scope.get("pid"))
    for key, allowed in [("descendants", (False,)), ("threads", ("all",)),
                         ("execution_levels", ("user-and-kernel",)),
                         ("window", ("process-lifetime", "observed-interval"))]:
        if key in scope and (scope[key] not in allowed or (key == "descendants" and scope[key] is not False)):
            raise AppleProfileError("Process scope must retain all threads, user/kernel execution and no descendants")
    measured = counters if scope.get("window") == "process-lifetime" else None
    if "snapshot_before" in result or "snapshot_after" in result:
        if scope.get("window") != "observed-interval":
            raise AppleProfileError("Process snapshots require observed-interval scope")
        before, after = result.get("snapshot_before"), result.get("snapshot_after")
        _validate_stat_counters(before, timebase)
        _validate_stat_counters(after, timebase)
        if (set(before) != set(after) or not before.get("process_start_mach")
                or before["process_start_mach"] != after.get("process_start_mach")
                or before.get("process_exit_mach") != 0 or "process_exit_mach" not in after):
            raise AppleProfileError("Process snapshots must retain one live process identity and counter layout")
        if after["process_exit_mach"] and after["process_exit_mach"] < after["process_start_mach"]:
            raise AppleProfileError("Process snapshot exit precedes its start")
        fields = set(before) - {"process_start_mach", "process_exit_mach"}
        if set(counters) != fields or any(after[key] - before[key] != counters[key] for key in fields):
            raise AppleProfileError("Process interval counters must match their native snapshot differences")
        measured = after
    if "observed_duration_ns" in result and (not _nonnegative(result["observed_duration_ns"]) or not result["observed_duration_ns"]):
        raise AppleProfileError("Process observation duration must be a positive native nanosecond count")
    if "counters_available" in result:
        available = result["counters_available"]
        if (type(available) is not bool or (measured is not None
                and available is not (measured["instructions"] > 0 and measured["cycles"] > 0))):
            raise AppleProfileError("Process counter availability disagrees with native observations")


def _validate_soc(result):
    channels = result.get("channels")
    if not isinstance(channels, list) or not channels or not all(isinstance(c, dict) for c in channels):
        raise AppleProfileError("Require native SoC channel observations")
    if not isinstance(result.get("scope"), str) or not result["scope"].startswith("system-and-cluster"):
        raise AppleProfileError("SoC observations require explicit system-and-cluster scope without PID attribution")
    if "channel_count" in result and (type(result["channel_count"]) is not int or result["channel_count"] != len(channels)):
        raise AppleProfileError("SoC channel count must match its native channels")
    identities, totals = set(), []
    for channel in channels:
        if (not all(isinstance(channel.get(key), str) for key in ["group", "subgroup", "name", "unit_label"])
                or not channel["group"] or not channel["name"]
                or not _uint(channel.get("id")) or not _uint(channel.get("unit"))):
            raise AppleProfileError("Require native SoC channel identities, units and labels")
        identity = channel["group"], channel["subgroup"], channel["id"]
        if identity in identities:
            raise AppleProfileError("Duplicate native SoC channel identity")
        identities.add(identity)
        states = channel.get("states")
        if (not isinstance(states, list) or not states
                or not all(isinstance(s, dict) and _uint(s.get("id")) and isinstance(s.get("label"), str)
                           and _uint(s.get("value")) and _uint(s.get("intransitions")) for s in states)):
            raise AppleProfileError("Require native SoC state identities, labels and unsigned values/transitions")
        if len({s["id"] for s in states}) != len(states):
            raise AppleProfileError("Duplicate native SoC state identity")
        total = sum(s["value"] for s in states)
        totals.append(total)
        if "native_value_total" in channel and (type(channel["native_value_total"]) is not int or channel["native_value_total"] != total):
            raise AppleProfileError("SoC channel total must match its native state values")
        if "observations_available" in channel and channel["observations_available"] is not (total > 0):
            raise AppleProfileError("SoC observation availability must match its native state values")
        for state in states:
            if "share_of_reported_values" not in state:
                continue
            share = state["share_of_reported_values"]
            if not total:
                valid = share is None
            else:
                valid = (type(share) in (int, float) and 0 <= share <= 1
                         and math.isclose(share, state["value"] / total, rel_tol=1e-12, abs_tol=1e-12))
            if not valid:
                raise AppleProfileError("SoC state shares must match native values; unobserved states require null shares")
    if "observations_available" in result and result["observations_available"] is not any(totals):
        raise AppleProfileError("SoC observation availability must match its native channels")


def _validate_scheduling(interval):
    scheduling = interval.get("scheduling")
    if not isinstance(scheduling, dict):
        raise AppleProfileError("Require native scheduling annotations for every decoded interval")
    duration = interval["duration_ns"]
    covered, uncovered, states = scheduling.get("covered_ns"), scheduling.get("uncovered_ns"), scheduling.get("states_ns")
    if (not _uint(covered) or not _uint(uncovered) or covered + uncovered != duration
            or not isinstance(states, dict) or not all(isinstance(k, str) and k and _uint(v) for k, v in states.items())
            or sum(states.values()) != covered):
        raise AppleProfileError("Scheduling states and coverage must agree with their decoded interval duration")
    complete = duration > 0 and covered == duration
    if scheduling.get("coverage_complete") is not complete:
        raise AppleProfileError("Scheduling completeness must agree with its observed time coverage")
    cores = scheduling.get("running_cores")
    if (not isinstance(cores, list) or not all(isinstance(core, dict) and _uint(core.get("core"))
            and (core.get("core_label") is None or isinstance(core["core_label"], str))
            and _uint(core.get("duration_ns")) and core["duration_ns"] > 0 for core in cores)):
        raise AppleProfileError("Require native running-core identities and positive observed durations")
    identities = {(core["core"], core.get("core_label")) for core in cores}
    if len(identities) != len(cores) or sum(core["duration_ns"] for core in cores) > states.get("Running", 0):
        raise AppleProfileError("Running-core durations must not duplicate or exceed observed Running time")
    expected = None
    if complete and len(cores) == 1 and cores[0]["duration_ns"] == duration:
        expected = {"core": cores[0]["core"], "core_label": cores[0].get("core_label")}
    single = scheduling.get("single_running_core")
    if single is not None and (not isinstance(single, dict) or not _uint(single.get("core"))):
        raise AppleProfileError("Require a native single-running-core identity")
    if single != expected:
        raise AppleProfileError("A single running core requires complete observed Running coverage on that core")
    return expected is not None


def _validate_trace_context(result):
    gaps_by_thread = defaultdict(list)
    for gap in result["gaps"]:
        gaps_by_thread[gap["tid"]].append(gap["time_ns"])
    for times in gaps_by_thread.values():
        times.sort()
    if ("target_gap_count_in_run" in result and (not _nonnegative(result["target_gap_count_in_run"])
            or result["target_gap_count_in_run"] < len(result["gaps"]))):
        raise AppleProfileError("Total native trace gap count cannot be less than the gaps in scope")
    annotated = any("scheduling" in interval for interval in result["intervals"])
    available = result.get("scheduling_available", annotated)
    if type(available) is not bool or (annotated and not available):
        raise AppleProfileError("Scheduling availability disagrees with its native interval annotations")
    singles = 0
    for interval in result["intervals"]:
        start, end = interval["start_ns"], interval["start_ns"] + interval["duration_ns"]
        times = gaps_by_thread[interval["tid"]]
        if interval["gap_markers_inside"] != bisect_left(times, end) - bisect_left(times, start):
            raise AppleProfileError("Trace interval gap counts must match native markers for that thread and time span")
        if available:
            singles += _validate_scheduling(interval)
    if "single_running_core_intervals" in result and (type(result["single_running_core_intervals"]) is not int
            or result["single_running_core_intervals"] != singles):
        raise AppleProfileError("Single-running-core summary must match its decoded interval annotations")
    for key in ["thread_state_rows", "thread_state_other_pid_rows"]:
        if key in result and not _nonnegative(result[key]):
            raise AppleProfileError("Native thread-state row counts must be nonnegative integers")


def _validate_profile(result, rows):
    scope = result.get("scope")
    if (not isinstance(scope, dict) or not _uint(scope.get("pid")) or not scope["pid"]
            or not _nonnegative(scope.get("start_ns")) or "end_ns" not in scope
            or (scope["end_ns"] is not None and (not _nonnegative(scope["end_ns"]) or scope["end_ns"] <= scope["start_ns"]))):
        raise AppleProfileError("Require an explicit positive PID and nonempty native time scope")
    for field in ["excluded_rows", "unmeasured_rows", "boundary_rows_excluded", "invalid_intervals"]:
        if not _nonnegative(result.get(field, 0)):
            raise AppleProfileError(f"Invalid native {field}")
    schema, kind = result["schema"], result["measurement_kind"]
    if schema == "counters-profile":
        events = EVENT_GROUPS[kind]
        if result.get("configured_events") != list(events) or result.get("event_value_order_verified") is not (events == FIXED_EVENTS):
            raise AppleProfileError("Require the native event configuration and verified mapping status")
    for row in rows:
        _validate_observation(row, scope, "start_ns" if schema == "processor-trace-intervals" else "time_ns")
        if schema == "processor-trace-intervals":
            if (not row["tid"] or not all(_uint(row.get(key)) for key in
                    ["pid", "tid", "start_ns", "duration_ns", "depth", "self_instructions", "self_cycles", "gap_markers_inside"])
                    or row["start_ns"] + row["duration_ns"] >= 2**64
                    or (scope["end_ns"] is not None and row["start_ns"] + row["duration_ns"] > scope["end_ns"])
                    or (row.get("self_duration_ns") is not None and
                        (not _uint(row["self_duration_ns"]) or row["self_duration_ns"] > row["duration_ns"]))
                    or (row.get("binary_offset") is not None and not _uint(row["binary_offset"]))):
                raise AppleProfileError("Require complete native trace intervals and unsigned self counters")
            _validate_frames(row.get("frames"))
        elif schema == "MetricTable":
            if (not _uint(row.get("core")) or not _uint(row.get("duration_ns")) or not row["duration_ns"]
                    or not _uint(row.get("cycles")) or not row["cycles"] or not _shares_valid(row.get("shares"))):
                raise AppleProfileError("Require native guided intervals, cycles and four bottleneck shares")
        else:
            if not _uint(row.get("core")):
                raise AppleProfileError("Require a native core identity")
            if schema == "cpu-pmc-value":
                if (not isinstance(row.get("event"), str) or not row["event"].strip()
                        or not _uint(row.get("value")) or not _uint(row.get("sample_index"))):
                    raise AppleProfileError("Require native event names, unsigned PMC values and sample indices")
            else:
                if not _uint(row.get("weight")):
                    raise AppleProfileError("Require a native integer sample weight")
                _validate_frames(row.get("frames"))
                if schema == "counters-profile":
                    values = row.get("event_values") if events == FIXED_EVENTS else row.get("native_event_values")
                    if events == FIXED_EVENTS:
                        valid = isinstance(values, dict) and set(values) == set(events) and all(_uint(v) for v in values.values())
                    else:
                        valid = isinstance(values, list) and len(values) == len(events) and all(_uint(v) for v in values)
                    if not valid:
                        raise AppleProfileError("Require one native uint64 value per configured event")
    if schema == "processor-trace-intervals":
        for gap in result["gaps"]:
            _validate_observation(gap, scope, "time_ns")
            if (not gap["tid"] or not all(_uint(gap.get(key)) for key in ["pid", "tid", "time_ns"])
                    or (gap.get("description") is not None and not isinstance(gap["description"], str))):
                raise AppleProfileError("Require native trace gap identities and descriptions")
        _validate_trace_context(result)
    if schema == "MetricTable":
        _validate_guided_coverage(result, rows)
        _validate_guided_summary(result.get("apple_bottlenecks"), rows)
        by_core = result.get("apple_bottlenecks_by_core")
        if (not isinstance(by_core, list) or not all(isinstance(entry, dict) and _nonnegative(entry.get("core")) for entry in by_core)
                or len(by_core) != len({row["core"] for row in rows})
                or {entry["core"] for entry in by_core} != {row["core"] for row in rows}):
            raise AppleProfileError("Require one native guided summary per observed core")
        for entry in by_core:
            _validate_guided_summary(entry, [row for row in rows if row["core"] == entry["core"]])


def validate_result(result: dict) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("measurement_kind"), str):
        raise AppleProfileError("Require a native Apple profile.json, stat.json or soc.json result")
    gpu = result["measurement_kind"] == "apple-gpu-replay-profile"
    if result.get("tma_available") is not False or result.get("tma", dict.fromkeys(TMA_ROOTS) if gpu else None) != dict.fromkeys(TMA_ROOTS):
        raise AppleProfileError("Apple results must retain unavailable Intel TMA fields as null")
    kind = result["measurement_kind"]
    if gpu:
        from topdown.apple_gpu_report import summarize

        try:
            summarize(result)
        except ValueError as exc:
            raise AppleProfileError(str(exc)) from exc
    elif kind == STAT_KIND:
        _validate_stat(result)
    elif kind == SOC_KIND:
        _validate_soc(result)
    elif kind in PROFILE_SCHEMAS:
        if result.get("schema") != PROFILE_SCHEMAS[kind]:
            raise AppleProfileError("Apple measurement kind and schema disagree")
        trace = result["schema"] == "processor-trace-intervals"
        field, count = ("intervals", "interval_count") if trace else ("samples", "sample_count")
        rows = result.get(field)
        if (not isinstance(rows, list) or not rows or type(result.get(count)) is not int
                or result[count] != len(rows) or not all(isinstance(row, dict) for row in rows)):
            raise AppleProfileError(f"Require native {field} and a matching {count}")
        if trace:
            if (not isinstance(result.get("gaps"), list) or type(result.get("gap_count")) is not int
                    or result["gap_count"] != len(result["gaps"]) or result.get("coverage_complete") is not False):
                raise AppleProfileError("Trace results must retain their gaps and incomplete decoding status")
        _validate_profile(result, rows)
    else:
        raise AppleProfileError(f"Unsupported Apple measurement kind: {kind}")
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise AppleProfileError(f"Invalid native Apple JSON: {exc}") from exc


def _read_json(path):
    try:
        data = read_regular_bytes(path)
        value = decode_json_object(data)
    except (OSError, ValueError) as exc:
        raise AppleProfileError(f"Cannot read Apple artifact {path}: {exc}") from exc
    return value, hashlib.sha256(data).hexdigest()


def _date(value):
    try:
        if isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            raise ValueError("timezone required")
        return date.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise AppleProfileError("Apple artifact timestamps must be timezone-aware ISO dates") from exc


def _timestamps(document):
    if "ended_at" in document and "started_at" not in document:
        raise AppleProfileError("Apple artifact end timestamp requires a start timestamp")
    started = _date(document["started_at"]) if "started_at" in document else None
    ended = _date(document["ended_at"]) if "ended_at" in document else None
    if ended is not None and ended < started:
        raise AppleProfileError("Apple artifact ends before it starts")
    return started, ended


def validate_run_options(process_name: str | None, labels: dict | None) -> None:
    if labels is not None and (not isinstance(labels, dict)
                              or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items())):
        raise AppleProfileError("Labels must map strings to strings")
    if process_name is not None and (not isinstance(process_name, str) or not process_name.strip()):
        raise AppleProfileError("Require a nonempty process name")


def store_apple_result(backend, path: Path, process_name: str | None = None, labels: dict | None = None) -> Run:
    """Import one artifact as a labeled run, retaining its complete native document."""
    validate_run_options(process_name, labels)
    if path.is_dir():
        candidates = [p for name in ["profile.json", "stat.json", "soc.json"] if (p := path / name).is_file()]
        if len(candidates) != 1:
            raise AppleProfileError("Choose one profile.json, stat.json or soc.json file explicitly")
        path = candidates[0]
    result, source_sha256 = _read_json(path)
    validate_result(result)
    capture_path = path.parent / "capture.json"
    capture = _read_json(capture_path)[0] if path.name == "profile.json" and capture_path.is_file() else {}
    imported = datetime.now(timezone.utc)
    result_started, result_ended = _timestamps(result)
    capture_started, capture_ended = _timestamps(capture)
    if result_started is not None:
        started, ended, time_basis = result_started, result_ended, "result_timestamps"
    elif capture_started is not None:
        started, ended, time_basis = capture_started, capture_ended, "capture_metadata"
    else:
        started, ended, time_basis = imported, None, "import_time"
    command = result.get("command") or capture.get("command")
    scope = result.get("scope", {})
    pid = scope.get("pid") if isinstance(scope, dict) else None
    system_wide = result["measurement_kind"] == SOC_KIND
    if not process_name:
        if isinstance(command, list) and command and isinstance(command[0], str):
            process_name = Path(command[0]).name
        else:
            process_name = "system" if system_wide else f"pid:{pid}" if pid else "apple-profile"
    environment = result.get("environment") or capture.get("environment") or {}
    all_labels = dict(labels or {})
    if isinstance(environment, dict):
        for key in ["cpu", "arch", "os_version", "os_build", "kernel_uuid", "xctrace"]:
            if isinstance(environment.get(key), str):
                all_labels[key] = environment[key]
    all_labels.update(collector="apple", measurement_kind=result["measurement_kind"], time_basis=time_basis)
    if result["measurement_kind"] == "apple-gpu-replay-profile":
        all_labels.update(processor="gpu", device=result["device"])
    run = Run(started_at=started, ended_at=ended, duration_seconds=(ended - started).total_seconds() if ended else 0,
              process_name=process_name, level=0, system_wide=system_wide, labels=all_labels)
    metadata = {"imported_at": imported.isoformat(), "source_sha256": source_sha256, "time_basis": time_basis,
                "source_filename": path.name, "capture": capture}
    from topdown.collector.apple_runtime import check_cancelled

    check_cancelled()
    backend.insert_apple_result(run, {"schema_version": 1, "result": result, "metadata": metadata})
    return run


def query_apple_result(backend, run_id: str, section: str = "summary", offset: int = 0, limit: int = 100) -> dict:
    if section not in {"summary", *BULK_FIELDS} or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise AppleProfileError("Choose summary/samples/intervals/gaps/channels/leaf_sample_counts/encoders, offset >= 0 and limit 1–1000")
    run = backend.get_run(run_id)
    document = backend.get_apple_result_page(run_id, tuple(sorted(BULK_FIELDS)),
                                             None if section == "summary" else section, offset, limit)
    if run is None or document is None:
        raise AppleProfileError(f"No stored Apple result for run {run_id}")
    result = document["result"]
    response = {"run_id": run_id, "process_name": run.process_name, "labels": run.labels,
                "metadata": document["metadata"], "result": {k: v for k, v in result.items() if k not in BULK_FIELDS}}
    if section != "summary":
        rows = document["rows"]
        if not isinstance(rows, list):
            raise AppleProfileError(f"This Apple result has no {section} section")
        response.update(section=section, offset=offset, limit=limit, total=document["total"], rows=rows)
    return response


def stored_apple_hotspots(backend, run_id: str, limit: int = 20) -> dict:
    from topdown.apple_report import summarize_hotspots

    document = backend.get_apple_result(run_id)
    if document is None:
        raise AppleProfileError(f"No stored Apple result for run {run_id}")
    return {"run_id": run_id, **summarize_hotspots(document["result"], limit)}


def stored_apple_gpu_report(backend, run_id: str, limit: int = 20, offset: int = 0) -> dict:
    from topdown.apple_gpu_report import summarize

    document = backend.get_apple_result(run_id)
    if document is None:
        raise AppleProfileError(f"No stored Apple result for run {run_id}")
    try:
        return {"run_id": run_id, **summarize(document["result"], limit, offset)}
    except ValueError as exc:
        raise AppleProfileError(str(exc)) from exc


def collect_apple_result(output: Path, mode: str, command: list[str] | None, pid: int | None,
                         duration_seconds: float) -> Path:
    """Reuse bounded native collectors; retain artifacts even when capture or DB storage fails."""
    if type(duration_seconds) not in (int, float) or not 0 < duration_seconds <= 300 or not math.isfinite(duration_seconds):
        raise AppleProfileError("Require a finite duration greater than zero and at most 300 seconds")
    if command is not None and (not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command)):
        raise AppleProfileError("Command must be a nonempty executable argv list")
    if pid is not None:
        from topdown.collector.apple_stat import _validate_pid

        _validate_pid(pid)
    if mode == "soc":
        if command is not None or pid is not None:
            raise AppleProfileError("SoC observations cover the system/cluster and do not accept a command or PID")
        from topdown.collector.apple_soc import sample_soc

        result = sample_soc(output, duration_seconds)
        name = "soc.json"
    elif mode == "stat":
        if (command is None) == (pid is None):
            raise AppleProfileError("Stat requires either an executable command or an existing PID")
        from topdown.collector.apple_stat import stat_pid, stat_process

        result = stat_pid(output, pid, duration_seconds) if pid is not None else stat_process(output, command, duration_seconds)
        name = "stat.json"
    elif mode in MODES:
        if pid is not None or command is None:
            raise AppleProfileError("Stack/trace recording requires an executable command; use stat for an existing PID")
        if duration_seconds < 1 or int(duration_seconds) != duration_seconds:
            raise AppleProfileError("Instruments recording requires a whole number of seconds, at least one")
        from topdown.collector.apple import record_profile

        record_profile(output, command, int(duration_seconds), mode)
        return output / "profile.json"
    else:
        raise AppleProfileError("Choose stat, soc, fixed, counters, cpu, time, trace, m1, m1-map or m1-fetch")
    if result.get("exit_code") != 0:
        raise AppleProfileError(f"Apple capture {result.get('status', 'failed')}: {result.get('error', '')}. Artifacts: {output}")
    return output / name
