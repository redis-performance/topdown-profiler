"""Measured firmware state observations, native ownership and bounded CLI capture."""

import ctypes as C
import json
import platform
import plistlib
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from topdown.cli import app
from topdown.collector.apple import AppleProfileError
from topdown.collector.apple_soc import _IOReportReader, _deltas, _main, _native_measure, _selected, sample_soc

FIXTURE = Path(__file__).parent / "test_data/apple_soc_dram_stall_m6.json"


def snapshots():
    after = json.loads(FIXTURE.read_text())
    before = deepcopy(after)
    for state in before["states"]:
        state.update(value=0, intransitions=0)
    return [before], [after]


def payload():
    before, after = snapshots()
    return {"snapshot_before": before, "snapshot_after": after,
            "read_times": {"first_begin_ns": 100, "first_end_ns": 110, "last_begin_ns": 200, "last_end_ns": 210}}


@pytest.fixture
def native_host():
    with patch("topdown.collector.apple_soc.platform.system", return_value="Darwin"), \
         patch("topdown.collector.apple_soc.platform.machine", return_value="arm64"):
        yield


def test_measured_dram_observations_retain_native_units_and_labels():
    before, after = snapshots()
    result = _deltas(before, after)[0]
    assert result["name"] == "DRAM Stall" and result["unit_label"] == "events"
    assert result["native_value_total"] == sum(s["value"] for s in after[0]["states"])
    assert result["native_value_total"] > 1000
    assert sum(s["share_of_reported_values"] for s in result["states"]) == pytest.approx(1)
    assert [s["label"] for s in result["states"]] == [s["label"] for s in after[0]["states"]]
    assert [s["value"] for s in result["states"]] == [s["value"] for s in after[0]["states"]]
    assert "stall_percent" not in result and "bandwidth_gbps" not in result


def test_zero_observations_have_null_shares():
    before, _ = snapshots()
    result = _deltas(before, deepcopy(before))[0]
    assert result["native_value_total"] == 0
    assert result["observations_available"] is False
    assert all(s["share_of_reported_values"] is None for s in result["states"])


def test_uint64_deltas_preserve_precision():
    before, after = snapshots()
    before[0]["states"][0]["value"] = 2**63 + 3
    after[0]["states"][0]["value"] = 2**64 - 1
    assert _deltas(before, after)[0]["states"][0]["value"] == 2**63 - 4


@pytest.mark.parametrize("value", [-1, 2**64, 2**63, True, 1.5])
def test_counter_reset_invalid_sentinel_or_wrong_type_never_becomes_zero(value):
    before, after = snapshots()
    after[0]["states"][0]["value"] = value
    with pytest.raises(AppleProfileError, match="reset, decreased|uint64"):
        _deltas(before, after)


@pytest.mark.parametrize("change", ["decrease", "missing-channel", "duplicate-channel", "name", "unit", "unit_label",
                                   "state-count", "empty-states", "state-id", "state-label", "duplicate-state"])
def test_changed_source_or_state_layout_fails_closed(change):
    before, after = snapshots()
    if change == "decrease":
        before[0]["states"][0]["value"] = after[0]["states"][0]["value"] + 1
    elif change == "missing-channel":
        after.clear()
    elif change == "duplicate-channel":
        after.append(deepcopy(after[0]))
    elif change in {"name", "unit", "unit_label"}:
        after[0][change] = "changed"
    elif change == "state-count":
        after[0]["states"].pop()
    elif change == "empty-states":
        before[0]["states"].clear()
        after[0]["states"].clear()
    elif change == "state-id":
        after[0]["states"][0]["id"] += 1
    elif change == "state-label":
        after[0]["states"][0]["label"] = "different"
    else:
        before[0]["states"][1]["id"] = before[0]["states"][0]["id"]
        after[0]["states"][1]["id"] = after[0]["states"][0]["id"]
    with pytest.raises(AppleProfileError):
        _deltas(before, after)


@pytest.mark.parametrize("group,subgroup,name,expected", [
    ("PMP", "PACC0", "DRAM Stall", True), ("PMP", "EACC0", "M$ Stall", True),
    ("PMP", "PACC1", "Core Block Slot", True), ("PMP", "DCS BW", "AMCC RD+WR", True),
    ("PMP", "DCS BW", "EACC0 RD", True), ("CPU Stats", "CPU Core Performance States", "EACC_ECPU0", True),
    ("PMP", "DCS BW", "AGX RD+WR", False), ("PMP", "BWR", "AMCC RD+WR", False),
    ("AMC Stats", "Perf Counters", "DRAM Stall", False), ("Energy Model", "", "CPU Energy", False),
    ("PMP", "PACC0", "unknown", False),
])
def test_only_the_intended_native_context_channels_are_requested(group, subgroup, name, expected):
    channel = {"IOReportGroupName": group, "IOReportSubGroupName": subgroup, "LegendChannel": [123, 2, name]}
    assert _selected(channel) is expected


def write_worker_result(cmd, output, name, timeout):
    (output / "ioreport.stdout").write_text(json.dumps(payload()))


def test_native_worker_is_bounded_and_scope_tma_are_explicit(tmp_path, native_host):
    with patch("topdown.collector.apple_soc._run", side_effect=write_worker_result) as run:
        result = sample_soc(tmp_path / "out", .25)
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert result["stall_channels_available"] is True and result["bandwidth_channels_available"] is False
    assert result["tma_available"] is False and set(result["tma"].values()) == {None}
    assert "no PID attribution" in result["scope"]
    assert run.call_args.args[0][1:] == ["-m", "topdown.collector.apple_soc", "--native", "0.25"]
    assert run.call_args.args[-1] == 30.25
    assert json.loads((tmp_path / "out/soc.json").read_text()) == result
    assert (tmp_path / "out/snapshot-before.json").exists()


@pytest.mark.parametrize("error,status,code", [(AppleProfileError("stalled", code="timeout"), "timeout", 5),
                                             (AppleProfileError("no subscription"), "failed", 1),
                                             (OSError("cannot load"), "failed", 1),
                                             (KeyboardInterrupt(), "interrupted", 130)])
def test_worker_failures_are_saved_without_observations(tmp_path, native_host, error, status, code):
    with patch("topdown.collector.apple_soc._run", side_effect=error):
        result = sample_soc(tmp_path / "out")
    assert result["status"] == status and result["exit_code"] == code
    assert result["channels"] is None
    assert json.loads((tmp_path / "out/soc.json").read_text()) == result


@pytest.mark.parametrize("change", ["json", "missing", "timestamp-type", "timestamp-order", "timestamp-zero", "reset"])
def test_invalid_worker_payload_is_never_a_success(tmp_path, native_host, change):
    def malformed(cmd, output, name, timeout):
        data = payload()
        if change == "missing":
            data.pop("snapshot_after")
        elif change == "timestamp-type":
            data["read_times"]["first_begin_ns"] = 1.5
        elif change == "timestamp-order":
            data["read_times"]["first_begin_ns"] = 300
        elif change == "timestamp-zero":
            data["read_times"] = dict.fromkeys(data["read_times"], 0)
        elif change == "reset":
            data["snapshot_after"][0]["states"][0]["value"] = -1
        (output / "ioreport.stdout").write_text("invalid json" if change == "json" else json.dumps(data))
    with patch("topdown.collector.apple_soc._run", side_effect=malformed):
        result = sample_soc(tmp_path / "out")
    assert result["exit_code"] == 1 and result["channels"] is None
    assert (tmp_path / "out/ioreport.stdout").exists()


@pytest.mark.parametrize("duration", [0, -1, 301, True, False, "1", 10**1000, 1e100, float("inf"), float("nan")])
def test_invalid_interval_never_creates_output(tmp_path, native_host, duration):
    with pytest.raises(AppleProfileError, match="duration"):
        sample_soc(tmp_path / "out", duration)
    assert not (tmp_path / "out").exists()


def test_output_is_not_overwritten(tmp_path, native_host):
    with patch("topdown.collector.apple_soc._run") as run:
        with pytest.raises(FileExistsError):
            sample_soc(tmp_path)
    run.assert_not_called()


def test_unsupported_host_never_loads_frameworks(tmp_path):
    with patch("topdown.collector.apple_soc.platform.system", return_value="Linux"), \
         patch("topdown.collector.apple_soc.C.CDLL") as load:
        with pytest.raises(AppleProfileError, match="Apple silicon"):
            sample_soc(tmp_path / "out")
    load.assert_not_called()


def reader_stub():
    reader = _IOReportReader.__new__(_IOReportReader)
    reader.cf, reader.io, reader.refs = MagicMock(), MagicMock(), []
    reader.key, reader.subscription, reader.subscribed = 99, 50, 60
    reader.expected = {("PMP", "PACC0", 56)}
    reader.io.IOReportCreateSamples.return_value = 10
    reader.cf.CFGetTypeID.side_effect = lambda ref: 2 if ref == 10 else 3
    reader.cf.CFDictionaryGetTypeID.return_value = 2
    reader.cf.CFArrayGetTypeID.return_value = 3
    reader.cf.CFDictionaryGetValue.return_value = 20
    reader.cf.CFArrayGetCount.return_value = 1
    reader.cf.CFArrayGetValueAtIndex.return_value = 30
    reader.io.IOReportChannelGetFormat.return_value = 2
    reader.io.IOReportStateGetCount.return_value = 1
    labels = {401: "PMP", 402: "PACC0", 403: "DRAM Stall", 404: "events", 405: "   4%"}
    reader._string = lambda ref: labels[ref]
    for suffix, ref in [("Group", 401), ("SubGroup", 402), ("ChannelName", 403), ("UnitLabel", 404)]:
        getattr(reader.io, "IOReportChannelGet" + suffix).return_value = ref
    reader.io.IOReportStateGetNameForIndex.return_value = 405
    reader.io.IOReportChannelGetChannelID.return_value = 56
    reader.io.IOReportChannelGetUnit.return_value = 7205759403792793600
    reader.io.IOReportStateGetIDForIndex.return_value = 64
    reader.io.IOReportStateGetResidency.return_value = 12345678901234567
    reader.io.IOReportStateGetInTransitions.return_value = 12
    return reader


def test_ffi_state_values_remain_uint64_and_sample_is_released():
    reader = reader_stub()
    data = reader.snapshot()
    assert data[0]["states"][0]["value"] == 12345678901234567
    assert data[0]["unit_label"] == "events"
    reader.cf.CFRelease.assert_called_once_with(10)


@pytest.mark.parametrize("change", ["no-sample", "sample-type", "no-array", "array-type", "channel-count",
                                   "state-count", "format", "sentinel", "partial"])
def test_native_missing_unknown_and_partial_data_fail_without_leaking_sample(change):
    reader = reader_stub()
    if change == "no-sample":
        reader.io.IOReportCreateSamples.return_value = None
    elif change == "sample-type":
        reader.cf.CFGetTypeID.side_effect = lambda ref: 0
    elif change == "no-array":
        reader.cf.CFDictionaryGetValue.return_value = None
    elif change == "array-type":
        reader.cf.CFGetTypeID.side_effect = lambda ref: 2
    elif change == "channel-count":
        reader.cf.CFArrayGetCount.return_value = 513
    elif change == "state-count":
        reader.io.IOReportStateGetCount.return_value = 0
    elif change == "format":
        reader.io.IOReportChannelGetFormat.return_value = 1
    elif change == "sentinel":
        reader.io.IOReportStateGetResidency.return_value = 2**63
    else:
        reader.expected = {("PMP", "PACC0", 99)}
    with pytest.raises(AppleProfileError):
        reader.snapshot()
    if change == "no-sample":
        reader.cf.CFRelease.assert_not_called()
    else:
        reader.cf.CFRelease.assert_called_once_with(10)


@pytest.mark.parametrize("failure", [None, "no-enumeration", "no-pmp", "allocation", "subscription", "no-channel-set"])
def test_subscription_ownership_and_signed_metadata_ids(failure):
    cf, io = MagicMock(), MagicMock()
    cf.CFStringCreateWithCString.return_value = 100
    io.IOReportCopyAllChannels.return_value = None if failure == "no-enumeration" else 101
    channel = {"IOReportGroupName": "PMP", "IOReportSubGroupName": "PACC0", "LegendChannel": [-1, 2, "DRAM Stall"]}
    metadata = {"IOReportChannels": [] if failure == "no-pmp" else [channel]}
    def subscribe(_, desired, target, flags, context):
        target._obj.value = None if failure == "no-channel-set" else 103
        return None if failure == "subscription" else 104
    io.IOReportCreateSubscription.side_effect = subscribe
    with patch("topdown.collector.apple_soc.C.CDLL", side_effect=[cf, io]), \
         patch.object(_IOReportReader, "_serialize", return_value=metadata), \
         patch.object(_IOReportReader, "_deserialize", return_value=None if failure == "allocation" else 102):
        if failure:
            with pytest.raises(AppleProfileError):
                _IOReportReader()
        else:
            reader = _IOReportReader()
            assert reader.expected == {("PMP", "PACC0", 2**64 - 1)}
            reader.close()
            reader.close()
    released = [call.args[0] for call in cf.CFRelease.call_args_list]
    assert len(released) == len(set(released))
    assert 100 in released
    if failure != "no-enumeration":
        assert 101 in released
    if failure in {None, "subscription"}:
        assert 103 in released


def test_native_worker_closes_subscription_even_if_read_fails():
    reader = MagicMock()
    reader.snapshot.side_effect = AppleProfileError("read failed")
    with patch("topdown.collector.apple_soc._IOReportReader", return_value=reader):
        with pytest.raises(AppleProfileError):
            _native_measure(.1)
    reader.close.assert_called_once()


def test_worker_read_timing_and_snapshot_order():
    reader = MagicMock()
    reader.snapshot.side_effect = [[1], [2]]
    with patch("topdown.collector.apple_soc._IOReportReader", return_value=reader), \
         patch("time.monotonic_ns", side_effect=[10, 20, 100, 120]), patch("time.sleep") as sleep:
        data = _native_measure(.5)
    assert data["snapshot_before"] == [1] and data["snapshot_after"] == [2]
    assert list(data["read_times"].values()) == [10, 20, 100, 120]
    sleep.assert_called_once_with(.5)
    reader.close.assert_called_once()


def test_native_metadata_serialization_preserves_uint64_and_releases_temporary_data():
    reader = reader_stub()
    data = plistlib.dumps({"value": 2**64 - 1}, fmt=plistlib.FMT_BINARY)
    buffer = C.create_string_buffer(data)
    reader.cf.CFPropertyListCreateData.return_value = 11
    reader.cf.CFDataGetLength.return_value = len(data)
    reader.cf.CFDataGetBytePtr.return_value = C.addressof(buffer)
    assert reader._serialize(40) == {"value": 2**64 - 1}
    reader.cf.CFRelease.assert_called_once_with(11)


@pytest.mark.parametrize("change", ["no-data", "size", "no-bytes"])
def test_invalid_native_metadata_is_not_dereferenced(change):
    reader = reader_stub()
    reader.cf.CFPropertyListCreateData.return_value = None if change == "no-data" else 11
    reader.cf.CFDataGetLength.return_value = 2**30 if change == "size" else 10
    reader.cf.CFDataGetBytePtr.return_value = None
    with pytest.raises(AppleProfileError):
        reader._serialize(40)
    if change == "no-data":
        reader.cf.CFRelease.assert_not_called()
    else:
        reader.cf.CFRelease.assert_called_once_with(11)


@pytest.mark.parametrize("allocated", [True, False])
def test_native_metadata_deserialization_owns_only_the_returned_dictionary(allocated):
    reader = reader_stub()
    reader.cf.CFDataCreate.return_value = 11 if allocated else None
    reader.cf.CFPropertyListCreateWithData.return_value = 12
    if allocated:
        assert reader._deserialize({"value": 1}) == 12
        reader.cf.CFRelease.assert_called_once_with(11)
    else:
        with pytest.raises(AppleProfileError, match="allocate"):
            reader._deserialize({"value": 1})
        reader.cf.CFRelease.assert_not_called()


def test_native_strings_decode_without_changing_labels():
    reader = reader_stub()
    reader.cf.CFStringGetLength.return_value = 3
    reader.cf.CFStringGetMaximumSizeForEncoding.return_value = 5
    def copy(ref, buffer, size, encoding):
        C.memmove(buffer, b" 4%\0", 4)
        return True
    reader.cf.CFStringGetCString.side_effect = copy
    assert _IOReportReader._string(reader, 1) == " 4%"
    assert _IOReportReader._string(reader, None) == ""


@pytest.mark.parametrize("change", ["size", "decode"])
def test_invalid_native_strings_fail(change):
    reader = reader_stub()
    reader.cf.CFStringGetMaximumSizeForEncoding.return_value = 5000 if change == "size" else 5
    reader.cf.CFStringGetCString.return_value = False
    with pytest.raises(AppleProfileError):
        _IOReportReader._string(reader, 1)


@pytest.mark.parametrize("args", [[], ["invalid", "1"], ["--native", "0"], ["--native", "nan"],
                                  ["--native", "inf"], ["--native", "301"]])
def test_internal_worker_rejects_unbounded_intervals(args, capsys):
    with patch("topdown.collector.apple_soc._native_measure") as measure:
        assert _main(args) == 1
    measure.assert_not_called()
    assert capsys.readouterr().err


def test_internal_worker_returns_only_snapshot_json(capsys):
    with patch("topdown.collector.apple_soc._native_measure", return_value=payload()):
        assert _main(["--native", ".01"]) == 0
    assert json.loads(capsys.readouterr().out) == payload()


def test_internal_framework_failure_is_not_a_measurement(capsys):
    with patch("topdown.collector.apple_soc._native_measure", side_effect=OSError("missing framework")):
        assert _main(["--native", "1"]) == 1
    assert "missing framework" in capsys.readouterr().err


def test_cli_only_json_and_exit_status(tmp_path):
    result = {"status": "completed", "exit_code": 0, "channels": []}
    with patch("topdown.collector.apple_soc.sample_soc", return_value=result) as sample:
        output = CliRunner().invoke(app, ["apple", "soc", "--output", str(tmp_path / "out"), "--duration", "2"])
    assert output.exit_code == 0 and json.loads(output.stdout) == result
    assert sample.call_args.args[1] == 2


def test_cli_preflight_error_is_visible(tmp_path):
    with patch("topdown.collector.apple_soc.sample_soc", side_effect=AppleProfileError("unsupported")):
        output = CliRunner().invoke(app, ["apple", "soc", "--output", str(tmp_path / "out")])
    assert output.exit_code == 1 and "unsupported" in output.output


@pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64", reason="Native macOS arm64 IOReport")
def test_native_firmware_read_and_manual_deltas():
    try:
        payload = _native_measure(.01)
    except AppleProfileError as exc:
        if "No supported PMP" in str(exc) or "cannot be subscribed" in str(exc):
            pytest.skip(str(exc))
        raise
    result = _deltas(payload["snapshot_before"], payload["snapshot_after"])
    assert result
    assert all(c["unit_label"] in {"events", "24Mticks"} for c in result)
    assert all(0 <= s["value"] < 2**64 for c in result for s in c["states"])
