"""Tests for the AMD perf-stat fallback collector."""

from unittest.mock import MagicMock, patch

import pytest

from topdown.collector.perf_stat_amd import (
    _EVENTS,
    _compute_l1,
    _event_to_key,
    PerfStatAmdOptions,
    PerfStatAmdRunner,
    parse_perf_stat_amd_output,
)


# ── event-name mapping ──────────────────────────────────────────────


class TestEventToKey:
    def test_canonical(self):
        assert _event_to_key("de_src_op_disp.all") == "dispatched"
        assert _event_to_key("ex_ret_ops") == "retired"
        assert _event_to_key("ex_ret_brn_misp") == "br_misp"
        assert _event_to_key("de_no_dispatch_per_slot.no_ops_from_frontend") == "fe_no_disp"
        assert _event_to_key("de_no_dispatch_per_slot.backend_stalls") == "be_stall"
        assert _event_to_key("cpu-cycles") == "cycles"

    def test_uppercase_tolerated(self):
        assert _event_to_key("DE_SRC_OP_DISP.ALL") == "dispatched"

    def test_cpu_prefix_stripped(self):
        assert _event_to_key("cpu/de_src_op_disp.all/") == "dispatched"
        assert _event_to_key("cpu_core/ex_ret_ops/") == "retired"

    def test_unknown(self):
        assert _event_to_key("some_random_event") is None
        assert _event_to_key("") is None


# ── L1 computation ──────────────────────────────────────────────────


class TestComputeL1:
    def test_sums_to_100(self):
        result = _compute_l1({
            "dispatched": 1000.0,
            "retired": 500.0,
            "br_misp": 50.0,
            "fe_no_disp": 150.0,
            "be_stall": 250.0,
            "cycles": 1000.0,
        })
        assert result is not None
        total = sum(result.values())
        assert total == pytest.approx(100.0)

    def test_missing_denominator(self):
        assert _compute_l1({"retired": 100.0}) is None

    def test_zero_denominator(self):
        assert _compute_l1({"dispatched": 0.0, "retired": 100.0}) is None

    def test_returns_all_four_l1(self):
        result = _compute_l1({
            "dispatched": 1000.0, "retired": 500.0, "br_misp": 20.0,
            "fe_no_disp": 100.0, "be_stall": 300.0, "cycles": 1000.0,
        })
        assert set(result) == {"Retiring", "Frontend_Bound", "Backend_Bound", "Bad_Speculation"}

    def test_retiring_percentage(self):
        result = _compute_l1({
            "dispatched": 1000.0, "retired": 600.0, "br_misp": 10.0,
            "fe_no_disp": 100.0, "be_stall": 100.0, "cycles": 1000.0,
        })
        # Retiring should be roughly 60% (before normalization to 100)
        # After normalization total = 60+10+10+20=100 so retiring stays 60.
        # br_misp ignored in ratio calc — Bad_Speculation comes from residual
        assert result["Retiring"] == pytest.approx(60.0, rel=1e-3)


# ── parser ──────────────────────────────────────────────────────────


class TestParser:
    def test_basic_interval(self):
        # perf stat -I 1000 -x, output with 6 events
        text = (
            "1.001,1200000,,de_src_op_disp.all,1000000000,100.00,,\n"
            "1.001,720000,,ex_ret_ops,1000000000,100.00,,\n"
            "1.001,24000,,ex_ret_brn_misp,1000000000,100.00,,\n"
            "1.001,120000,,de_no_dispatch_per_slot.no_ops_from_frontend,1000000000,100.00,,\n"
            "1.001,240000,,de_no_dispatch_per_slot.backend_stalls,1000000000,100.00,,\n"
            "1.001,180000,,cpu-cycles,1000000000,100.00,,\n"
        )
        samples = parse_perf_stat_amd_output(text)
        assert len(samples) == 4
        names = {s.metric_name for s in samples}
        assert names == {"Retiring", "Frontend_Bound", "Backend_Bound", "Bad_Speculation"}
        vals = {s.metric_name: s.value for s in samples}
        assert sum(vals.values()) == pytest.approx(100.0)
        # Retiring should be ~60% (720k / 1200k)
        assert vals["Retiring"] == pytest.approx(60.0, rel=1e-3)

    def test_two_intervals(self):
        text = (
            "1.001,1200000,,de_src_op_disp.all,1000000000,100.00,,\n"
            "1.001,720000,,ex_ret_ops,1000000000,100.00,,\n"
            "1.001,24000,,ex_ret_brn_misp,1000000000,100.00,,\n"
            "1.001,120000,,de_no_dispatch_per_slot.no_ops_from_frontend,1000000000,100.00,,\n"
            "1.001,240000,,de_no_dispatch_per_slot.backend_stalls,1000000000,100.00,,\n"
            "1.001,180000,,cpu-cycles,1000000000,100.00,,\n"
            "2.002,1250000,,de_src_op_disp.all,1000000000,100.00,,\n"
            "2.002,780000,,ex_ret_ops,1000000000,100.00,,\n"
            "2.002,22000,,ex_ret_brn_misp,1000000000,100.00,,\n"
            "2.002,110000,,de_no_dispatch_per_slot.no_ops_from_frontend,1000000000,100.00,,\n"
            "2.002,230000,,de_no_dispatch_per_slot.backend_stalls,1000000000,100.00,,\n"
            "2.002,190000,,cpu-cycles,1000000000,100.00,,\n"
        )
        samples = parse_perf_stat_amd_output(text)
        assert len(samples) == 8  # 2 intervals x 4 metrics

    def test_skips_not_counted(self):
        text = (
            "1.001,<not counted>,,de_src_op_disp.all,1000000000,100.00,,\n"
        )
        samples = parse_perf_stat_amd_output(text)
        assert samples == []

    def test_skips_comments(self):
        text = (
            "# started on ...\n"
            "1.001,1200000,,de_src_op_disp.all,1000000000,100.00,,\n"
            "1.001,720000,,ex_ret_ops,1000000000,100.00,,\n"
            "1.001,24000,,ex_ret_brn_misp,1000000000,100.00,,\n"
            "1.001,120000,,de_no_dispatch_per_slot.no_ops_from_frontend,1000000000,100.00,,\n"
            "1.001,240000,,de_no_dispatch_per_slot.backend_stalls,1000000000,100.00,,\n"
            "1.001,180000,,cpu-cycles,1000000000,100.00,,\n"
        )
        samples = parse_perf_stat_amd_output(text)
        assert len(samples) == 4

    def test_unknown_events_ignored(self):
        text = (
            "1.001,1200000,,de_src_op_disp.all,1000000000,100.00,,\n"
            "1.001,720000,,ex_ret_ops,1000000000,100.00,,\n"
            "1.001,500,,some_other_event,1000000000,100.00,,\n"
            "1.001,24000,,ex_ret_brn_misp,1000000000,100.00,,\n"
            "1.001,120000,,de_no_dispatch_per_slot.no_ops_from_frontend,1000000000,100.00,,\n"
            "1.001,240000,,de_no_dispatch_per_slot.backend_stalls,1000000000,100.00,,\n"
            "1.001,180000,,cpu-cycles,1000000000,100.00,,\n"
        )
        samples = parse_perf_stat_amd_output(text)
        # Unknown event shouldn't disturb the 4-metric computation
        assert len(samples) == 4

    def test_empty(self):
        assert parse_perf_stat_amd_output("") == []

    def test_malformed_timestamp_skipped(self):
        text = (
            "not-a-time,1200000,,de_src_op_disp.all,1000000000,100.00,,\n"
        )
        samples = parse_perf_stat_amd_output(text)
        assert samples == []


# ── runner command-line ─────────────────────────────────────────────


class TestRunnerCommand:
    def test_build_command_includes_all_events(self):
        runner = PerfStatAmdRunner(PerfStatAmdOptions())
        cmd = runner.build_command()
        assert cmd[0] == "perf"
        assert cmd[1] == "stat"
        assert "-e" in cmd
        events_str = cmd[cmd.index("-e") + 1]
        for evt in _EVENTS:
            assert evt in events_str

    def test_build_command_csv(self):
        runner = PerfStatAmdRunner(PerfStatAmdOptions())
        cmd = runner.build_command()
        assert "-x," in cmd

    def test_build_command_interval(self):
        runner = PerfStatAmdRunner(PerfStatAmdOptions(interval_ms=500))
        cmd = runner.build_command()
        assert "-I500" in cmd

    def test_pid_mode(self):
        runner = PerfStatAmdRunner(PerfStatAmdOptions(pids=[12345, 67890]))
        cmd = runner.build_command()
        assert "-p" in cmd
        pid_str = cmd[cmd.index("-p") + 1]
        assert "12345" in pid_str
        assert "67890" in pid_str

    def test_system_wide_mode(self):
        runner = PerfStatAmdRunner(PerfStatAmdOptions(system_wide=True))
        cmd = runner.build_command()
        assert "-a" in cmd


# ── run() subprocess wrapping ───────────────────────────────────────


class TestRunSubprocess:
    def _stderr_sample(self):
        # Two intervals worth of canonical events
        return (
            "1.001,1200000,,de_src_op_disp.all,1000000000,100.00,,\n"
            "1.001,720000,,ex_ret_ops,1000000000,100.00,,\n"
            "1.001,24000,,ex_ret_brn_misp,1000000000,100.00,,\n"
            "1.001,120000,,de_no_dispatch_per_slot.no_ops_from_frontend,1000000000,100.00,,\n"
            "1.001,240000,,de_no_dispatch_per_slot.backend_stalls,1000000000,100.00,,\n"
            "1.001,180000,,cpu-cycles,1000000000,100.00,,\n"
        )

    def test_run_popen_file_not_found(self):
        runner = PerfStatAmdRunner(PerfStatAmdOptions())
        with patch(
            "topdown.collector.perf_stat_amd.subprocess.Popen",
            side_effect=FileNotFoundError("no perf"),
        ):
            with pytest.raises(RuntimeError, match="perf not found"):
                runner.run(5)

    def test_run_system_wide_uses_sleep_not(self):
        """System-wide mode: no `-- sleep N` workload; send SIGINT after duration."""
        runner = PerfStatAmdRunner(PerfStatAmdOptions(system_wide=True))
        fake_proc = MagicMock()
        fake_proc.communicate.return_value = ("", self._stderr_sample())
        with patch("topdown.collector.perf_stat_amd.subprocess.Popen", return_value=fake_proc), \
             patch("topdown.collector.perf_stat_amd.time.sleep") as sleep_mock:
            runner.run(3)
            sleep_mock.assert_called_with(3)
            fake_proc.send_signal.assert_called_once()

    def test_run_workload_mode_appends_sleep(self):
        runner = PerfStatAmdRunner(PerfStatAmdOptions())  # no pids, not system-wide
        fake_proc = MagicMock()
        fake_proc.communicate.return_value = ("", self._stderr_sample())
        with patch("topdown.collector.perf_stat_amd.subprocess.Popen", return_value=fake_proc) as popen:
            runner.run(7)
            called_cmd = popen.call_args[0][0]
            assert called_cmd[-3:] == ["--", "sleep", "7"]

    def test_run_and_parse_happy_path(self):
        runner = PerfStatAmdRunner(PerfStatAmdOptions(system_wide=True))
        fake_proc = MagicMock()
        fake_proc.communicate.return_value = ("", self._stderr_sample())
        with patch("topdown.collector.perf_stat_amd.subprocess.Popen", return_value=fake_proc), \
             patch("topdown.collector.perf_stat_amd.time.sleep"):
            samples = runner.run_and_parse(3)
        assert len(samples) == 4  # 4 L1 metrics
        names = {s.metric_name for s in samples}
        assert names == {"Retiring", "Frontend_Bound", "Backend_Bound", "Bad_Speculation"}

    def test_run_and_parse_falls_back_to_stdout(self):
        """Some kernels emit CSV on stdout instead of stderr."""
        runner = PerfStatAmdRunner(PerfStatAmdOptions(system_wide=True))
        fake_proc = MagicMock()
        fake_proc.communicate.return_value = (self._stderr_sample(), "")  # stdout has it
        with patch("topdown.collector.perf_stat_amd.subprocess.Popen", return_value=fake_proc), \
             patch("topdown.collector.perf_stat_amd.time.sleep"):
            samples = runner.run_and_parse(3)
        assert len(samples) == 4


# ── availability check ─────────────────────────────────────────────


class TestCheckSupported:
    from topdown.collector.perf_stat_amd import check_perf_stat_amd_supported

    def test_events_present(self):
        from topdown.collector.perf_stat_amd import check_perf_stat_amd_supported
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = (
            "  de_src_op_disp.all                           [Kernel PMU event]\n"
            "  ex_ret_ops                                   [Kernel PMU event]\n"
        )
        fake_result.stderr = ""
        with patch("topdown.collector.perf_stat_amd.subprocess.run", return_value=fake_result):
            ok, msg = check_perf_stat_amd_supported()
            assert ok
            assert "available" in msg.lower()

    def test_events_missing(self):
        from topdown.collector.perf_stat_amd import check_perf_stat_amd_supported
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "cpu-cycles\ninstructions\n"
        fake_result.stderr = ""
        with patch("topdown.collector.perf_stat_amd.subprocess.run", return_value=fake_result):
            ok, msg = check_perf_stat_amd_supported()
            assert not ok
            assert "not found" in msg

    def test_perf_not_installed(self):
        from topdown.collector.perf_stat_amd import check_perf_stat_amd_supported
        with patch(
            "topdown.collector.perf_stat_amd.subprocess.run",
            side_effect=FileNotFoundError("no perf"),
        ):
            ok, msg = check_perf_stat_amd_supported()
            assert not ok
            assert "perf not found" in msg


# ── L1 computation extra edge cases ────────────────────────────────


class TestComputeL1Edge:
    def test_handles_retired_exceeding_slots_clamp(self):
        # Sometimes ex_ret_ops slightly exceeds dispatched due to multi-uop retirement
        result = _compute_l1({
            "dispatched": 1000.0, "retired": 1020.0, "br_misp": 5.0,
            "fe_no_disp": 50.0, "be_stall": 80.0, "cycles": 1000.0,
        })
        assert result is not None
        # Sum is normalized to 100 exactly
        assert sum(result.values()) == pytest.approx(100.0)
        # Bad_Speculation floor at 0 when the residual is negative
        assert result["Bad_Speculation"] >= 0.0

    def test_only_denominator_present(self):
        """Missing other events -> defaults to 0, still computes."""
        result = _compute_l1({"dispatched": 1000.0})
        assert result is not None
        # With only denominator, retiring/fe/be = 0 -> bad_spec becomes 100% residual
        assert result["Bad_Speculation"] == pytest.approx(100.0)


from topdown.collector.perf_stat_amd import _compute_l1  # noqa: E402
