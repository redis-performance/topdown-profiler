"""Apple XML/lifecycle tests; PMU rows are synthetic, not measured M6 data."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest
from typer.testing import CliRunner

from topdown.cli import app
from topdown.collector import make_runner, resolve_collector
from topdown.collector.apple import AppleProfileError, doctor, export_profile, parse_profile, record_profile
from topdown.config import TopdownConfig


def xml_table(schema="time-profile", second_row=True):
    fields = {"time": "sample-time", "thread": "thread", "process": "process", "core": "core"}
    if schema == "cpu-pmc-value":
        fields.update({"sample-index": "uint64", "name": "pmc-event-name", "value": "uint64"})
        value = '<uint64>0</uint64><pmc-event-name fmt="FIXED_CYCLES">1</pmc-event-name><uint64>9007199254740993</uint64>'
        more = '<uint64>1</uint64><pmc-event-name fmt="FIXED_CYCLES">1</pmc-event-name><uint64>50</uint64>'
    else:
        fields.update({"weight": "cycle-weight" if schema == "cpu-profile" else "weight", "stack": "tagged-backtrace"})
        value = ('<weight id="w">1000000</weight><tagged-backtrace id="s">'
                 '<frame id="f" name="alu_work" addr="0x100"><binary name="probe"/></frame>'
                 '<frame ref="f"/></tagged-backtrace>')
        more = '<weight ref="w"/><tagged-backtrace ref="s"/>'
    cols = "".join(f"<col><mnemonic>{k}</mnemonic><engineering-type>{v}</engineering-type></col>"
                   for k, v in fields.items())
    row = ('<row><sample-time>100</sample-time><thread id="t"><tid>2</tid>'
           '<process id="p"><pid>42</pid></process></thread><process ref="p"/>'
           f'<core id="c" fmt="CPU 7 (P Core)">7</core>{value}</row>')
    row2 = (f'<row><sample-time>200</sample-time><thread ref="t"/><process ref="p"/><core ref="c"/>{more}</row>'
            if second_row else "")
    return f'<trace-query-result><node><schema name="{schema}">{cols}</schema>{row}{row2}</node></trace-query-result>'


@pytest.fixture
def write_xml(tmp_path):
    def write(text):
        p = tmp_path / "samples.xml"
        p.write_text(text)
        return p
    return write


def test_time_profile_resolves_ids_and_keeps_native_units(write_xml):
    p = parse_profile(write_xml(xml_table()), "time-profile", 42)
    assert p["sample_count"] == 2
    assert p["samples"][1]["weight"] == 1000000
    assert p["samples"][1]["frames"][1]["name"] == "alu_work"
    assert p["leaf_sample_counts"][0]["samples"] == 2
    assert p["samples"][0]["core_label"] == "CPU 7 (P Core)"
    assert set(p["tma"].values()) == {None}


def test_cycle_weights_are_not_timer_weights(write_xml):
    p = parse_profile(write_xml(xml_table("cpu-profile")), "cpu-profile", 42)
    assert p["samples"][0]["weight_type"] == "cycle-weight"


def test_native_pmc_values_preserve_uint64_without_summing(write_xml):
    p = parse_profile(write_xml(xml_table("cpu-pmc-value")), "cpu-pmc-value", 42)
    assert [s["value"] for s in p["samples"]] == [9007199254740993, 50]
    assert p["samples"][0]["event"] == "FIXED_CYCLES"
    assert p["samples"][0]["native_event_id"] == "1"
    assert p["column_types"]["value"] == "uint64"
    assert p["leaf_sample_counts"] == []
    assert all(value is None for value in p["tma"].values())


def test_half_open_time_window(write_xml):
    p = parse_profile(write_xml(xml_table()), "time-profile", 42, 100, 200)
    assert p["sample_count"] == 1
    assert p["excluded_rows"] == 1


@pytest.mark.parametrize("pid,start,end", [(0, 0, None), (42, -1, None), (42, 100, 100)])
def test_invalid_scope(write_xml, pid, start, end):
    with pytest.raises(AppleProfileError, match="Require positive PID"):
        parse_profile(write_xml(xml_table()), "time-profile", pid, start, end)


def test_no_rows_for_target_is_not_zero_measurement(write_xml):
    with pytest.raises(AppleProfileError, match="No time-profile samples"):
        parse_profile(write_xml(xml_table()), "time-profile", 99)


@pytest.mark.parametrize("old,new,message", [
    ('ref="p"', 'ref="missing"', "reference"),
    ('<core id="c"', '<core id="p"', "Duplicate"),
    ('<sample-time>100</sample-time>', '<sample-time>NaN</sample-time>', "invalid integer"),
    ('<sample-time>100</sample-time>', '<sample-time>-1</sample-time>', "Negative"),
    ('<engineering-type>weight</engineering-type>', '<engineering-type>cycle-weight</engineering-type>', "Unknown"),
    ('<mnemonic>stack</mnemonic>', '<mnemonic>unknown</mnemonic>', "Missing"),
])
def test_malformed_data_fails_closed(write_xml, old, new, message):
    with pytest.raises(AppleProfileError, match=message):
        parse_profile(write_xml(xml_table().replace(old, new)), "time-profile", 42)


def test_timer_schema_cannot_be_imported_as_pmu(write_xml):
    with pytest.raises(AppleProfileError, match="Expected exactly one cpu-pmc-value"):
        parse_profile(write_xml(xml_table()), "cpu-pmc-value", 42)


def test_thread_pid_must_agree_with_explicit_process(write_xml):
    data = xml_table().replace('<process ref="p"/>', '<process><pid>77</pid></process>')
    with pytest.raises(AppleProfileError, match="PID disagree"):
        parse_profile(write_xml(data), "time-profile", 77)


def fake_export(cmd, **kwargs):
    path = Path(cmd[cmd.index("--output") + 1])
    if "--toc" in cmd:
        path.write_text('<trace-toc><run number="1"><info><target><process pid="42" return-exit-status="0"/>'
                        '</target></info><data><table schema="time-profile"/></data></run></trace-toc>')
    else:
        path.write_text(xml_table())
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_native_export_with_automatic_target_and_checksums(tmp_path):
    output = tmp_path / "out"
    with patch("subprocess.run", side_effect=fake_export) as mock:
        p = export_profile(tmp_path / "input.trace", output, "time")
    assert p["scope"]["pid"] == 42
    assert p["measurement_kind"] == "timer-sampled-stacks"
    assert p["target_exit_status"] == "0"
    assert len(p["raw_xml_sha256"]) == 64
    assert json.loads((output / "profile.json").read_text())["sample_count"] == 2
    assert mock.call_count == 2


def test_counterless_successful_trace_is_rejected_without_fallback(tmp_path):
    with patch("subprocess.run", side_effect=fake_export) as mock:
        with pytest.raises(AppleProfileError, match="do not establish PMU access"):
            export_profile(tmp_path / "input.trace", tmp_path / "out", "counters")
    assert mock.call_count == 1
    assert not (tmp_path / "out/profile.json").exists()


def test_export_refuses_to_replace_existing_artifacts(tmp_path):
    (tmp_path / "toc.xml").touch()
    with patch("subprocess.run") as mock:
        with pytest.raises(AppleProfileError, match="already exist"):
            export_profile(tmp_path / "input.trace", tmp_path, "time")
    mock.assert_not_called()


def test_failed_recorder_keeps_logs_and_no_success_profile(tmp_path):
    output = tmp_path / "capture"
    failed = subprocess.CompletedProcess([], 2, "Recording failed", "Failed set kpc configuration: Operation not permitted")
    with patch("topdown.collector.apple.doctor", return_value={}), patch("subprocess.run", return_value=failed):
        with pytest.raises(AppleProfileError, match="kpc configuration"):
            record_profile(output, [__file__])
    assert json.loads((output / "capture.json").read_text())["status"] == "failed"
    assert "Operation not permitted" in (output / "record.stderr").read_text()
    assert not (output / "profile.json").exists()


def test_timeout_keeps_partial_output_and_no_retry(tmp_path):
    output = tmp_path / "capture"
    error = subprocess.TimeoutExpired([], 5, output=b"partial", stderr=b"denied")
    with patch("topdown.collector.apple.doctor", return_value={}), patch("subprocess.run", side_effect=error) as mock:
        with pytest.raises(AppleProfileError, match="timed out"):
            record_profile(output, [__file__])
    assert (output / "record.stdout").read_text() == "partial"
    assert mock.call_count == 1


def test_record_argv_has_single_target_no_shell_and_no_prompt(tmp_path):
    output = tmp_path / "capture"
    with patch("topdown.collector.apple.doctor", return_value={}), \
         patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mock, \
         patch("topdown.collector.apple.export_profile", return_value={"sample_count": 1}):
        record_profile(output, [__file__, 'literal; $(command)'], mode="time")
    cmd = mock.call_args.args[0]
    assert cmd[cmd.index("--launch"): ] == ["--launch", "--", __file__, 'literal; $(command)']
    assert "--no-prompt" in cmd
    assert "--all-processes" not in cmd
    assert "shell" not in mock.call_args.kwargs


def test_doctor_does_not_collect_or_change_settings():
    def fake(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "Apple M6\nCPU Counters\nCPU Profiler\nTime Profiler\n", "")
    with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"), \
         patch("subprocess.run", side_effect=fake) as mock:
        result = doctor()
    assert result["hardware_access"] == "unverified"
    assert len(mock.call_args_list) == 7
    assert all("record" not in call.args[0] and "-enable" not in call.args[0] for call in mock.call_args_list)


def test_darwin_routes_away_from_linux_tools():
    with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"):
        assert resolve_collector(TopdownConfig()) == "apple"
        with pytest.raises(RuntimeError, match="topdown apple doctor"):
            make_runner(TopdownConfig(), [42], False, 2)
        with patch("topdown.collector.toplev.check_perf_permissions") as mock:
            result = CliRunner().invoke(app, ["collect", "--process", "probe"])
        assert result.exit_code == 1
        assert "topdown apple doctor" in result.output
        mock.assert_not_called()


def test_cli_launch_arguments(tmp_path):
    with patch("topdown.apple_cli.record_profile", return_value={"sample_count": 2, "measurement_kind": "native", "tma": {}}) as mock:
        result = CliRunner().invoke(app, ["apple", "record", "--output", str(tmp_path / "new"),
                                          "--mode", "time", "--", "/bin/probe", "--flag", "literal value"])
    assert result.exit_code == 0, result.output
    assert mock.call_args.args[1] == ["/bin/probe", "--flag", "literal value"]
    assert json.loads(result.output)["sample_count"] == 2


def test_mcp_darwin_does_not_try_linux_perf():
    from topdown.mcp_server import collect_topdown
    with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"), \
         patch("topdown.collector.toplev.check_perf_permissions") as mock:
        result = collect_topdown("probe")
    assert "topdown apple doctor" in result
    mock.assert_not_called()


def test_measured_m6_timer_excerpt():
    path = Path(__file__).parent / "test_data/apple_time_profile_m6.xml"
    pid = int(ET.parse(path).findtext(".//process/pid"))
    p = parse_profile(path, "time-profile", pid)
    assert p["sample_count"] == 3
    assert all(s["frames"][0]["name"] == "alu_work" for s in p["samples"])
    assert sum(x["samples"] for x in p["leaf_sample_counts"]) == 3
    assert p["samples"][0]["time_ns"] < p["samples"][-1]["time_ns"]
    assert not any(p["tma"].values())


def test_pmu_overflow_fails(write_xml):
    data = xml_table("cpu-pmc-value").replace("9007199254740993", str(2**64))
    with pytest.raises(AppleProfileError, match="exceeds uint64"):
        parse_profile(write_xml(data), "cpu-pmc-value", 42)


def test_cyclic_reference_fails(write_xml):
    data = xml_table().replace('<process id="p"><pid>42</pid></process>', '<process id="p" ref="p"/>')
    with pytest.raises(AppleProfileError, match="Cyclic"):
        parse_profile(write_xml(data), "time-profile", 42)


def test_malformed_xml_fails(write_xml):
    with pytest.raises(AppleProfileError, match="Cannot read Instruments XML"):
        parse_profile(write_xml("<broken>"), "time-profile", 42)


@pytest.mark.parametrize("mode,duration,start,end", [("bogus", 5, 0, None), ("time", 0, 0, None),
                                                   ("time", 5, 100, 1)])
def test_invalid_recording_request_never_launches(tmp_path, mode, duration, start, end):
    with patch("subprocess.run") as mock:
        with pytest.raises(AppleProfileError):
            record_profile(tmp_path / "capture", [__file__], duration, mode, start_ns=start, end_ns=end)
    mock.assert_not_called()


def test_recording_requires_real_executable_path(tmp_path):
    with pytest.raises(AppleProfileError, match="executable path"):
        record_profile(tmp_path / "capture", [])


def test_interrupted_recorder_marks_capture(tmp_path):
    with patch("topdown.collector.apple.doctor", return_value={}), \
         patch("subprocess.run", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            record_profile(tmp_path / "capture", [__file__])
    metadata = json.loads((tmp_path / "capture/capture.json").read_text())
    assert metadata["status"] == "interrupted"


def test_missing_xcrun_is_actionable(tmp_path):
    with patch("subprocess.run", side_effect=FileNotFoundError("xcrun")):
        with pytest.raises(AppleProfileError, match="Cannot execute xcrun"):
            export_profile(tmp_path / "input.trace", tmp_path / "out", "time")


def test_doctor_on_linux_does_not_launch_tools():
    with patch("platform.system", return_value="Linux"), patch("subprocess.run") as mock:
        result = doctor()
    assert "requires macOS" in result["error"]
    mock.assert_not_called()


def test_doctor_retains_tool_failures():
    with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"), \
         patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "missing")):
        result = doctor()
    assert result["xctrace"] is None
    assert result["errors"]["xctrace"] == "missing"


def test_cli_doctor_failure_exit_code():
    with patch("topdown.apple_cli.doctor", return_value={"error": "requires macOS"}):
        result = CliRunner().invoke(app, ["apple", "doctor"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "requires macOS"


def test_cli_recorder_failure(tmp_path):
    with patch("topdown.apple_cli.record_profile", side_effect=AppleProfileError("KPC denied")):
        result = CliRunner().invoke(app, ["apple", "record", "--output", str(tmp_path / "out"), "--", "/probe"])
    assert result.exit_code == 1
    assert "KPC denied" in result.output


def test_cli_exports_native_profile(tmp_path):
    trace = tmp_path / "input.trace"
    trace.mkdir()
    with patch("subprocess.run", side_effect=fake_export):
        result = CliRunner().invoke(app, ["apple", "export", str(trace), "--output", str(tmp_path / "out"),
                                          "--mode", "time"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["measurement_kind"] == "timer-sampled-stacks"


def test_cli_export_errors_are_reported_without_success(tmp_path):
    trace = tmp_path / "input.trace"
    trace.mkdir()
    with patch("topdown.apple_cli.export_profile", side_effect=AppleProfileError("No PMU")):
        result = CliRunner().invoke(app, ["apple", "export", str(trace), "--output", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "No PMU" in result.output
    existing = CliRunner().invoke(app, ["apple", "export", str(trace), "--output", str(tmp_path)])
    assert existing.exit_code == 1
    assert "new export directory" in existing.output
