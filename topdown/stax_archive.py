"""Read pinned Stax archives without interpreting samples as TMA measurements."""

from bisect import bisect_right
from collections import Counter
from contextlib import contextmanager
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import stat

from topdown.collector.apple_files import NativeJSONDecoder
from topdown.collector.apple_runtime import check_cancelled
from topdown.apple_json import unicode_scalars

SUPPORTED_ARCHIVE_VERSIONS = (3,)
UPSTREAM_REVISION = "aedd85e08894fc3250ec975a486544d091e11281"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class StaxError(ValueError):
    """An unavailable capability, invalid archive, or failed Stax capture."""


def uint(value, name, bits=64):
    if type(value) is not int or not 0 <= value < 2**bits:
        raise StaxError(f"{name} must be an unsigned {bits}-bit integer")
    return value


def _object(value, name):
    if not isinstance(value, dict):
        raise StaxError(f"{name} must be an object")
    return value


def _array(value, name):
    if not isinstance(value, list):
        raise StaxError(f"{name} must be an array")
    return value


def _text(value, name):
    if not isinstance(value, str):
        raise StaxError(f"{name} must be a string")
    return value


@contextmanager
def _open(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise StaxError(f"Require a regular Stax artifact: {path}")
        if info.st_size > MAX_ARTIFACT_BYTES:
            raise StaxError(
                f"Stax artifact exceeds {MAX_ARTIFACT_BYTES} bytes; use a directory archive or shorter capture: {path}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _read(path):
    with _open(path) as stream:
        data = stream.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise StaxError("Stax artifact grew beyond the import limit")
    check_cancelled()
    return data


def _decode(data):
    try:
        return json.loads(data, cls=NativeJSONDecoder)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StaxError(f"Invalid Stax JSON: {exc}") from exc


def _member(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise StaxError("Archive members must use relative paths")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise StaxError("Archive member escapes its directory")
    return path


def _replay(events, fallback_runs):
    archive = {
        "runs": [],
        "aggregator": {"thread_names": [], "threads": []},
        "binaries": {"binaries": []},
        "target_ingest": {},
        "observability": {},
        "application_records": [],
    }
    threads, names = {}, {}
    for event in events:
        check_cancelled()
        if not isinstance(event, dict) or len(event) != 1:
            raise StaxError("Each Stax archive event must have one variant")
        kind, payload = next(iter(event.items()))
        _object(payload, kind)
        if kind == "RunSummary":
            archive["runs"].append(payload["run"])
        elif kind == "AggregatorClock":
            archive["aggregator"].update(payload)
        elif kind == "ThreadName":
            names[uint(payload["tid"], "thread ID", 32)] = _text(payload["name"], "thread name")
        elif kind == "BinaryLoaded":
            archive["binaries"]["binaries"].append(payload["binary"])
        elif kind in ("PetSample", "Interval", "Wakeup"):
            tid = uint(payload["tid"], "thread ID", 32)
            thread = threads.setdefault(tid, {"tid": tid, "pet_samples": [], "intervals": [], "wakeups": []})
            field, key = {
                "PetSample": ("pet_samples", "sample"),
                "Interval": ("intervals", "interval"),
                "Wakeup": ("wakeups", "wakeup"),
            }[kind]
            thread[field].append(payload[key])
        elif kind == "TargetIngestDiagnostics":
            archive["target_ingest"] = payload["diagnostics"]
        elif kind == "ObservabilityDiagnostics":
            archive["observability"]["diagnostics"] = payload["diagnostics"]
        elif kind == "ArchiveSaved":
            uint(payload["saved_at_unix_ns"], "saved time")
        elif kind in ("TargetEventKind", "TargetEvent", "TargetCounterSet", "TargetCounterSample", "TargetContract"):
            archive["application_records"].append(event)
        else:
            raise StaxError(f"Unsupported Stax archive event: {kind}")
    archive["runs"] = archive["runs"] or fallback_runs
    archive["aggregator"]["thread_names"] = [{"tid": tid, "name": name} for tid, name in names.items()]
    archive["aggregator"]["threads"] = list(threads.values())
    return archive


def _event_stream(path, files):
    digest, size = hashlib.sha256(), 0
    with _open(path) as stream:
        while line := stream.readline(MAX_ARTIFACT_BYTES - size + 1):
            check_cancelled()
            size += len(line)
            if size > MAX_ARTIFACT_BYTES:
                raise StaxError("Stax event stream grew beyond the import limit")
            digest.update(line)
            if line.strip():
                yield _decode(line)
    files.append({"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()})


class _Symbols:
    def __init__(self, binaries):
        self.binaries = []
        for binary in _array(binaries, "binaries"):
            check_cancelled()
            _object(binary, "binary")
            start, end = uint(binary["base_avma"], "binary base"), uint(binary["avma_end"], "binary end")
            if end <= start:
                raise StaxError("Binary mappings must have positive length")
            uint(binary["text_svma"], "binary text address")
            _text(binary["path"], "binary path")
            symbols = []
            for symbol in _array(binary["symbols"], "symbols"):
                check_cancelled()
                lo, hi = uint(symbol["start_svma"], "symbol start"), uint(symbol["end_svma"], "symbol end")
                if hi < lo:
                    raise StaxError("Symbol ends before its start")
                name = bytes(uint(v, "symbol byte", 8) for v in _array(symbol["name"], "symbol name"))
                symbols.append((lo, hi, name.decode("utf-8", errors="replace"), list(name)))
            symbols.sort(key=lambda item: item[0])
            self.binaries.append((start, end, binary, symbols, [s[0] for s in symbols]))
        self.binaries.sort(key=lambda item: item[0])
        self.starts = [b[0] for b in self.binaries]
        self.cache = {}

    def frame(self, address):
        uint(address, "stack address")
        if address in self.cache:
            return self.cache[address]
        frame = {"address": address, "name": None, "name_bytes": None, "binary": None, "symbol_address": None}
        index = bisect_right(self.starts, address) - 1
        if index >= 0:
            start, end, binary, symbols, starts = self.binaries[index]
            if address < end:
                svma = (address - start + binary["text_svma"]) % 2**64
                frame["binary"] = binary["path"]
                symbol_index = bisect_right(starts, svma) - 1
                if symbol_index >= 0 and svma < symbols[symbol_index][1]:
                    lo, _, name, name_bytes = symbols[symbol_index]
                    frame.update(
                        name=name, name_bytes=name_bytes, symbol_address=(lo + start - binary["text_svma"]) % 2**64
                    )
        self.cache[address] = frame
        return frame

    def stack(self, addresses):
        return [self.frame(address) for address in _array(addresses, "stack")]

    def references(self, addresses):
        for address in _array(addresses, "stack"):
            self.frame(address)
        return addresses


class _Stacks:
    def __init__(self, symbols):
        self.symbols, self.rows, self.index = symbols, [], {}

    def intern(self, addresses):
        key = tuple(uint(address, "stack address") for address in _array(addresses, "stack"))
        if key not in self.index:
            self.symbols.references(addresses)
            self.index[key] = len(self.rows)
            self.rows.append(addresses)
        return self.index[key]


def load_archive(path):
    """Import a v3 bundle or directory, replaying its authoritative event stream."""
    path = Path(path).expanduser().resolve()
    if not unicode_scalars(str(path)):
        raise StaxError("Stax artifact paths require valid Unicode scalar values")
    files = []

    def read(member):
        data = _read(member)
        files.append({"path": str(member), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return data

    try:
        directory = path if path.is_dir() else path.parent if path.name == "manifest.json" else None
        source = _object(_decode(read(directory / "manifest.json" if directory else path)), "archive")
        if type(source.get("format_version")) is not int or source["format_version"] not in SUPPORTED_ARCHIVE_VERSIONS:
            raise StaxError(f"Unsupported Stax archive version: {source.get('format_version')!r}; expected v3")
        provenance = _object(source.get("provenance"), "provenance")
        for key in ("producer", "producer_version", "os", "arch"):
            _text(provenance.get(key), f"provenance {key}")
        if "events" in source:
            _array(source["events"], "events")
        if directory:
            event_path = _member(directory, "events.jsonl")
            events = _event_stream(event_path, files) if event_path.exists() else iter(())
            first = next(events, None)
            replayed = first is not None
            if replayed:
                archive = _replay(chain((first,), events), source.get("runs"))
            else:
                members = _object(source["files"], "manifest files")
                archive = {
                    key: _decode(read(_member(directory, members[key])))
                    for key in ("aggregator", "binaries", "target_ingest", "observability")
                }
                archive["runs"] = source["runs"]
        else:
            replayed = bool(source.get("events"))
            archive = _replay(_array(source["events"], "events"), source.get("runs")) if replayed else source
        result = _normalize(archive)
        result["clock"]["domain"] = {"linux": "CLOCK_MONOTONIC", "macos": "mach_absolute_time nanoseconds"}.get(
            provenance["os"], "upstream monotonic clock"
        )
        if provenance["os"] == "macos":
            result["quality"]["pmu_counter_semantics"] = (
                "unverified: XNU source returns cumulative KPC thread snapshots; "
                "Stax v3 does not record whether samples were converted to deltas"
            )
            result["limitations"].append(
                "macOS PMU fields cannot be summed as per-sample deltas without verified conversion provenance."
            )
        return {
            "metadata": {
                "collector": "stax",
                "archive_version": source["format_version"],
                "provenance": source.get("provenance", {}),
                "artifacts": files,
                "ingestion": "event-replay" if replayed else "aggregates",
                "artifact_scope": "ingested structured files; code blobs retained externally",
                "parser_tested_upstream_revision": UPSTREAM_REVISION,
            },
            "result": result,
        }
    except (KeyError, TypeError, OverflowError) as exc:
        raise StaxError(f"Malformed Stax archive: {exc}") from exc


def capture_summary(path, pid):
    """Check the small saved manifest before accepting an owned capture as complete."""
    try:
        manifest = _object(_decode(_read(Path(path) / "manifest.json")), "manifest")
        if (
            manifest.get("format_version") not in SUPPORTED_ARCHIVE_VERSIONS
            or type(manifest.get("format_version")) is not int
        ):
            raise StaxError("Captured an unsupported Stax archive version; expected v3")
        runs = _array(manifest["runs"], "runs")
        if len(runs) != 1 or runs[0].get("target_pid") != pid or type(runs[0].get("target_pid")) is not int:
            raise StaxError("Saved Stax capture does not match the requested PID")
        run = _validate_run(runs[0])
        if run.get("state") != "Stopped" or run.get("stopped_at_unix_ns") is None:
            raise StaxError("Saved Stax capture is not a stopped run")
        if isinstance(run.get("stop_reason"), dict) and "RecorderError" in run["stop_reason"]:
            raise StaxError(f"Stax recorder reported an error: {run['stop_reason']['RecorderError']}")
        return run
    except (KeyError, TypeError, AttributeError) as exc:
        raise StaxError(f"Malformed Stax capture manifest: {exc}") from exc


def companion_metadata(path):
    path = Path(path).expanduser().resolve()
    parent = path.parent if path.is_dir() or path.name != "manifest.json" else path.parent.parent
    metadata_path = parent / "capture.json"
    if not metadata_path.exists():
        return {}, None
    data = _read(metadata_path)
    metadata = _object(_decode(data), "capture metadata")
    if metadata.get("collector") != "stax":
        raise StaxError("Companion capture.json is not Stax metadata; choose an archive without unrelated metadata")
    return metadata, {"path": str(metadata_path), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _validate_run(run):
    run = _object(run, "run")
    identity = _array(run.get("id"), "run ID")
    if len(identity) != 1:
        raise StaxError("Require one wrapped Stax run ID")
    uint(identity[0], "run ID")
    if run.get("state") not in ("Recording", "Stopped"):
        raise StaxError("Require a Stax Recording/Stopped run state")
    for field in ("pet_samples", "off_cpu_intervals"):
        uint(run[field], field)
    uint(run["started_at_unix_ns"], "run start")
    if run.get("stopped_at_unix_ns") is not None:
        if uint(run["stopped_at_unix_ns"], "run stop") < run["started_at_unix_ns"]:
            raise StaxError("Run stops before it starts")
    if run.get("target_pid") is not None:
        uint(run["target_pid"], "target PID", 32)
    _text(run["label"], "run label")
    return run


def _observability(archive):
    source = _object(archive.get("observability", {}), "observability")
    records = list(_array(archive.get("application_records", []), "application records"))
    variants = {
        "event_kinds": ("TargetEventKind", None),
        "events": ("TargetEvent", "event"),
        "counter_sets": ("TargetCounterSet", None),
        "counter_samples": ("TargetCounterSample", "sample"),
        "contracts": ("TargetContract", None),
    }
    if set(source) - {*variants, "diagnostics"}:
        raise StaxError("Unsupported Stax observability field")
    for field, (variant, wrapper) in variants.items():
        for row in _array(source.get(field, []), field):
            records.append({variant: {wrapper: row} if wrapper else row})
    return {"diagnostics": source.get("diagnostics", {})}, records


def _normalize(archive):
    runs = _array(archive["runs"], "runs")
    if len(runs) != 1:
        raise StaxError("Require exactly one run per Stax archive to preserve attribution")
    run = _validate_run(runs[0])
    aggregator = _object(archive["aggregator"], "aggregator")
    clock_end = aggregator.get("last_event_ns")
    for field in ("session_start_ns", "last_event_ns"):
        if aggregator.get(field) is not None:
            uint(aggregator[field], field)
    names = {
        uint(n["tid"], "thread ID", 32): _text(n["name"], "thread name")
        for n in _array(aggregator["thread_names"], "thread names")
    }
    symbols = _Symbols(archive["binaries"]["binaries"])
    stacks = _Stacks(symbols)
    target_ingest = _object(archive.get("target_ingest", {}), "target ingest diagnostics")
    target_lanes = _array(target_ingest.get("lanes", []), "target lanes")
    observability, application_records = _observability(archive)
    target_tids = {
        uint(lane["tid"], "target lane thread ID", 32)
        for lane in _array(target_ingest.get("lanes", []), "target lanes")
    }
    samples, intervals, threads, wakeups = [], [], [], []
    seen = set()
    for thread in _array(aggregator["threads"], "threads"):
        check_cancelled()
        tid = uint(thread["tid"], "thread ID", 32)
        if tid in seen:
            raise StaxError("Duplicate Stax thread ID")
        seen.add(tid)
        target_lane = tid in target_tids or any(
            isinstance(_object(row, "interval").get("kind"), dict) and "SyntheticSpan" in row["kind"]
            for row in _array(thread["intervals"], "intervals")
        )
        counts = Counter(
            {
                "sample_count": 0,
                "target_stack_sample_count": 0,
                "on_cpu_interval_ns": 0,
                "off_cpu_interval_ns": 0,
                "target_span_ns": 0,
            }
        )
        for sample in _array(thread["pet_samples"], "samples"):
            check_cancelled()
            pmc = _object(sample["pmc"], "PMU snapshot")
            for key, value in pmc.items():
                uint(value, f"PMU {key}")
            cpu = sample.get("cpu")
            if cpu is not None:
                cpu = uint(cpu, "sample CPU ID", 32)
            samples.append(
                {
                    "tid": tid,
                    "timestamp_ns": uint(sample["timestamp_ns"], "sample time"),
                    "kind": "target-span-stack" if target_lane else "cpu-stack",
                    "stack_id": stacks.intern(sample["stack"]),
                    "kernel_stack_id": stacks.intern(sample["kernel_stack"]),
                    "pmc": pmc,
                    **({"trace_cpu_id": cpu} if cpu is not None else {}),
                }
            )
            counts["target_stack_sample_count" if target_lane else "sample_count"] += 1
        for interval in _array(thread["intervals"], "intervals"):
            check_cancelled()
            start, end = uint(interval["start_ns"], "interval start"), uint(interval["end_ns"], "interval end")
            open_interval = end == 0
            if open_interval:
                if clock_end is None:
                    raise StaxError("Open intervals require the archive's last event time")
                end = max(start, clock_end)
            if end < start:
                raise StaxError("Interval ends before it starts")
            variant = interval["kind"]
            if not isinstance(variant, str) and (not isinstance(variant, dict) or len(variant) != 1):
                raise StaxError("Interval kind must have one variant")
            kind, payload = (variant, {}) if isinstance(variant, str) else next(iter(variant.items()))
            if kind not in ("OnCpu", "OffCpu", "SyntheticSpan") or not isinstance(payload, dict):
                raise StaxError(f"Unsupported Stax interval kind: {kind}")
            if kind in ("OffCpu", "SyntheticSpan") and "stack" not in payload:
                raise StaxError("Stax wait/target intervals require a stack")
            if kind == "SyntheticSpan" and payload.get("lane_kind") not in ("Generic", "Metal"):
                raise StaxError("Stax target lanes require Generic or Metal lane kind")
            if kind == "OnCpu" and target_lane:
                raise StaxError("Synthetic target lanes cannot contain physical CPU intervals")
            if isinstance(variant, dict) and len(variant) != 1:
                raise StaxError("Interval kind must have one variant")
            row = {
                "tid": tid,
                "start_ns": start,
                "end_ns": end,
                "duration_ns": end - start,
                "kind": kind,
                "open_at_save": open_interval,
            }
            if "stack" in payload:
                row["stack_id"] = stacks.intern(payload["stack"])
            for field in ("waker_tid", "origin_tid"):
                if payload.get(field) is not None:
                    uint(payload[field], field, 32)
                if field in payload:
                    row[field] = payload[field]
            if payload.get("waker_user_stack") is not None:
                row["waker_user_stack_id"] = stacks.intern(payload["waker_user_stack"])
            if "lane_kind" in payload:
                row["lane_kind"] = payload["lane_kind"]
            intervals.append(row)
            counts[
                {"OnCpu": "on_cpu_interval_ns", "OffCpu": "off_cpu_interval_ns", "SyntheticSpan": "target_span_ns"}[
                    kind
                ]
            ] += end - start
            counts[
                {
                    "OnCpu": "on_cpu_interval_count",
                    "OffCpu": "off_cpu_interval_count",
                    "SyntheticSpan": "target_span_count",
                }[kind]
            ] += 1
        for wakeup in _array(thread["wakeups"], "wakeups"):
            check_cancelled()
            uint(wakeup["timestamp_ns"], "wakeup time")
            uint(wakeup["waker_tid"], "waker thread ID", 32)
            wakeups.append(
                {
                    "tid": tid,
                    "timestamp_ns": wakeup["timestamp_ns"],
                    "waker_tid": wakeup["waker_tid"],
                    "waker_user_stack_id": stacks.intern(wakeup["waker_user_stack"]),
                    "waker_kernel_stack_id": stacks.intern(wakeup["waker_kernel_stack"]),
                }
            )
        threads.append(
            {"tid": tid, "name": names.get(tid), "kind": "target-lane" if target_lane else "cpu-thread", **dict(counts)}
        )
    cpu_samples = sum(t["sample_count"] for t in threads)
    cpu_rows = [sample for sample in samples if sample["kind"] == "cpu-stack"]
    missing = sum(not stacks.rows[sample["stack_id"]] for sample in cpu_rows)
    unresolved = sum(
        bool(addresses := stacks.rows[sample["stack_id"]]) and symbols.cache[addresses[0]]["name"] is None
        for sample in cpu_rows
    )
    return {
        "measurement_kind": "stax-sampled-stacks-and-scheduler-intervals",
        "tma_available": False,
        "clock": {
            "session_start_ns": aggregator.get("session_start_ns"),
            "last_event_ns": clock_end,
            "units": "upstream monotonic nanoseconds; target timestamps require producer calibration",
        },
        "quality": {
            "reported_cpu_samples": run["pet_samples"],
            "observed_cpu_samples": cpu_samples,
            "cpu_sample_counts_match": run["pet_samples"] == cpu_samples,
            "cpu_samples_without_user_stack": missing,
            "cpu_samples_with_unresolved_user_leaf": unresolved,
            "cpu_samples_with_trace_cpu_id": sum("trace_cpu_id" in sample for sample in cpu_rows),
            "sampling_observed": cpu_samples > 0,
            "pmu_availability": "unverified",
            "native_loss_accounting": "not represented by the v3 archive",
            "symbol_names": "archived ranges; not demangled",
        },
        "counts": {
            "samples": len(samples),
            "intervals": len(intervals),
            "wakeups": len(wakeups),
            "cpu_samples": cpu_samples,
            "target_stack_samples": sum(t["target_stack_sample_count"] for t in threads),
            "threads": len(threads),
            "frame_table": len(symbols.cache),
            "stack_table": len(stacks.rows),
            "application_records": len(application_records),
            "target_lanes": len(target_lanes),
        },
        "scope": run,
        "threads": threads,
        "samples": samples,
        "intervals": intervals,
        "wakeups": wakeups,
        "frame_table": sorted(symbols.cache.values(), key=lambda frame: frame["address"]),
        "stack_table": stacks.rows,
        "target_ingest": {key: value for key, value in target_ingest.items() if key != "lanes"},
        "target_lanes": target_lanes,
        "observability": observability,
        "application_records": application_records,
        "limitations": [
            "Sample shares describe observed stack hits, not CPU time or TMA pipeline slots.",
            "Scheduler interval totals may overlap; they are not a utilization denominator.",
            "Raw PMU fields are retained without summing; zero does not establish counter availability.",
            "Trace CPU IDs are raw kdebug indices; sampled-thread residency, core type and PMU scope remain unvalidated.",
            "Synthetic target spans are separate from CPU intervals and native GPU hardware counters.",
            "Symbols use archived names without demangling; source artifacts remain required for disassembly.",
        ],
    }
