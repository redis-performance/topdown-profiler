"""Native replay percentages remain distinct from additive pipeline-slot shares."""

import json
from pathlib import Path
import subprocess

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "examples" / "apple_gpu"


from topdown.collector import apple_gpu as REPLAY  # noqa: E402
from topdown import apple_gpu_report as REPORT  # noqa: E402


def native_profile():
    return {
        "measurement_kind": "apple-gpu-replay-profile", "tma_available": False, "device": "Apple M6",
        "scope": {"kind": "captured-metal-workload", "trace_name": "test.gputrace"},
        "settings": {"execution": "overlapping", "gpu_state": "default", "replayed": True},
        "timeline": {"GPU time": "0.2661 ms"}, "limitations": ["Replay measurements"],
        "encoders": [{"id": "ce0", "listing": {"name": "ce0", "values": [{"type": "string", "value": '"compute pass 0"'}]},
                      "counters": {"Cost": "100.00%", "Duration": "0.2661 ms", "Control Flow Limiter": "64,52%",
                                   "Integer and Conditional Limiter": "37,34%", "Kernel Occupancy": "21,25%",
                                   "Instructions": "18446744073709551615"}}],
    }


def test_report_preserves_native_values_without_normalizing_overlapping_limiters():
    profile = native_profile()
    report = REPORT.summarize(profile)
    row = report["encoders"][0]
    assert row["label"] == "compute pass 0" and row["cost_percent"] == 100
    assert row["limiters"] == [
        {"name": "Control Flow Limiter", "percent": 64.52, "native_value": "64,52%", "exceeds_100": False},
        {"name": "Integer and Conditional Limiter", "percent": 37.34, "native_value": "37,34%", "exceeds_100": False},
    ]
    assert sum(entry["percent"] for entry in row["limiters"]) > 100
    assert row["native_counters"] == profile["encoders"][0]["counters"]
    assert "do not sum to 100%" in REPORT.render(report) and "not application wall time" in REPORT.render(report)


@pytest.mark.parametrize("value", ["NaN%", "inf%", "100.1%", "-1%", "12.5", True, None])
def test_bad_native_percentages_fail_instead_of_fabricating_shares(value):
    with pytest.raises(ValueError):
        REPORT.percent(value)


def test_native_limiters_above_100_are_retained_and_flagged():
    profile = native_profile()
    profile["encoders"][0]["counters"]["Compute Shader Launch Limiter"] = "105,26%"
    result = REPORT.summarize(profile)
    limiter = result["encoders"][0]["limiters"][0]
    assert limiter["percent"] == 105.26 and limiter["exceeds_100"]
    assert "native value exceeds 100%" in REPORT.render(result)


@pytest.mark.parametrize("source", ["", "error: profiling unavailable", "[]", '{"ok":true}\nerror: failed'])
def test_non_json_tool_errors_are_rejected_even_when_tool_exits_zero(source):
    with pytest.raises(ValueError):
        REPLAY.json_documents(source)


def test_multiple_json_responses_and_whitespace():
    assert REPLAY.json_documents(' \n{"a":1}\n{\n"b":2\n}\n') == [{"a": 1}, {"b": 2}]


@pytest.fixture
def fake_gpudebug(monkeypatch):
    state = {"commands": [], "loaded": True, "performance_opened": False, "timeline_opened": False,
             "error": None, "pending": 0, "error_stream": "stdout", "warning": ""}
    profile = native_profile()

    def run(command, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["start_new_session"]
        state["commands"].append(command)
        if "-t" in command:
            response = "Session 42 created.\nProfile: not loaded (0 sessions available)\n"
        elif "--terminate" in command:
            response = "Session 42 terminated."
        else:
            instruction = command[command.index("-c") + 1]
            if instruction == state["error"]:
                response = "error: counter metadata unavailable"
            elif instruction == "go /performance/encoders" and state["pending"]:
                state["pending"] -= 1
                response = "error: 'encoders' has no info. Available actions: go"
            elif instruction.startswith("profile run"):
                response = "Profile data collected. (1s)"
            elif instruction == "profile":
                response = json.dumps({"state": "loaded" if state["loaded"] else "not_loaded"})
            elif instruction == "go /performance":
                state["performance_opened"] = True
                response = '{"children":[],"totalCount":0}'
            elif instruction == "go /performance/timeline/counters":
                state["timeline_opened"] = True
                response = '{"children":[],"totalCount":0}'
            elif instruction == "info /performance/timeline":
                assert state["timeline_opened"]
                response = json.dumps(profile["timeline"])
            elif instruction == "list --all" and state["timeline_opened"]:
                response = '{"children":[],"totalCount":0}'
            elif instruction in {"go /performance/encoders", "list --all"}:
                assert state["performance_opened"]
                response = json.dumps({"children": [profile["encoders"][0]["listing"]], "totalCount": 1})
            elif instruction == "info /performance/encoders/ce0":
                response = json.dumps(profile["encoders"][0]["counters"])
            elif instruction == "status":
                response = '{"device":"Apple M6"}'
            else:
                raise AssertionError(instruction)
        if response.startswith("error:") and state["error_stream"] == "stderr":
            return subprocess.CompletedProcess(command, 0, "", "\x1b[31m" + response + "\x1b[0m")
        return subprocess.CompletedProcess(command, 0, response, state["warning"])
    monkeypatch.setattr(REPLAY.subprocess, "run", run)
    return state


def test_collect_native_replay_preserves_metadata_and_closes_only_its_session(tmp_path, fake_gpudebug):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    output = tmp_path / "replay"
    profile = REPLAY.collect(trace, output)
    assert profile["encoders"][0]["counters"] == native_profile()["encoders"][0]["counters"]
    assert json.loads((output / "profile.json").read_text()) == profile
    assert fake_gpudebug["commands"][-1] == ["xcrun", "gpudebug", "--terminate", "42"]
    assert (output / "001.command.json").is_file() and (output / "001.stdout").is_file()
    assert REPORT.summarize(profile)["encoders"][0]["limiters"][0]["percent"] == 64.52


@pytest.mark.parametrize("failure", ["unsupported", "json-error"])
@pytest.mark.parametrize("error_stream", ["stdout", "stderr"])
def test_capture_failure_retains_logs_and_terminates_owned_session(tmp_path, fake_gpudebug, failure, error_stream):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    fake_gpudebug["error_stream"] = error_stream
    if failure == "unsupported":
        fake_gpudebug["loaded"] = False
    else:
        fake_gpudebug["error"] = "info /performance/timeline"
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="artifacts"):
        REPLAY.collect(trace, output)
    assert not (output / "profile.json").exists()
    assert fake_gpudebug["commands"][-1][-2:] == ["--terminate", "42"]
    assert list(output.glob("*.stdout"))


@pytest.mark.parametrize("error_stream", ["stdout", "stderr"])
def test_async_tables_are_retried_until_ready(tmp_path, fake_gpudebug, monkeypatch, error_stream):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    fake_gpudebug["pending"] = 1
    fake_gpudebug["error_stream"] = error_stream
    sleeps = []
    monkeypatch.setattr(REPLAY.time, "sleep", sleeps.append)
    result = REPLAY.collect(trace, tmp_path / "retry")
    assert result["encoders"] and sleeps == [0.25]


def test_native_stderr_warnings_are_retained_without_discarding_valid_results(tmp_path, fake_gpudebug):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    fake_gpudebug["warning"] = "warning: capture format is newer\n"
    output = tmp_path / "warnings"
    assert REPLAY.collect(trace, output)["encoders"]
    assert all(path.read_text() == fake_gpudebug["warning"] for path in output.glob("*.stderr"))


def test_performance_readiness_has_a_deadline(tmp_path, fake_gpudebug, monkeypatch):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    fake_gpudebug["pending"] = 10
    clock = [0]
    monkeypatch.setattr(REPLAY.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(REPLAY.time, "sleep", lambda _: clock.__setitem__(0, 31))
    with pytest.raises(ValueError, match="did not become ready"):
        REPLAY.collect(trace, tmp_path / "not-ready")
    assert fake_gpudebug["commands"][-1][-2:] == ["--terminate", "42"]


@pytest.mark.parametrize("replacement", [None, {}, {"values": None}, {"values": []}])
def test_missing_encoder_labels_fail_cleanly(replacement):
    profile = native_profile()
    profile["encoders"][0]["listing"] = replacement
    with pytest.raises(ValueError):
        REPORT.summarize(profile)


def test_encoder_labels_keep_their_own_quotes_and_zero_limiters_are_observations():
    profile = native_profile()
    row = profile["encoders"][0]
    row["listing"]["values"][0]["value"] = '""quoted""'
    row["counters"] = {"Cost": "0%", "Duration": "0 ns"}
    result = REPORT.summarize(profile)
    assert result["encoders"][0]["label"] == '"quoted"'
    assert result["encoders"][0]["limiters"] == []
    assert "unavailable" in REPORT.render(result)


def test_new_output_and_existing_trace_are_required(tmp_path, fake_gpudebug):
    with pytest.raises(ValueError, match="existing"):
        REPLAY.collect(tmp_path / "missing.gputrace", tmp_path / "unused")
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    with pytest.raises(FileExistsError):
        REPLAY.collect(trace, tmp_path)
    assert fake_gpudebug["commands"] == []


def test_report_cli_outputs_readable_and_json_views(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(native_profile()))
    import sys
    for flags in [[], ["--json"]]:
        result = subprocess.run([sys.executable, str(SCRIPTS / "report_replay.py"), str(path), *flags],
                                text=True, capture_output=True, check=True)
        if flags:
            assert json.loads(result.stdout)["encoders"][0]["limiters"][0]["percent"] == 64.52
        else:
            assert "Control Flow Limiter" in result.stdout


EVIDENCE = json.loads((SCRIPTS.parents[1] / "docs/apple-gpu-m6-validation.json").read_text())


@pytest.mark.parametrize("case", EVIDENCE["replay_cases"], ids=lambda case: case["case"])
def test_measured_m6_profiles_keep_native_values_and_sampling_coverage(case):
    profile = {"measurement_kind": "apple-gpu-replay-profile", "tma_available": False,
               "device": "Apple M6", "scope": {"kind": "captured-metal-workload", "trace_name": "test.gputrace"}, "settings": case["settings"], "timeline": case["timeline"],
               "encoders": [{"id": row["id"], "listing": {"values": [{"value": row["label"]}]},
                             "counters": row["native_counters"]} for row in case["encoders"]]}
    report = REPORT.summarize(profile)
    assert len(report["encoders"]) == case["probe"]["passes"]
    assert report["timeline"]["Cores"] in REPORT.render(report)
    originals = {row["id"]: row["native_counters"] for row in case["encoders"]}
    assert {row["id"]: row["native_counters"] for row in report["encoders"]} == originals
    assert case["probe"]["checksum_verified"]
    if case["case"].startswith("memory-"):
        assert any(entry["exceeds_100"] for row in report["encoders"] for entry in row["limiters"])


def test_replay_timestamps_can_be_imported_without_the_probe_wrapper(tmp_path, fake_gpudebug, sqlite_backend):
    from datetime import datetime
    from topdown.apple_storage import store_apple_result

    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    output = tmp_path / "replay"
    result = REPLAY.collect(trace, output)
    assert datetime.fromisoformat(result["started_at"]) <= datetime.fromisoformat(result["ended_at"])
    run = store_apple_result(sqlite_backend, output)
    assert run.labels["time_basis"] == "result_timestamps" and run.duration_seconds >= 0


def test_total_replay_budget_still_cleans_up_the_owned_session(tmp_path, fake_gpudebug, monkeypatch):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    native_run, clock = REPLAY.subprocess.run, [0]
    monkeypatch.setattr(REPLAY.time, "monotonic", lambda: clock[0])
    def run(command, **kwargs):
        response = native_run(command, **kwargs)
        if "-c" in command and command[command.index("-c") + 1].startswith("profile run"):
            clock[0] = 301
        return response
    monkeypatch.setattr(REPLAY.subprocess, "run", run)
    with pytest.raises(ValueError, match="300 second budget"):
        REPLAY.collect(trace, tmp_path / "timeout")
    assert fake_gpudebug["commands"][-1][-2:] == ["--terminate", "42"]


@pytest.mark.parametrize("failure", ["timeout", "exit-code"])
def test_partial_session_start_failures_are_cleaned_up(tmp_path, monkeypatch, failure):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "--terminate" in command:
            return subprocess.CompletedProcess(command, 0, "Session 42 terminated.", "")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 120, output=b"Session 42 created.\n", stderr=b"partial failure")
        return subprocess.CompletedProcess(command, 1, "Session 42 created.\n", "native failure")
    monkeypatch.setattr(REPLAY.subprocess, "run", run)
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="artifacts"):
        REPLAY.collect(trace, output)
    assert calls[-1][-2:] == ["--terminate", "42"]
    assert (output / "001.stdout").read_text() == "Session 42 created.\n"
    assert not (output / "profile.json").exists()


@pytest.mark.parametrize("already_failing", [True, False])
def test_cleanup_failure_does_not_hide_the_original_error(tmp_path, fake_gpudebug, monkeypatch, already_failing):
    trace = tmp_path / "test.gputrace"
    trace.mkdir()
    native_run = REPLAY.subprocess.run
    fake_gpudebug["loaded"] = not already_failing
    def run(command, **kwargs):
        response = native_run(command, **kwargs)
        if "--terminate" in command:
            return subprocess.CompletedProcess(command, 1, "", "cleanup failed")
        return response
    monkeypatch.setattr(REPLAY.subprocess, "run", run)
    with pytest.raises(ValueError, match="profiling is unavailable" if already_failing else "cleanup failed"):
        REPLAY.collect(trace, tmp_path / "cleanup")
    assert fake_gpudebug["commands"][-1][-2:] == ["--terminate", "42"]


def test_gpu_collect_retains_capture_host_identity(tmp_path, fake_gpudebug, monkeypatch):
    metadata = {"system": "Darwin", "arch": "arm64", "cpu": "Apple M6", "os_build": "26A434",
                "kernel_uuid": "CAPTURE-KERNEL"}
    monkeypatch.setattr(REPLAY, "capture_environment", lambda: metadata.copy())
    trace = tmp_path / "native.gputrace"
    trace.mkdir()
    output = tmp_path / "replay"
    result = REPLAY.collect(trace, output)
    assert result["environment"] == metadata
    assert json.loads((output / "profile.json").read_text())["environment"] == metadata


@pytest.mark.parametrize("source", [
    '{"Cost":"0%","Cost":"100%"}', '{"value":NaN}', '{"value":1e999}',
    '{"nested":' + '[' * 20000 + '0' + ']' * 20000 + '}',
], ids=["duplicate-cost", "nan", "overflowing-float", "nesting"])
def test_native_gpu_json_stream_rejects_ambiguous_or_unreadable_results(source):
    with pytest.raises(ValueError):
        REPLAY.json_documents(source)


@pytest.mark.parametrize("prefix", ['Trace label: "Session 999 created."\n', '{"name":"Session 999 created."}\n'])
def test_session_ownership_comes_from_complete_creation_messages(tmp_path, fake_gpudebug, monkeypatch, prefix):
    native_run = REPLAY.subprocess.run
    def run(command, **kwargs):
        response = native_run(command, **kwargs)
        if "-t" in command:
            response.stdout = prefix + response.stdout
        return response
    monkeypatch.setattr(REPLAY.subprocess, "run", run)
    trace = tmp_path / "owned.gputrace"
    trace.mkdir()
    REPLAY.collect(trace, tmp_path / "profile")
    assert fake_gpudebug["commands"][-1][-2:] == ["--terminate", "42"]
    assert all(command[command.index("-s") + 1] == "42" for command in fake_gpudebug["commands"] if "-s" in command)


@pytest.mark.parametrize("output", ["Session 42 created.\nSession 99 created.\n", 'Trace "Session 99 created."\n'])
@pytest.mark.parametrize("timeout", [False, True])
def test_ambiguous_or_missing_session_messages_never_signal_a_guessed_session(tmp_path, monkeypatch, output, timeout):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if timeout:
            raise subprocess.TimeoutExpired(command, 120, output=output.encode())
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(REPLAY.subprocess, "run", run)
    trace = tmp_path / "unknown.gputrace"
    trace.mkdir()
    artifacts = tmp_path / "failed"
    with pytest.raises(ValueError, match="artifacts"):
        REPLAY.collect(trace, artifacts)
    assert len(calls) == 1 and "--terminate" not in calls[0]
    assert (artifacts / "001.stdout").read_bytes() == output.encode()


def test_repeated_identical_creation_lines_are_unambiguous():
    assert REPLAY._created_session(b"Session 42 created.\r\nSession 42 created.\n") == "42"


def test_native_tty_creation_format_does_not_hide_its_session_identity():
    assert REPLAY._created_session(b"\r\x1b[2K| loading trace  (ctrl+b to run in background)\r\x1b[2KSession 218 created.\n") == "218"
