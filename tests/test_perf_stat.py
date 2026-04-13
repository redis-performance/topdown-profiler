"""Tests for the perf stat --topdown collector."""

from pathlib import Path

import pytest

from topdown.collector.perf_stat import (
    PerfStatOptions,
    PerfStatRunner,
    _HEADER_NAME_MAP,
    _EVENT_NAME_MAP,
    _extract_metric_name,
    parse_perf_stat_output,
)

TEST_DATA_DIR = Path(__file__).parent / "test_data"


# ── Format A: header-based (ARM / newer perf) ─────────────────────────


class TestHeaderFormatParser:
    """Tests for the header-based output format (real ARM output)."""

    def test_parse_basic_output(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown.csv").read_text()
        samples = parse_perf_stat_output(text)
        # 4 metrics x 3 timestamps = 12 samples
        assert len(samples) == 12

    def test_metric_name_mapping(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown.csv").read_text()
        samples = parse_perf_stat_output(text)
        names = {s.metric_name for s in samples}
        assert names == {"Retiring", "Frontend_Bound", "Backend_Bound", "Bad_Speculation"}

    def test_timestamp_preserved(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown.csv").read_text()
        samples = parse_perf_stat_output(text)
        first = samples[0]
        assert first.timestamp == pytest.approx(1.000839140)

    def test_values_correct(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown.csv").read_text()
        samples = parse_perf_stat_output(text)
        # First timestamp: bad_spec=1.4, retiring=31.6, frontend=16.9, backend=50.0
        ts1 = [s for s in samples if s.timestamp == pytest.approx(1.000839140)]
        vals = {s.metric_name: s.value for s in ts1}
        assert vals["Bad_Speculation"] == pytest.approx(1.4)
        assert vals["Retiring"] == pytest.approx(31.6)
        assert vals["Frontend_Bound"] == pytest.approx(16.9)
        assert vals["Backend_Bound"] == pytest.approx(50.0)

    def test_unit_is_percent(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown.csv").read_text()
        samples = parse_perf_stat_output(text)
        assert all(s.unit == "%" for s in samples)

    def test_cpu_is_none(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown.csv").read_text()
        samples = parse_perf_stat_output(text)
        assert all(s.cpu is None for s in samples)

    def test_all_four_l1_metrics_per_timestamp(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown.csv").read_text()
        samples = parse_perf_stat_output(text)
        ts1_samples = [s for s in samples if s.timestamp == pytest.approx(1.000839140)]
        names = sorted(s.metric_name for s in ts1_samples)
        assert names == ["Backend_Bound", "Bad_Speculation", "Frontend_Bound", "Retiring"]

    def test_empty_value_row_skipped(self):
        """Rows with all empty values (warmup ticks) produce no samples."""
        text = (
            "time,percent of slots  bad_speculation,percent of slots  retiring,\n"
            "1.000,,\n"
            "2.000,5.0,30.0,\n"
        )
        samples = parse_perf_stat_output(text)
        assert len(samples) == 2  # only row 2 has values
        assert samples[0].timestamp == pytest.approx(2.000)


# ── Format B: per-line event names (some Intel perf) ──────────────────


class TestPerlineFormatParser:
    """Tests for the per-line event name format."""

    def test_parse_basic_output(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown_perline.csv").read_text()
        samples = parse_perf_stat_output(text)
        # 4 metrics x 2 timestamps = 8 samples
        assert len(samples) == 8

    def test_metric_name_mapping(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown_perline.csv").read_text()
        samples = parse_perf_stat_output(text)
        names = {s.metric_name for s in samples}
        assert names == {"Retiring", "Frontend_Bound", "Backend_Bound", "Bad_Speculation"}

    def test_timestamp_preserved(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown_perline.csv").read_text()
        samples = parse_perf_stat_output(text)
        first = samples[0]
        assert first.timestamp == pytest.approx(1.001234)

    def test_value_parsed(self):
        text = (TEST_DATA_DIR / "perf_stat_topdown_perline.csv").read_text()
        samples = parse_perf_stat_output(text)
        retiring_first = next(
            s for s in samples
            if s.metric_name == "Retiring" and s.timestamp == pytest.approx(1.001234)
        )
        assert retiring_first.value == pytest.approx(25.30)

    def test_unknown_event_skipped(self):
        text = "1.001,25.30,%,topdown-retiring,1000,100.00\n1.001,5.20,%,some-other-event,1000,100.00\n"
        samples = parse_perf_stat_output(text)
        assert len(samples) == 1
        assert samples[0].metric_name == "Retiring"

    def test_empty_value_skipped(self):
        """Lines with empty counter-value should be skipped."""
        text = "1.001,,%,topdown-retiring,1000,100.00\n2.002,25.30,%,topdown-retiring,1000,100.00\n"
        samples = parse_perf_stat_output(text)
        assert len(samples) == 1
        assert samples[0].timestamp == pytest.approx(2.002)

    def test_comment_lines_skipped(self):
        text = "# this is a comment\n# another comment\n1.001,25.30,%,topdown-retiring,1000,100.00\n"
        samples = parse_perf_stat_output(text)
        assert len(samples) == 1

    def test_l2_metric_mapping(self):
        """L2 events should be mapped to dotted hierarchy."""
        text = "1.001,12.50,%,topdown-mem-bound,1000,100.00\n"
        samples = parse_perf_stat_output(text)
        assert len(samples) == 1
        assert samples[0].metric_name == "Backend_Bound.Memory_Bound"


# ── Shared / general ──────────────────────────────────────────────────


class TestParserGeneral:
    def test_empty_input(self):
        assert parse_perf_stat_output("") == []
        assert parse_perf_stat_output("\n\n") == []

    def test_name_map_completeness(self):
        """All L1 metric names must be in both maps."""
        l1_canonical = {"Retiring", "Frontend_Bound", "Backend_Bound", "Bad_Speculation"}
        assert l1_canonical == set(
            v for v in _HEADER_NAME_MAP.values()
            if "." not in v
        )
        assert l1_canonical == set(
            v for v in _EVENT_NAME_MAP.values()
            if "." not in v
        )


class TestExtractMetricName:
    def test_header_col_with_prefix(self):
        assert _extract_metric_name("percent of slots  bad_speculation") == "Bad_Speculation"

    def test_header_col_retiring(self):
        assert _extract_metric_name("percent of slots  retiring") == "Retiring"

    def test_topdown_event_name(self):
        assert _extract_metric_name("topdown-retiring") == "Retiring"
        assert _extract_metric_name("topdown-fe-bound") == "Frontend_Bound"

    def test_time_column(self):
        assert _extract_metric_name("time") is None

    def test_empty(self):
        assert _extract_metric_name("") is None
        assert _extract_metric_name("  ") is None

    def test_unknown(self):
        assert _extract_metric_name("something_else") is None


# ── PerfStatRunner command building ───────────────────────────────────


class TestPerfStatRunner:
    def test_build_command_basic(self):
        options = PerfStatOptions()
        runner = PerfStatRunner(options)
        cmd = runner.build_command()
        assert cmd[:3] == ["perf", "stat", "--topdown"]
        assert "-I1000" in cmd
        assert "-x," in cmd

    def test_build_command_with_pids(self):
        options = PerfStatOptions(pids=[1234, 5678])
        runner = PerfStatRunner(options)
        cmd = runner.build_command()
        assert "-p" in cmd
        idx = cmd.index("-p")
        assert cmd[idx + 1] == "1234,5678"

    def test_build_command_system_wide(self):
        options = PerfStatOptions(system_wide=True)
        runner = PerfStatRunner(options)
        cmd = runner.build_command()
        assert "-a" in cmd

    def test_build_command_custom_interval(self):
        options = PerfStatOptions(interval_ms=500)
        runner = PerfStatRunner(options)
        cmd = runner.build_command()
        assert "-I500" in cmd

    def test_build_command_extra_args(self):
        options = PerfStatOptions(extra_args=["--verbose"])
        runner = PerfStatRunner(options)
        cmd = runner.build_command()
        assert "--verbose" in cmd

    def test_pids_mutually_exclusive_with_system_wide(self):
        """When pids are set, -a should not be in the command."""
        options = PerfStatOptions(pids=[1234], system_wide=True)
        runner = PerfStatRunner(options)
        cmd = runner.build_command()
        assert "-p" in cmd
        assert "-a" not in cmd
