"""Cluster-scoped firmware observations from native IOReport state channels."""

import ctypes as C
import json
import math
import platform
import plistlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from topdown.collector.apple import AppleProfileError, TMA_ROOTS, _run, _sha256, _write_json
from topdown.collector.apple_environment import capture_environment

STALL_NAMES = {"Core Block Slot", "M$ Stall", "DRAM Stall"}


def _selected(channel):
    group, subgroup = channel.get("IOReportGroupName"), channel.get("IOReportSubGroupName", "")
    legend = channel.get("LegendChannel", [])
    name = legend[2] if len(legend) > 2 else ""
    if group == "CPU Stats" and subgroup == "CPU Core Performance States":
        return True
    if group != "PMP":
        return False
    cluster = subgroup.startswith(("EACC", "PACC"))
    return (cluster and name in STALL_NAMES) or (
        subgroup == "DCS BW" and name.startswith(("AMCC ", "EACC", "PACC")))


class _IOReportReader:
    def __init__(self):
        self.refs = []
        try:
            self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.io = C.CDLL("/usr/lib/libIOReport.dylib")
            self._bind()
            self.key = self._own(self.cf.CFStringCreateWithCString(None, b"IOReportChannels", 0x08000100))
            channels = self.io.IOReportCopyAllChannels(0, 0)
            if not channels:
                raise AppleProfileError("IOReport channel enumeration is unavailable")
            try:
                metadata = self._serialize(channels)
            finally:
                self.cf.CFRelease(channels)
            selected = [channel for channel in metadata.get("IOReportChannels", []) if _selected(channel)]
            if not any(c.get("IOReportGroupName") == "PMP" for c in selected):
                raise AppleProfileError("No supported PMP firmware channels were enumerated")
            self.expected = {(c["IOReportGroupName"], c.get("IOReportSubGroupName", ""), c["LegendChannel"][0] & (2**64 - 1))
                             for c in selected}
            metadata["IOReportChannels"] = selected
            desired = self._own(self._deserialize(metadata))
            subscribed = C.c_void_p()
            subscription = self.io.IOReportCreateSubscription(None, desired, C.byref(subscribed), 0, None)
            if subscribed.value:
                self._own(subscribed.value)
            if not subscription:
                raise AppleProfileError("The enumerated IOReport channels cannot be subscribed to")
            self.subscription = self._own(subscription)
            self.subscribed = subscribed.value
            if not self.subscribed:
                raise AppleProfileError("IOReport subscription did not supply a channel set")
        except BaseException:
            self.close()
            raise

    def _bind(self):
        pointer, integer = C.c_void_p, C.c_long
        cf_functions = {
            "CFRelease": ([pointer], None), "CFGetTypeID": ([pointer], C.c_ulong),
            "CFArrayGetTypeID": ([], C.c_ulong), "CFDictionaryGetTypeID": ([], C.c_ulong),
            "CFStringCreateWithCString": ([pointer, C.c_char_p, C.c_uint32], pointer),
            "CFStringGetLength": ([pointer], integer),
            "CFStringGetMaximumSizeForEncoding": ([integer, C.c_uint32], integer),
            "CFStringGetCString": ([pointer, pointer, integer, C.c_uint32], C.c_bool),
            "CFPropertyListCreateData": ([pointer, pointer, integer, C.c_ulong, pointer], pointer),
            "CFDataGetLength": ([pointer], integer), "CFDataGetBytePtr": ([pointer], pointer),
            "CFDataCreate": ([pointer, pointer, integer], pointer),
            "CFPropertyListCreateWithData": ([pointer, pointer, C.c_ulong, pointer, pointer], pointer),
            "CFDictionaryGetValue": ([pointer, pointer], pointer),
            "CFArrayGetCount": ([pointer], integer), "CFArrayGetValueAtIndex": ([pointer, integer], pointer),
        }
        io_functions = {
            "IOReportCopyAllChannels": ([C.c_uint64, C.c_uint64], pointer),
            "IOReportCreateSubscription": ([pointer, pointer, C.POINTER(pointer), C.c_uint64, pointer], pointer),
            "IOReportCreateSamples": ([pointer, pointer, pointer], pointer),
            "IOReportChannelGetFormat": ([pointer], C.c_uint8),
            "IOReportChannelGetUnit": ([pointer], C.c_uint64),
            "IOReportChannelGetChannelID": ([pointer], C.c_uint64),
            "IOReportStateGetCount": ([pointer], C.c_int),
        }
        for name in ["Group", "SubGroup", "ChannelName", "UnitLabel"]:
            io_functions["IOReportChannelGet" + name] = ([pointer], pointer)
        io_functions["IOReportStateGetNameForIndex"] = ([pointer, C.c_int], pointer)
        for name in ["IDForIndex", "Residency", "InTransitions"]:
            io_functions["IOReportStateGet" + name] = ([pointer, C.c_int], C.c_uint64)
        for library, functions in [(self.cf, cf_functions), (self.io, io_functions)]:
            for name, (args, result) in functions.items():
                function = getattr(library, name)
                function.argtypes, function.restype = args, result

    def _own(self, ref):
        if not ref:
            raise AppleProfileError("CoreFoundation allocation failed")
        self.refs.append(ref)
        return ref

    def close(self):
        for ref in reversed(self.refs):
            self.cf.CFRelease(ref)
        self.refs.clear()

    def _serialize(self, ref):
        data = self.cf.CFPropertyListCreateData(None, ref, 200, 0, None)
        if not data:
            raise AppleProfileError("Cannot serialize native IOReport channel metadata")
        try:
            size = self.cf.CFDataGetLength(data)
            if not 0 < size <= 32 * 1024 * 1024:
                raise AppleProfileError("Invalid or oversized IOReport channel metadata")
            pointer = self.cf.CFDataGetBytePtr(data)
            if not pointer:
                raise AppleProfileError("IOReport channel metadata has no native bytes")
            return plistlib.loads(C.string_at(pointer, size))
        finally:
            self.cf.CFRelease(data)

    def _deserialize(self, value):
        payload = plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
        data = self.cf.CFDataCreate(None, C.c_char_p(payload), len(payload))
        if not data:
            raise AppleProfileError("Cannot allocate native IOReport channel metadata")
        try:
            return self.cf.CFPropertyListCreateWithData(None, data, 1, None, None)
        finally:
            self.cf.CFRelease(data)

    def _string(self, ref):
        if not ref:
            return ""
        size = self.cf.CFStringGetMaximumSizeForEncoding(self.cf.CFStringGetLength(ref), 0x08000100) + 1
        if not 0 < size <= 4096:
            raise AppleProfileError("Invalid IOReport channel string size")
        buffer = C.create_string_buffer(size)
        if not self.cf.CFStringGetCString(ref, buffer, size, 0x08000100):
            raise AppleProfileError("Cannot decode an IOReport channel string")
        return buffer.value.decode("utf-8")

    def snapshot(self):
        sample = self.io.IOReportCreateSamples(self.subscription, self.subscribed, None)
        if not sample:
            raise AppleProfileError("IOReport did not return a sample")
        try:
            if self.cf.CFGetTypeID(sample) != self.cf.CFDictionaryGetTypeID():
                raise AppleProfileError("Unknown IOReport sample representation")
            array = self.cf.CFDictionaryGetValue(sample, self.key)
            if not array or self.cf.CFGetTypeID(array) != self.cf.CFArrayGetTypeID():
                raise AppleProfileError("IOReport sample has no native channel array")
            count = self.cf.CFArrayGetCount(array)
            if not 0 < count <= 512:
                raise AppleProfileError("Invalid IOReport sample channel count")
            channels = []
            for index in range(count):
                channel = self.cf.CFArrayGetValueAtIndex(array, index)
                if not channel or self.io.IOReportChannelGetFormat(channel) != 2:
                    raise AppleProfileError("Unknown IOReport state-channel format")
                states = self.io.IOReportStateGetCount(channel)
                if not 0 < states <= 4096:
                    raise AppleProfileError("Invalid IOReport state count")
                row = {key: self._string(getattr(self.io, "IOReportChannelGet" + name)(channel))
                       for key, name in [("group", "Group"), ("subgroup", "SubGroup"), ("name", "ChannelName"),
                                         ("unit_label", "UnitLabel")]}
                row.update(id=self.io.IOReportChannelGetChannelID(channel), unit=self.io.IOReportChannelGetUnit(channel),
                           states=[{"id": self.io.IOReportStateGetIDForIndex(channel, i),
                                    "label": self._string(self.io.IOReportStateGetNameForIndex(channel, i)),
                                    "value": self.io.IOReportStateGetResidency(channel, i),
                                    "intransitions": self.io.IOReportStateGetInTransitions(channel, i)}
                                   for i in range(states)])
                if any(s["value"] == 2**63 or s["intransitions"] == 2**63 for s in row["states"]):
                    raise AppleProfileError("IOReport returned its invalid-value sentinel")
                channels.append(row)
            if {_channel_key(c) for c in channels} != self.expected:
                raise AppleProfileError("IOReport supplied a partial or changed channel set")
            return channels
        finally:
            self.cf.CFRelease(sample)


def _channel_key(channel):
    return channel["group"], channel["subgroup"], channel["id"]


def _deltas(before, after):
    first, last = {_channel_key(c): c for c in before}, {_channel_key(c): c for c in after}
    if not first or len(first) != len(before) or len(last) != len(after) or first.keys() != last.keys():
        raise AppleProfileError("IOReport channel identities changed or contain duplicates")
    result = []
    for key, old in first.items():
        new = last[key]
        if any(old[field] != new[field] for field in ["name", "unit", "unit_label"]):
            raise AppleProfileError("IOReport channel units or labels changed")
        if len(old["states"]) != len(new["states"]) or not old["states"]:
            raise AppleProfileError("IOReport state layout changed")
        states = []
        ids = set()
        for left, right in zip(old["states"], new["states"]):
            if left["id"] != right["id"] or left["label"] != right["label"] or left["id"] in ids:
                raise AppleProfileError("IOReport state identities changed or contain duplicates")
            ids.add(left["id"])
            row = {"id": left["id"], "label": left["label"]}
            for field in ["value", "intransitions"]:
                a, b = left[field], right[field]
                if (type(a) is not int or type(b) is not int or not 0 <= a <= b < 2**64
                        or a == 2**63 or b == 2**63):
                    raise AppleProfileError("IOReport counters reset, decreased or are not uint64 values")
                row[field] = b - a
            states.append(row)
        total = sum(s["value"] for s in states)
        for state in states:
            state["share_of_reported_values"] = state["value"] / total if total else None
        result.append({**{k: v for k, v in old.items() if k != "states"}, "states": states,
                       "native_value_total": total, "observations_available": total > 0})
    return result


def _native_measure(duration):
    reader = _IOReportReader()
    try:
        first_begin = time.monotonic_ns()
        before = reader.snapshot()
        first_end = time.monotonic_ns()
        time.sleep(duration)
        last_begin = time.monotonic_ns()
        after = reader.snapshot()
        last_end = time.monotonic_ns()
        return {"snapshot_before": before, "snapshot_after": after,
                "read_times": {"first_begin_ns": first_begin, "first_end_ns": first_end,
                               "last_begin_ns": last_begin, "last_end_ns": last_end}}
    finally:
        reader.close()


def sample_soc(output: Path, duration: float = 1) -> dict:
    """Read cluster/system firmware context in a bounded native worker."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise AppleProfileError("Native SoC firmware observations require macOS on Apple silicon")
    if type(duration) not in (int, float) or not 0 < duration <= 300:
        raise AppleProfileError("Require a finite duration greater than zero and at most 300 seconds")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = {
        "schema_version": 1, "measurement_kind": "native-soc-firmware-state-observations",
        "started_at": datetime.now(timezone.utc).isoformat(), "collector_sha256": _sha256(Path(__file__)),
        "environment": capture_environment(),
        "scope": "system-and-cluster; all processes, no PID attribution", "duration_seconds": duration,
        "source": "IOReport native state channels; value=IOReportStateGetResidency; units retained per channel",
        "clock": time.get_clock_info("monotonic").implementation,
        "status": "failed", "exit_code": 1, "channels": None,
        "tma_available": False, "tma": dict.fromkeys(TMA_ROOTS),
        "limitations": [
            "System/cluster firmware observations include unrelated processes and devices.",
            "State labels and units are native. Events are not nanoseconds; bandwidth labels are not byte deltas.",
            "Shares describe reported values only; they do not establish wall-time or pipeline-slot percentages.",
            "Bucket boundary conventions and the firmware's sampling/active-time denominator remain unvalidated.",
            "No observations yields null shares, not zero stall or bandwidth.",
            "No per-process bottleneck classification, IPC, exact bandwidth or four-category TMA is derived.",
        ],
    }
    try:
        # A worker bounds private-framework stalls and releases its subscription on timeout/interruption.
        _run([sys.executable, "-m", "topdown.collector.apple_soc", "--native", str(duration)],
             output, "ioreport", duration + 30)
        payload = json.loads((output / "ioreport.stdout").read_text())
        before, after = payload["snapshot_before"], payload["snapshot_after"]
        channels = _deltas(before, after)
        _write_json(output / "snapshot-before.json", before)
        _write_json(output / "snapshot-after.json", after)
        times = payload["read_times"]
        values = [times[key] for key in ["first_begin_ns", "first_end_ns", "last_begin_ns", "last_end_ns"]]
        if any(type(v) is not int or v < 0 for v in values) or values != sorted(values) or values[-1] == values[0]:
            raise AppleProfileError("Invalid native IOReport read timestamps")
        result.update(channels=channels, channel_count=len(channels), read_times=times,
                      stall_channels_available=any(c["group"] == "PMP" and c["name"] in STALL_NAMES for c in channels),
                      bandwidth_channels_available=any(c["group"] == "PMP" and c["subgroup"] == "DCS BW" for c in channels),
                      observations_available=any(c["observations_available"] for c in channels),
                      status="completed", exit_code=0)
    except (AppleProfileError, OSError, ValueError, KeyError, TypeError) as exc:
        result["error"] = str(exc)
        if isinstance(exc, AppleProfileError):
            if exc.code == "timeout":
                result.update(status="timeout", exit_code=5)
            elif exc.code == "interrupted":
                result.update(status="interrupted", exit_code=130)
    except KeyboardInterrupt:
        result.update(status="interrupted", exit_code=130)
    finally:
        result["ended_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(output / "soc.json", result)
    return result


def _main(args):
    try:
        if len(args) != 2 or args[0] != "--native":
            raise ValueError("Internal IOReport worker requires --native DURATION")
        seconds = float(args[1])
        if not math.isfinite(seconds) or not 0 < seconds <= 300:
            raise ValueError("Invalid native worker duration")
        print(json.dumps(_native_measure(seconds)))
        return 0
    except (AppleProfileError, OSError, AttributeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
