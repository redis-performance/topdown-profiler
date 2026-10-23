"""Native Apple imports must preserve accounting identities and measurement provenance."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from topdown.apple_storage import store_apple_result, validate_result
from topdown.collector.apple import AppleProfileError, TMA_ROOTS
from topdown.collector.apple_soc import _deltas


@pytest.fixture
def native_stat():
    return {"measurement_kind": "process-accounted-fixed-counters", "tma_available": False,
            "tma": dict.fromkeys(TMA_ROOTS), "scope": {"pid": 42, "descendants": False},
            "counters": {"instructions": 100, "cycles": 200, "performance_instructions": 60,
                         "other_instructions": 40, "performance_cycles": 150, "other_cycles": 50}}


@pytest.fixture
def native_soc():
    after = json.loads((Path(__file__).parent / "test_data/apple_soc_dram_stall_m6.json").read_text())
    before = deepcopy(after)
    for state in before["states"]:
        state.update(value=0, intransitions=0)
    return {"measurement_kind": "native-soc-firmware-state-observations", "tma_available": False,
            "tma": dict.fromkeys(TMA_ROOTS), "scope": "system-and-cluster", "channel_count": 1,
            "channels": _deltas([before], [after])}


def write(tmp_path, result, name="stat.json"):
    path = tmp_path / name
    path.write_text(json.dumps(result))
    return path


@pytest.mark.parametrize("change", [
    {"performance_instructions": 101}, {"other_instructions": 41}, {"performance_cycles": 201},
    {"other_cycles": 51}, {"performance_user_time_ns": 1, "other_user_time_ns": 2},
    {"user_time_ns": 10, "performance_user_time_ns": 8, "other_user_time_ns": 3},
    {"system_time_ns": 10, "performance_system_time_ns": 8, "other_system_time_ns": 3},
])
def test_inconsistent_core_accounting_is_rejected_before_insert(sqlite_backend, tmp_path, native_stat, change):
    native_stat["counters"].update(change)
    with pytest.raises(AppleProfileError, match="core|aggregate|total"):
        store_apple_result(sqlite_backend, write(tmp_path, native_stat))
    assert sqlite_backend.list_runs() == []


@pytest.mark.parametrize("field", ["performance_instructions", "other_instructions", "performance_cycles", "other_cycles"])
def test_partial_core_accounting_pairs_are_rejected(native_stat, field):
    del native_stat["counters"][field]
    with pytest.raises(AppleProfileError, match="core|pair"):
        validate_result(native_stat)


def test_valid_maximum_integer_core_accounting_and_legacy_aggregates_are_retained(sqlite_backend, tmp_path, native_stat):
    native_stat["counters"].update(instructions=2**64 - 1, performance_instructions=2**63,
                                   other_instructions=2**63 - 1)
    original = deepcopy(native_stat)
    run = store_apple_result(sqlite_backend, write(tmp_path, native_stat))
    assert sqlite_backend.get_apple_result(run.run_id)["result"] == original
    for field in ["performance_instructions", "other_instructions", "performance_cycles", "other_cycles"]:
        del native_stat["counters"][field]
    validate_result(native_stat)


@pytest.mark.parametrize("field,value", [("descendants", True), ("threads", "main"), ("window", "system"),
                                         ("execution_levels", "user-only")])
def test_stat_scope_cannot_claim_an_unmeasured_domain(native_stat, field, value):
    native_stat["scope"][field] = value
    with pytest.raises(AppleProfileError, match="scope|process|threads|descendants"):
        validate_result(native_stat)


@pytest.mark.parametrize("change", ["total", "share", "available", "duplicate-state", "duplicate-channel",
                                    "state-id", "state-label", "transitions", "channel-id", "unit", "group",
                                    "channel-count", "scope", "total-available"])
def test_inconsistent_soc_accounting_is_rejected_before_insert(sqlite_backend, tmp_path, native_soc, change):
    channel = native_soc["channels"][0]
    if change == "total":
        channel["native_value_total"] += 1
    elif change == "share":
        channel["states"][0]["share_of_reported_values"] = 0.9
    elif change == "available":
        channel["observations_available"] = False
    elif change == "duplicate-state":
        channel["states"].append(deepcopy(channel["states"][0]))
    elif change == "duplicate-channel":
        native_soc["channels"].append(deepcopy(channel))
        native_soc["channel_count"] = 2
    elif change.startswith("state-"):
        channel["states"][0][change[6:]] = True
    elif change == "transitions":
        channel["states"][0]["intransitions"] = -1
    elif change == "channel-id":
        channel["id"] = True
    elif change in {"unit", "group"}:
        channel[change] = None
    elif change == "channel-count":
        native_soc["channel_count"] = 2
    elif change == "scope":
        native_soc["scope"] = {"pid": 42}
    else:
        native_soc["observations_available"] = False
    with pytest.raises(AppleProfileError):
        store_apple_result(sqlite_backend, write(tmp_path, native_soc, "soc.json"))
    assert sqlite_backend.list_runs() == []


def test_soc_unobserved_states_keep_null_shares_and_large_delta_values_are_not_sentinels(native_soc):
    channel = native_soc["channels"][0]
    for state in channel["states"]:
        state.update(value=0, share_of_reported_values=None)
    channel.update(native_value_total=0, observations_available=False)
    validate_result(native_soc)
    channel["states"][0].update(value=2**63, share_of_reported_values=1.0)
    for state in channel["states"][1:]:
        state["share_of_reported_values"] = 0.0
    channel.update(native_value_total=2**63, observations_available=True)
    validate_result(native_soc)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_nonfinite_capture_metadata_is_rejected_before_insert(sqlite_backend, tmp_path, native_stat, constant):
    path = write(tmp_path, native_stat, "profile.json")
    (tmp_path / "capture.json").write_text('{"environment":{"unverified":' + constant + '}}')
    with pytest.raises(AppleProfileError, match="JSON|finite"):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []


@pytest.mark.parametrize("name", ["stat.json", "capture.json"])
def test_duplicate_native_json_keys_are_rejected_before_insert(sqlite_backend, tmp_path, native_stat, name):
    if name == "stat.json":
        path = write(tmp_path, native_stat)
        path.write_text(path.read_text().replace('"instructions": 100', '"instructions": 1, "instructions": 100'))
    else:
        path = write(tmp_path, native_stat, "profile.json")
        (tmp_path / name).write_text('{"environment":{"arch":"x86_64","arch":"arm64"}}')
    with pytest.raises(AppleProfileError, match="duplicate|Duplicate"):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []


@pytest.mark.parametrize("dates", [{"started_at": ""}, {"started_at": None}, {"ended_at": "2026-10-23T12:00:00Z"},
                                  {"started_at": "0001-01-01T00:00:00+01:00"}])
def test_invalid_present_timestamps_are_not_replaced_with_import_time(sqlite_backend, tmp_path, native_stat, dates):
    native_stat.update(dates)
    with pytest.raises(AppleProfileError, match="timestamp|date"):
        store_apple_result(sqlite_backend, write(tmp_path, native_stat))
    assert sqlite_backend.list_runs() == []


def test_unused_capture_timestamps_are_validated_too(sqlite_backend, tmp_path, native_stat):
    native_stat.update(started_at="2026-10-23T10:00:00Z", ended_at="2026-10-23T10:00:01Z")
    path = write(tmp_path, native_stat, "profile.json")
    (tmp_path / "capture.json").write_text('{"started_at":"yesterday"}')
    with pytest.raises(AppleProfileError, match="timestamp|date"):
        store_apple_result(sqlite_backend, path)
    assert sqlite_backend.list_runs() == []


def test_utc_z_timestamps_preserve_the_source_and_keep_utc_ordering(sqlite_backend, tmp_path, native_stat):
    native_stat.update(started_at="2026-10-23T10:00:00Z", ended_at="2026-10-23T10:00:01Z")
    run = store_apple_result(sqlite_backend, write(tmp_path, native_stat))
    assert run.started_at == datetime(2026, 10, 23, 10, tzinfo=timezone.utc) and run.duration_seconds == 1
    assert sqlite_backend.get_apple_result(run.run_id)["result"] == native_stat


@pytest.fixture
def interval_stat(native_stat):
    counters = native_stat["counters"]
    before = {**counters, "process_start_mach": 10, "process_exit_mach": 0}
    after = {**{key: 2 * value for key, value in counters.items()},
             "process_start_mach": 10, "process_exit_mach": 0}
    native_stat.update(snapshot_before=before, snapshot_after=after, observed_duration_ns=100, counters_available=True)
    native_stat["scope"]["window"] = "observed-interval"
    return native_stat


@pytest.mark.parametrize("change", ["before-missing", "after-missing", "scope", "identity", "exited", "exit-order",
                                    "difference", "layout", "snapshot-components", "duration", "available"])
def test_interval_import_must_agree_with_its_native_snapshots(interval_stat, change):
    if change == "before-missing":
        del interval_stat["snapshot_before"]
    elif change == "after-missing":
        del interval_stat["snapshot_after"]
    elif change == "scope":
        interval_stat["scope"]["window"] = "process-lifetime"
    elif change == "identity":
        interval_stat["snapshot_after"]["process_start_mach"] = 11
    elif change == "exited":
        interval_stat["snapshot_before"]["process_exit_mach"] = 20
    elif change == "exit-order":
        interval_stat["snapshot_after"]["process_exit_mach"] = 1
    elif change == "difference":
        interval_stat["counters"]["instructions"] += 1
        interval_stat["counters"]["other_instructions"] += 1
    elif change == "layout":
        interval_stat["snapshot_after"]["unknown"] = 0
    elif change == "snapshot-components":
        interval_stat["snapshot_after"]["other_instructions"] += 1
    elif change == "duration":
        interval_stat["observed_duration_ns"] = True
    else:
        interval_stat["counters_available"] = False
    with pytest.raises(AppleProfileError):
        validate_result(interval_stat)


def test_observed_idle_interval_keeps_zero_deltas_and_available_lifetime_counters(interval_stat):
    interval_stat["snapshot_after"] = deepcopy(interval_stat["snapshot_before"])
    interval_stat["counters"] = dict.fromkeys(interval_stat["counters"], 0)
    validate_result(interval_stat)
    interval_stat["snapshot_after"]["process_exit_mach"] = 20
    validate_result(interval_stat)


def test_valid_interval_snapshots_round_trip_exactly(sqlite_backend, tmp_path, interval_stat):
    run = store_apple_result(sqlite_backend, write(tmp_path, interval_stat))
    assert sqlite_backend.get_apple_result(run.run_id)["result"] == interval_stat


@pytest.mark.parametrize("flag", [0, 1, None, "true"])
def test_counter_availability_requires_boolean_metadata(native_stat, flag):
    native_stat["counters_available"] = flag
    with pytest.raises(AppleProfileError):
        validate_result(native_stat)


def test_cli_and_mcp_imports_share_the_same_accounting_checks(tmp_path, native_stat, monkeypatch):
    from typer.testing import CliRunner
    from topdown.cli import app
    from topdown.config import TopdownConfig
    from topdown import mcp_server

    native_stat["counters"]["performance_instructions"] = 101
    path = write(tmp_path, native_stat)
    db = tmp_path / "rejected.db"
    cli = CliRunner().invoke(app, ["apple", "store", str(path), "--db", str(db)])
    assert cli.exit_code == 1 and "aggregate total" in cli.stderr and not cli.stdout
    monkeypatch.setattr(mcp_server, "get_config", lambda: TopdownConfig(db_path=db))
    result = mcp_server.import_apple_result(str(path))
    assert "aggregate total" in result["error"]
    assert mcp_server.list_profiling_runs() == "No runs found."


@pytest.fixture
def guided_result(tmp_path):
    from tests.test_apple_bottlenecks import metric_xml
    from topdown.apple_storage import PROFILE_SCHEMAS
    from topdown.collector.apple import parse_apple_bottlenecks

    path = tmp_path / 'guided.xml'
    path.write_text(metric_xml())
    result = parse_apple_bottlenecks(path, 42)
    result.update(measurement_kind=next(k for k,v in PROFILE_SCHEMAS.items() if v=='MetricTable'),
                  invalid_cycles_with_known_count=0, invalid_intervals_without_cycle_count=0,
                  valid_cycle_fraction_of_scoped_intervals=1.0)
    return result


@pytest.mark.parametrize('change', [
    {'apple_bottlenecks_available':False}, {'apple_bottlenecks_available':1},
    {'invalid_cycles_with_known_count':-1}, {'invalid_cycles_with_known_count':True},
    {'invalid_intervals_without_cycle_count':1}, {'invalid_intervals_without_cycle_count':False},
    {'invalid_intervals':1,'invalid_cycles_with_known_count':100},
    {'invalid_intervals':1,'invalid_cycles_with_known_count':2**64,
     'valid_cycle_fraction_of_scoped_intervals':400/(400+2**64)},
    {'valid_cycle_fraction_of_scoped_intervals':.5}, {'valid_cycle_fraction_of_scoped_intervals':True},
    {'valid_cycle_fraction_of_scoped_intervals':None},
    {'invalid_intervals':1,'invalid_intervals_without_cycle_count':1,'valid_cycle_fraction_of_scoped_intervals':1},
])
def test_guided_coverage_claims_must_follow_native_interval_accounting(sqlite_backend,tmp_path,guided_result,change):
    guided_result.update(change)
    with pytest.raises(AppleProfileError,match='coverage|availability'):
        store_apple_result(sqlite_backend,write(tmp_path,guided_result,'profile.json'))
    assert sqlite_backend.list_runs()==[]


@pytest.mark.parametrize('missing', ['invalid_cycles_with_known_count','invalid_intervals_without_cycle_count',
                                     'valid_cycle_fraction_of_scoped_intervals'])
def test_incomplete_guided_coverage_is_rejected(guided_result,missing):
    del guided_result[missing]
    with pytest.raises(AppleProfileError,match='coverage'):
        validate_result(guided_result)


def test_guided_known_unknown_and_legacy_coverage_remain_distinct(guided_result):
    guided_result.update(invalid_intervals=2,invalid_cycles_with_known_count=100,
                         valid_cycle_fraction_of_scoped_intervals=.8)
    validate_result(guided_result)
    guided_result.update(invalid_intervals_without_cycle_count=1,valid_cycle_fraction_of_scoped_intervals=None)
    validate_result(guided_result)
    for key in ['invalid_cycles_with_known_count','invalid_intervals_without_cycle_count',
                'valid_cycle_fraction_of_scoped_intervals']:
        del guided_result[key]
    validate_result(guided_result)


def test_nested_programmatic_native_json_fails_with_an_artifact_error(native_stat):
    nested=[]
    native_stat['metadata']=nested
    for _ in range(20000):
        child=[]
        nested.append(child)
        nested=child
    with pytest.raises(AppleProfileError,match='Invalid native Apple JSON'):
        validate_result(native_stat)


@pytest.mark.parametrize("pid", [2**31, 2**32 + 42, 2**64 - 1])
def test_import_rejects_impossible_native_process_identities(sqlite_backend, tmp_path, native_stat, pid):
    native_stat["scope"]["pid"] = pid
    with pytest.raises(AppleProfileError, match="native signed pid_t"):
        store_apple_result(sqlite_backend, write(tmp_path, native_stat))
    assert sqlite_backend.list_runs() == []
