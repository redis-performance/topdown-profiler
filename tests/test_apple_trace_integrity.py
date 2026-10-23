"""Stored trace context must agree with decoded intervals and observed scheduling."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from topdown.apple_storage import store_apple_result, validate_result
from topdown.collector.apple import AppleProfileError
from topdown.collector.apple_schedule import annotate_scheduling
from topdown.collector.apple_trace import parse_trace_profile

DATA = Path(__file__).parent / "test_data"


@pytest.fixture
def native_trace():
    result = parse_trace_profile(DATA / "apple_processor_intervals_m6.xml", DATA / "apple_processor_gaps_m6.xml", 42)
    result["measurement_kind"] = "hardware-traced-execution-intervals"
    annotate_scheduling(result, DATA / "apple_thread_states_m6.xml")
    return result


@pytest.mark.parametrize("change", ["interval-gap-count", "total-gap-count", "negative-total-gaps", "gap-time",
                                    "covered", "uncovered", "states", "state-value", "core-duration", "core-id",
                                    "duplicate-core", "single-core", "complete", "summary", "missing-annotation",
                                    "unavailable", "metadata-count"])
def test_contradictory_trace_context_is_rejected_before_insert(sqlite_backend, tmp_path, native_trace, change):
    row = native_trace["intervals"][2]
    scheduling = row["scheduling"]
    if change == "interval-gap-count":
        row["gap_markers_inside"] += 1
    elif change == "total-gap-count":
        native_trace["target_gap_count_in_run"] = 0
    elif change == "negative-total-gaps":
        native_trace["target_gap_count_in_run"] = -1
    elif change == "gap-time":
        native_trace["gaps"][0]["time_ns"] = row["start_ns"]
    elif change == "covered":
        scheduling["covered_ns"] += 1
    elif change == "uncovered":
        scheduling["uncovered_ns"] += 1
    elif change == "states":
        scheduling["states_ns"]["Blocked"] = 1
    elif change == "state-value":
        scheduling["states_ns"]["Running"] = True
    elif change == "core-duration":
        scheduling["running_cores"][0]["duration_ns"] += 1
    elif change == "core-id":
        scheduling["running_cores"][0]["core"] = True
    elif change == "duplicate-core":
        scheduling["running_cores"].append(deepcopy(scheduling["running_cores"][0]))
    elif change == "single-core":
        scheduling["single_running_core"]["core"] += 1
    elif change == "complete":
        scheduling["coverage_complete"] = False
    elif change == "summary":
        native_trace["single_running_core_intervals"] += 1
    elif change == "missing-annotation":
        del row["scheduling"]
    elif change == "unavailable":
        native_trace["scheduling_available"] = False
    else:
        native_trace["thread_state_rows"] = -1
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(native_trace))
    with pytest.raises(AppleProfileError):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []


def test_native_trace_and_scheduling_round_trip_without_counter_attribution(sqlite_backend, tmp_path, native_trace):
    original = deepcopy(native_trace)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(original))
    run = store_apple_result(sqlite_backend, path)
    stored = sqlite_backend.get_apple_result(run.run_id)["result"]
    assert stored == original
    assert stored["coverage_complete"] is False
    assert stored["intervals"][2]["scheduling"]["single_running_core"] == {"core": 6, "core_label": "CPU 6 (S Core)"}


def test_native_running_time_without_core_attribution_is_valid(native_trace):
    row = native_trace["intervals"][2]
    row["scheduling"]["running_cores"] = []
    row["scheduling"]["single_running_core"] = None
    native_trace["single_running_core_intervals"] -= 1
    validate_result(native_trace)


def test_unscheduled_legacy_trace_keeps_its_original_gap_metadata(native_trace):
    for field in ["scheduling_available", "single_running_core_intervals", "thread_state_rows", "thread_state_other_pid_rows",
                  "thread_state_xml_sha256"]:
        native_trace.pop(field, None)
    for row in native_trace["intervals"]:
        row.pop("scheduling")
    validate_result(native_trace)


def test_gap_markers_follow_half_open_interval_boundaries(native_trace):
    row = native_trace["intervals"][2]
    native_trace["intervals"] = [row]
    native_trace["interval_count"] = 1
    native_trace["single_running_core_intervals"] = 1
    gap = native_trace["gaps"][0]
    gap["time_ns"] = row["start_ns"]
    row["gap_markers_inside"] = 1
    validate_result(native_trace)
    gap["time_ns"] = row["start_ns"] + row["duration_ns"]
    row["gap_markers_inside"] = 0
    validate_result(native_trace)
    gap["time_ns"] = row["start_ns"]
    gap["tid"] += 1
    validate_result(native_trace)


def test_partial_running_coverage_does_not_prorate_counts_or_assign_one_core(native_trace):
    row = native_trace["intervals"][2]
    original = row["self_instructions"], row["self_cycles"]
    scheduling = row["scheduling"]
    scheduling.update(covered_ns=100, uncovered_ns=row["duration_ns"] - 100, states_ns={"Running": 100},
                      coverage_complete=False, single_running_core=None)
    scheduling["running_cores"][0]["duration_ns"] = 100
    native_trace["single_running_core_intervals"] -= 1
    validate_result(native_trace)
    assert (row["self_instructions"], row["self_cycles"]) == original
