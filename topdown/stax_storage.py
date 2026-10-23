"""Labeled Stax results, sampled hotspots and scheduler waits."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from topdown.apple_storage import validate_run_options
from topdown.apple_json import unicode_scalars
from topdown.collector.apple_runtime import check_cancelled
from topdown.stax_archive import StaxError, companion_metadata, load_archive
from topdown.storage.models import Run

BULK_FIELDS = {
    "samples",
    "intervals",
    "wakeups",
    "threads",
    "application_records",
    "frame_table",
    "stack_table",
    "target_lanes",
}


def store_stax_result(backend, path, process_name=None, labels=None, capture=None):
    validate_run_options(process_name, labels)
    document = load_archive(path)
    result, metadata = document["result"], document["metadata"]
    scope = result["scope"]
    if capture is None:
        capture, companion = companion_metadata(path)
        if companion:
            metadata["artifacts"].append(companion)
    if not isinstance(capture, dict):
        raise StaxError("Stax capture metadata must be an object")
    if capture:
        if (
            not isinstance(capture, dict)
            or capture.get("collector") != "stax"
            or capture.get("pid") != scope["target_pid"]
        ):
            raise StaxError("Stax capture metadata does not match the archive target")
        summaries = [capture[key] for key in ("run", "partial_run") if key in capture]
        if summaries and not any(
            isinstance(run, dict)
            and all(run.get(key) == scope.get(key) for key in ("id", "target_pid", "started_at_unix_ns"))
            for run in summaries
        ):
            raise StaxError("Stax capture metadata belongs to a different recording")
        if type(capture.get("frequency_hz")) is not int or not 1 <= capture["frequency_hz"] <= 10000:
            raise StaxError("Stax capture metadata requires a sampling frequency between 1 and 10000 Hz")
    if not unicode_scalars(capture or {}):
        raise StaxError("Stax capture metadata requires valid Unicode scalar values")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    started = epoch + timedelta(microseconds=scope["started_at_unix_ns"] // 1000)
    stopped_ns = scope.get("stopped_at_unix_ns")
    ended = epoch + timedelta(microseconds=stopped_ns // 1000) if stopped_ns is not None else None
    provenance = metadata["provenance"]
    all_labels = dict(labels or {})
    all_labels.update(
        collector="stax", measurement_kind=result["measurement_kind"], time_basis="archive_run_timestamps"
    )
    if capture:
        all_labels["sampling_frequency_hz"] = str(capture["frequency_hz"])
    for key in ("os", "arch"):
        if isinstance(provenance.get(key), str):
            all_labels[key] = provenance[key]
    command = (capture or {}).get("command")
    inferred = Path(command[0]).name if isinstance(command, list) and command and isinstance(command[0], str) else None
    label = scope["label"] if scope["label"] != "(attached)" else ""
    process_name = process_name or inferred or Path(label).name or f"pid:{scope.get('target_pid')}"
    validate_run_options(process_name, all_labels)
    run = Run(
        started_at=started,
        ended_at=ended,
        duration_seconds=(stopped_ns - scope["started_at_unix_ns"]) / 1e9 if stopped_ns is not None else 0,
        process_name=process_name,
        level=0,
        labels=all_labels,
    )
    metadata.update(
        result_schema_version=1,
        imported_at=datetime.now(timezone.utc).isoformat(),
        capture=capture or {},
        capture_settings_status="recorded" if capture else "unavailable",
    )
    document["schema_version"] = 1
    check_cancelled()
    backend.insert_stax_result(run, document)
    return run


def _document(backend, run_id, retain=()):
    run = backend.get_run(run_id)
    if run is None:
        raise StaxError(f"Run {run_id} not found")
    if run.labels.get("collector") != "stax":
        raise StaxError("This run is not a Stax sampling result")
    document = backend.get_stax_result_page(run_id, tuple(BULK_FIELDS - set(retain)))
    if document is None:
        raise StaxError("Stax document is missing for this run")
    return run, document


def _page_options(offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise StaxError("Require offset >= 0 and limit between 1 and 1000")


def _checked(response):
    if not unicode_scalars(response):
        raise StaxError("Stored Stax result contains invalid Unicode scalar values")
    return response


def query_stax_result(backend, run_id, section="summary", offset=0, limit=100):
    _page_options(offset, limit)
    if section not in {"summary", *BULK_FIELDS}:
        raise StaxError(
            "Choose summary, threads, samples, intervals, wakeups, frame_table, stack_table, target_lanes or application_records"
        )
    run = backend.get_run(run_id)
    if run is None or run.labels.get("collector") != "stax":
        _document(backend, run_id)
    expand = section in ("samples", "intervals", "wakeups")
    document = backend.get_stax_result_page(
        run_id,
        tuple(BULK_FIELDS - ({"frame_table", "stack_table"} if expand else set())),
        None if section == "summary" else section,
        offset,
        limit,
    )
    if document is None:
        raise StaxError("Stax document is missing for this run")
    result = document["result"]
    response = {
        "run_id": run_id,
        "process_name": run.process_name,
        "labels": run.labels,
        "metadata": document["metadata"],
        "result": {k: v for k, v in result.items() if k not in BULK_FIELDS},
        "counts": result["counts"],
    }
    if section != "summary":
        page = document["rows"] or []
        if expand:
            frames = {frame["address"]: frame for frame in result["frame_table"]}
            page = [_expand(row, frames, result["stack_table"]) for row in page]
        response.update(section=section, offset=offset, limit=limit, total=document["total"], rows=page)
    return _checked(response)


def _location(frame):
    return (
        frame["binary"],
        frame["symbol_address"] if frame["symbol_address"] is not None else frame["address"],
        frame["name"],
    )


def _expand(row, frames, stacks):
    row = dict(row)
    for field, expanded in (
        ("stack", "frames"),
        ("kernel_stack", "kernel_frames"),
        ("waker_user_stack", "waker_frames"),
        ("waker_kernel_stack", "waker_kernel_frames"),
    ):
        if field + "_id" in row:
            row[field] = stacks[row[field + "_id"]]
            row[expanded] = [frames[address] for address in row[field]]
    return row


def get_stax_hotspots(backend, run_id, tid=None, stack="user", sort="self", limit=20):
    _page_options(0, limit)
    if tid is not None and (type(tid) is not int or not 0 <= tid < 2**32):
        raise StaxError("Require an unsigned 32-bit thread ID")
    if stack not in ("user", "kernel") or sort not in ("self", "total"):
        raise StaxError("Choose user/kernel stacks and self/total ranking")
    _, document = _document(backend, run_id, ("samples", "frame_table", "stack_table"))
    samples = [
        s for s in document["result"]["samples"] if s["kind"] == "cpu-stack" and (tid is None or s["tid"] == tid)
    ]
    frame_table = {frame["address"]: frame for frame in document["result"]["frame_table"]}
    functions = {}
    missing_stacks = 0
    for sample in samples:
        check_cancelled()
        addresses = document["result"]["stack_table"][sample["stack_id" if stack == "user" else "kernel_stack_id"]]
        frames = [frame_table[address] for address in addresses]
        if not frames:
            missing_stacks += 1
            continue
        seen = set()
        for index, frame in enumerate(frames):
            key = _location(frame)
            row = functions.setdefault(key, {**frame, "self_samples": 0, "total_samples": 0})
            if index == 0:
                row["self_samples"] += 1
            if key not in seen:
                row["total_samples"] += 1
                seen.add(key)
    rows = list(functions.values())
    for row in rows:
        row["self_sample_share"] = row["self_samples"] / len(samples)
        row["total_sample_share"] = row["total_samples"] / len(samples)
    rows.sort(key=lambda row: (-row[sort + "_samples"], row["binary"] or "", row["address"]))
    return _checked(
        {
            "run_id": run_id,
            "collector": "stax",
            "unit": "samples",
            "stack": stack,
            "tid": tid,
            "denominator_samples": len(samples),
            "samples_without_stack": missing_stacks,
            "total_functions": len(rows),
            "rows": rows[:limit],
            "limitations": document["result"]["limitations"],
        }
    )


def get_stax_thread_waits(backend, run_id, tid=None, limit=20):
    _page_options(0, limit)
    if tid is not None and (type(tid) is not int or not 0 <= tid < 2**32):
        raise StaxError("Require an unsigned 32-bit thread ID")
    _, document = _document(backend, run_id, ("threads", "intervals", "frame_table", "stack_table"))
    result = document["result"]
    frame_table = {frame["address"]: frame for frame in result["frame_table"]}
    waits = defaultdict(lambda: {"interval_count": 0, "duration_ns": 0})
    for interval in result["intervals"]:
        check_cancelled()
        if interval["kind"] != "OffCpu" or tid is not None and interval["tid"] != tid:
            continue
        addresses = result["stack_table"][interval["stack_id"]] if "stack_id" in interval else []
        frames = [frame_table[address] for address in addresses]
        key = (interval["tid"], tuple(_location(f) for f in frames), interval.get("waker_tid"))
        row = waits[key]
        row.update(tid=interval["tid"], frames=frames, waker_tid=interval.get("waker_tid"))
        row["interval_count"] += 1
        row["duration_ns"] += interval["duration_ns"]
    rows = sorted(waits.values(), key=lambda row: (-row["duration_ns"], row["tid"]))
    threads = sorted(
        (t for t in result["threads"] if tid is None or t["tid"] == tid),
        key=lambda thread: (-thread.get("off_cpu_interval_ns", 0), thread["tid"]),
    )
    return _checked(
        {
            "run_id": run_id,
            "collector": "stax",
            "unit": "ns",
            "tid": tid,
            "threads": threads[:limit],
            "total_threads": len(threads),
            "total_wait_stacks": len(rows),
            "rows": rows[:limit],
            "limitations": result["limitations"],
        }
    )


def get_stax_target_spans(backend, run_id, tid=None, limit=20):
    _page_options(0, limit)
    if tid is not None and (type(tid) is not int or not 0 <= tid < 2**32):
        raise StaxError("Require an unsigned 32-bit thread ID")
    _, document = _document(backend, run_id, ("threads", "intervals", "frame_table", "stack_table", "target_lanes"))
    result = document["result"]
    frames = {frame["address"]: frame for frame in result["frame_table"]}
    spans = {}
    for interval in result["intervals"]:
        check_cancelled()
        if interval["kind"] != "SyntheticSpan" or tid is not None and interval["tid"] != tid:
            continue
        stack = [frames[address] for address in result["stack_table"][interval["stack_id"]]]
        leaf = stack[0] if stack else None
        key = (interval["tid"], interval["lane_kind"], _location(leaf) if leaf is not None else None)
        duration = interval["duration_ns"]
        row = spans.setdefault(
            key,
            {
                "tid": interval["tid"],
                "lane_kind": interval["lane_kind"],
                "leaf": leaf,
                "span_count": 0,
                "sum_target_span_ns": 0,
                "min_span_ns": duration,
                "max_span_ns": duration,
                "origin_tids": set(),
            },
        )
        row["span_count"] += 1
        row["sum_target_span_ns"] += duration
        row["min_span_ns"] = min(row["min_span_ns"], duration)
        row["max_span_ns"] = max(row["max_span_ns"], duration)
        if interval.get("origin_tid") is not None:
            row["origin_tids"].add(interval["origin_tid"])
    rows = sorted(spans.values(), key=lambda row: (-row["sum_target_span_ns"], row["tid"]))
    for row in rows:
        row["origin_thread_count"] = len(row["origin_tids"])
        row["origin_tids"] = sorted(row["origin_tids"])[:limit]
    return _checked(
        {
            "run_id": run_id,
            "collector": "stax",
            "unit": "ns",
            "tid": tid,
            "clock": "producer-supplied target span timestamps",
            "total_target_functions": len(rows),
            "rows": rows[:limit],
            "target_ingest": result["target_ingest"],
            "lanes": [lane for lane in result["target_lanes"] if tid is None or lane["tid"] == tid][:limit],
            "total_lanes": sum(tid is None or lane["tid"] == tid for lane in result["target_lanes"]),
            "limitations": [
                *result["limitations"],
                "Target span durations can overlap; their sum is not GPU utilization.",
                "A Metal lane label alone does not establish hardware timestamp provenance.",
            ],
        }
    )
