"""Tests for the AMDuProfPcm collector (AMD Zen)."""

from pathlib import Path
from unittest.mock import patch, mock_open

import pytest

from topdown.collector.uprof_pcm import (
    UprofPcmOptions,
    UprofPcmRunner,
    _AMD_NAME_MAP,
    _canonicalize_amd_metric,
    _parse_timestamp,
    check_uprof_pcm_available,
    detect_amd_vendor,
    parse_uprof_pcm_output,
)

TEST_DATA_DIR = Path(__file__).parent / "test_data"


# ── parser: real-ish fixture ────────────────────────────────────────


class TestParseFixture:
    """Parse the sample pipeline_util CSV fixture into ToplevSamples."""

    def _load(self):
        return (TEST_DATA_DIR / "uprof_pcm_pipeline_util.csv").read_text()

    def test_produces_samples(self):
        samples = parse_uprof_pcm_output(self._load())
        # 12 canonical metrics (4 L1 + 8 L2) x 3 timestamps = 36 samples
        assert len(samples) == 36

    def test_covers_all_l1_metrics(self):
        samples = parse_uprof_pcm_output(self._load())
        names = {s.metric_name for s in samples}
        assert "Frontend_Bound" in names
        assert "Bad_Speculation" in names
        assert "Backend_Bound" in names
        # L2 names
        assert "Frontend_Bound.Fetch_Latency" in names
        assert "Frontend_Bound.Fetch_Bandwidth" in names
        assert "Bad_Speculation.Branch_Mispredicts" in names
        assert "Bad_Speculation.Machine_Clears" in names
        assert "Backend_Bound.Memory_Bound" in names
        assert "Backend_Bound.Core_Bound" in names
        assert "Retiring.Light_Operations" in names
        assert "Retiring.Heavy_Operations" in names

    def test_values_first_interval(self):
        samples = parse_uprof_pcm_output(self._load())
        ts0 = [s for s in samples if s.timestamp == pytest.approx(0.0)]
        vals = {s.metric_name: s.value for s in ts0}
        assert vals["Frontend_Bound"] == pytest.approx(15.2)
        assert vals["Frontend_Bound.Fetch_Latency"] == pytest.approx(10.3)
        assert vals["Frontend_Bound.Fetch_Bandwidth"] == pytest.approx(4.9)
        assert vals["Backend_Bound.Memory_Bound"] == pytest.approx(30.2)
        assert vals["Backend_Bound.Core_Bound"] == pytest.approx(14.4)
        assert vals["Retiring.Light_Operations"] == pytest.approx(30.4)
        assert vals["Retiring.Heavy_Operations"] == pytest.approx(4.7)

    def test_unit_is_percent(self):
        samples = parse_uprof_pcm_output(self._load())
        assert all(s.unit == "%" for s in samples)

    def test_cpu_is_none(self):
        samples = parse_uprof_pcm_output(self._load())
        assert all(s.cpu is None for s in samples)

    def test_timestamps_relative(self):
        """Second timestamp should be 1.0s after the first (09:12:08.123 -> 09:12:09.123)."""
        samples = parse_uprof_pcm_output(self._load())
        timestamps = sorted({s.timestamp for s in samples})
        assert timestamps[0] == pytest.approx(0.0)
        assert timestamps[1] == pytest.approx(1.0)
        assert timestamps[2] == pytest.approx(2.0)

    def test_skip_total_dispatch_slots(self):
        """Total_Dispatch_Slots and IPC columns should not become samples."""
        samples = parse_uprof_pcm_output(self._load())
        names = {s.metric_name for s in samples}
        # Ensure these are NOT emitted as TMA metrics
        assert "Total_Dispatch_Slots" not in names
        assert "IPC" not in names
        assert "total_dispatch_slots" not in names


# ── canonicalization: individual column headers ─────────────────────


class TestCanonicalize:
    @pytest.mark.parametrize("raw, expected", [
        ("Frontend_Bound", "Frontend_Bound"),
        ("frontend_bound", "Frontend_Bound"),
        ("Bad_Speculation", "Bad_Speculation"),
        ("Backend_Bound", "Backend_Bound"),
        ("Retiring", "Retiring"),
        # L2 direct
        ("Frontend_Bound.Latency", "Frontend_Bound.Fetch_Latency"),
        ("Frontend_Bound.BW", "Frontend_Bound.Fetch_Bandwidth"),
        ("Bad_Speculation.Mispredicts", "Bad_Speculation.Branch_Mispredicts"),
        ("Bad_Speculation.Pipeline_Restarts", "Bad_Speculation.Machine_Clears"),
        ("Backend_Bound.Memory", "Backend_Bound.Memory_Bound"),
        ("Backend_Bound.CPU", "Backend_Bound.Core_Bound"),
        ("Retiring.Fastpath", "Retiring.Light_Operations"),
        ("Retiring.Microcode", "Retiring.Heavy_Operations"),
        # quoted / whitespace
        ('"Frontend_Bound"', "Frontend_Bound"),
        ("  Frontend_Bound  ", "Frontend_Bound"),
        # prefix stripping
        ("Percent of Frontend_Bound", "Frontend_Bound"),
        ("pipeline_util.Frontend_Bound", "Frontend_Bound"),
        # space-separated variant some versions emit
        ("Backend Bound.Memory", "Backend_Bound.Memory_Bound"),
    ])
    def test_recognized(self, raw, expected):
        assert _canonicalize_amd_metric(raw) == expected

    @pytest.mark.parametrize("raw", [
        "Timestamp",
        "Total_Dispatch_Slots",
        "SMT_Disp_contention",
        "IPC",
        "",
        "unknown_metric",
    ])
    def test_skipped(self, raw):
        assert _canonicalize_amd_metric(raw) is None


# ── timestamp parsing ───────────────────────────────────────────────


class TestParseTimestamp:
    def test_hhmmss_fff(self):
        assert _parse_timestamp("09:12:08.123") == pytest.approx(9 * 3600 + 12 * 60 + 8 + 0.123)

    def test_hhmmss_no_fraction(self):
        assert _parse_timestamp("01:00:00") == pytest.approx(3600.0)

    def test_plain_float(self):
        assert _parse_timestamp("1.5") == pytest.approx(1.5)

    def test_empty(self):
        assert _parse_timestamp("") is None
        assert _parse_timestamp("   ") is None

    def test_garbage(self):
        assert _parse_timestamp("not-a-time") is None


# ── defensive: empty / malformed input ──────────────────────────────


class TestDefensive:
    def test_empty_string(self):
        assert parse_uprof_pcm_output("") == []

    def test_whitespace_only(self):
        assert parse_uprof_pcm_output("   \n  \n") == []

    def test_no_header_found(self):
        # Preamble only, never hits a "Timestamp" row
        text = "TSC_Frequency,100,Mhz\nSocket count,1\nNPS,1\n\n"
        assert parse_uprof_pcm_output(text) == []

    def test_header_but_no_recognized_columns(self):
        text = (
            "Timestamp,Foo,Bar\n"
            "09:00:00.000,10,20\n"
        )
        assert parse_uprof_pcm_output(text) == []

    def test_skips_empty_value_cells(self):
        text = (
            "Timestamp,Frontend_Bound,Retiring\n"
            "09:00:00.000,,50\n"
            "09:00:01.000,15,55\n"
        )
        samples = parse_uprof_pcm_output(text)
        # ts0: only Retiring=50; ts1: both
        metrics_at_ts0 = {s.metric_name for s in samples if s.timestamp == pytest.approx(0.0)}
        assert metrics_at_ts0 == {"Retiring"}
        metrics_at_ts1 = {s.metric_name for s in samples if s.timestamp == pytest.approx(1.0)}
        assert metrics_at_ts1 == {"Frontend_Bound", "Retiring"}

    def test_trailing_percent_sign(self):
        """Some uProf builds suffix values with % — we strip it."""
        text = (
            "Timestamp,Frontend_Bound,Retiring\n"
            "09:00:00.000,15.2%,50.1%\n"
        )
        samples = parse_uprof_pcm_output(text)
        fe = next(s for s in samples if s.metric_name == "Frontend_Bound")
        assert fe.value == pytest.approx(15.2)

    def test_quoted_header_and_cells(self):
        text = (
            '"Timestamp","Frontend_Bound","Retiring"\n'
            '"09:00:00.000","15.2","50.0"\n'
        )
        samples = parse_uprof_pcm_output(text)
        names = {s.metric_name for s in samples}
        assert names == {"Frontend_Bound", "Retiring"}


# ── passthrough of cache/memory columns (-m l1,l2,l3,memory) ─────────


class TestPassthroughUnmapped:
    def test_passthrough_off_by_default(self):
        text = (
            "Timestamp,Frontend_Bound,L3_Miss_Rate,Op_Cache_Fetch_Miss_Rate\n"
            "09:00:00.000,15.2,0.12,0.03\n"
        )
        samples = parse_uprof_pcm_output(text)
        names = {s.metric_name for s in samples}
        assert names == {"Frontend_Bound"}

    def test_passthrough_on_emits_amd_namespace(self):
        text = (
            "Timestamp,Frontend_Bound,L3_Miss_Rate,Op_Cache_Fetch_Miss_Rate\n"
            "09:00:00.000,15.2,0.12,0.03\n"
        )
        samples = parse_uprof_pcm_output(text, passthrough_unmapped=True)
        names = {s.metric_name for s in samples}
        assert "Frontend_Bound" in names
        assert any(n.startswith("AMD.") and "L3" in n for n in names)
        assert any(n.startswith("AMD.") and "Op_Cache" in n for n in names)

    def test_passthrough_preserves_tma_columns(self):
        """Canonical TMA columns should not get double-emitted under AMD.*"""
        text = (
            "Timestamp,Frontend_Bound,Backend_Bound\n"
            "09:00:00.000,15.2,44.6\n"
        )
        samples = parse_uprof_pcm_output(text, passthrough_unmapped=True)
        names = {s.metric_name for s in samples}
        assert names == {"Frontend_Bound", "Backend_Bound"}
        assert not any(n.startswith("AMD.") for n in names)

    def test_passthrough_ignores_random_text(self):
        """Arbitrary header tokens without metric hints should still be dropped."""
        text = (
            "Timestamp,Frontend_Bound,RandomNonsense,Notes\n"
            "09:00:00.000,15.2,abc,xyz\n"
        )
        samples = parse_uprof_pcm_output(text, passthrough_unmapped=True)
        names = {s.metric_name for s in samples}
        assert "Frontend_Bound" in names
        assert not any("RandomNonsense" in n for n in names)
        assert not any("Notes" in n for n in names)


# ── metric-group configurability ────────────────────────────────────


class TestMetricGroupOption:
    def test_default_is_pipeline_util(self):
        opts = UprofPcmOptions()
        assert opts.metric_group == "pipeline_util"

    def test_multi_group_string(self, tmp_path):
        fake = tmp_path / "AMDuProfPcm"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        runner = UprofPcmRunner(
            UprofPcmOptions(
                uprof_pcm_path=str(fake),
                metric_group="pipeline_util,l1,l2,l3,memory",
            )
        )
        cmd = runner.build_command("/tmp/out.csv", duration_seconds=30)
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "pipeline_util,l1,l2,l3,memory"

    def test_passthrough_option_default_off(self):
        assert UprofPcmOptions().passthrough_unmapped is False


# ── vendor detection ────────────────────────────────────────────────


class TestDetectAmdVendor:
    def test_detects_amd(self):
        cpuinfo = (
            "processor\t: 0\n"
            "vendor_id\t: AuthenticAMD\n"
            "cpu family\t: 25\n"
        )
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            assert detect_amd_vendor() is True

    def test_detects_intel_as_not_amd(self):
        cpuinfo = (
            "processor\t: 0\n"
            "vendor_id\t: GenuineIntel\n"
            "cpu family\t: 6\n"
        )
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            assert detect_amd_vendor() is False

    def test_missing_cpuinfo(self):
        with patch("builtins.open", side_effect=OSError("no /proc/cpuinfo")):
            assert detect_amd_vendor() is False


# ── binary discovery ────────────────────────────────────────────────


class TestCheckUprofPcmAvailable:
    def test_explicit_path_ok(self, tmp_path):
        fake = tmp_path / "AMDuProfPcm"
        fake.write_text("#!/bin/sh\necho fake\n")
        fake.chmod(0o755)
        ok, msg = check_uprof_pcm_available(str(fake))
        assert ok
        assert str(fake) in msg

    def test_explicit_path_missing(self, tmp_path):
        fake = tmp_path / "does_not_exist"
        ok, msg = check_uprof_pcm_available(str(fake))
        assert not ok

    def test_none_path_uses_which(self):
        with patch("topdown.collector.uprof_pcm.shutil.which", return_value="/usr/bin/AMDuProfPcm"), \
             patch("topdown.collector.uprof_pcm.Path.glob", return_value=[]):
            ok, msg = check_uprof_pcm_available(None)
            assert ok
            assert "/usr/bin/AMDuProfPcm" in msg

    def test_none_path_not_found(self):
        with patch("topdown.collector.uprof_pcm.shutil.which", return_value=None), \
             patch("topdown.collector.uprof_pcm.Path.glob", return_value=[]):
            ok, msg = check_uprof_pcm_available(None)
            assert not ok
            assert "AMDuProfPcm not found" in msg


# ── runner: command-line construction ───────────────────────────────


class TestUprofPcmRunnerCommand:
    def test_build_command_system_wide(self, tmp_path):
        fake = tmp_path / "AMDuProfPcm"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)

        runner = UprofPcmRunner(UprofPcmOptions(uprof_pcm_path=str(fake)))
        cmd = runner.build_command("/tmp/out.csv", duration_seconds=30)
        assert str(fake) == cmd[0]
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "pipeline_util"
        assert "-a" in cmd
        assert "-d" in cmd
        assert cmd[cmd.index("-d") + 1] == "30"
        assert "-o" in cmd
        assert cmd[cmd.index("-o") + 1] == "/tmp/out.csv"

    def test_build_command_custom_group(self, tmp_path):
        fake = tmp_path / "AMDuProfPcm"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)

        runner = UprofPcmRunner(
            UprofPcmOptions(uprof_pcm_path=str(fake), metric_group="pipeline_util,l1,l2")
        )
        cmd = runner.build_command("/tmp/out.csv", duration_seconds=5)
        assert cmd[cmd.index("-m") + 1] == "pipeline_util,l1,l2"

    def test_build_command_missing_binary_raises(self):
        with patch("topdown.collector.uprof_pcm.shutil.which", return_value=None), \
             patch("topdown.collector.uprof_pcm.Path.glob", return_value=[]):
            runner = UprofPcmRunner(UprofPcmOptions())
            with pytest.raises(RuntimeError, match="AMDuProfPcm not found"):
                runner.build_command("/tmp/x.csv", duration_seconds=10)
