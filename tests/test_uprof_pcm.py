"""Tests for the AMDuProfPcm collector (AMD Zen)."""

from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

from topdown.collector.uprof_pcm import (
    UprofPcmOptions,
    UprofPcmRunner,
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


# ── v5 format: real AMDuProfPcm 5.2.606 output from EPYC Zen 5 ──────


class TestRealV5Format:
    """Parse actual AMDuProfPcm 5.2.606 CSV from EPYC 9R45 (Zen 5)."""

    def _load(self):
        return (TEST_DATA_DIR / "uprof_pcm_5.2_real.csv").read_text()

    def test_parser_accepts_v5_format(self):
        """No 'Timestamp' column; parser must auto-detect by matching
        canonical TMA column names in the header."""
        samples = parse_uprof_pcm_output(self._load())
        assert len(samples) > 0, "v5 format should produce samples"

    def test_all_l1_metrics_extracted(self):
        samples = parse_uprof_pcm_output(self._load())
        names = {s.metric_name for s in samples}
        assert "Frontend_Bound" in names
        assert "Backend_Bound" in names
        assert "Bad_Speculation" in names
        assert "Retiring" in names

    def test_all_l2_metrics_extracted(self):
        samples = parse_uprof_pcm_output(self._load())
        names = {s.metric_name for s in samples}
        # Zen 5 pipeline_util exposes all 8 L2 sub-categories
        assert "Frontend_Bound.Fetch_Latency" in names
        assert "Frontend_Bound.Fetch_Bandwidth" in names
        assert "Bad_Speculation.Branch_Mispredicts" in names
        assert "Bad_Speculation.Machine_Clears" in names
        assert "Backend_Bound.Memory_Bound" in names
        assert "Backend_Bound.Core_Bound" in names
        assert "Retiring.Light_Operations" in names
        assert "Retiring.Heavy_Operations" in names

    def test_total_dispatch_slots_not_emitted(self):
        """Data row starts with Total_Dispatch_Slots value but that
        column must be skipped (metadata, not a TMA %)."""
        samples = parse_uprof_pcm_output(self._load())
        names = {s.metric_name for s in samples}
        assert "Total_Dispatch_Slots" not in names
        assert "SMT_Disp_contention" not in names

    def test_synthetic_timestamps(self):
        """v5 format has no Timestamp column. Parser assigns 0.0s to first
        row and increments by the sample interval read from preamble."""
        samples = parse_uprof_pcm_output(self._load())
        timestamps = sorted({s.timestamp for s in samples})
        assert timestamps[0] == pytest.approx(0.0)
        # Second interval at sample_interval_s (1.0 in our fixture's preamble)
        if len(timestamps) > 1:
            assert timestamps[1] == pytest.approx(1.0)

    def test_values_are_reasonable_percentages(self):
        """All TMA samples should be 0-100%."""
        samples = parse_uprof_pcm_output(self._load())
        for s in samples:
            assert 0.0 <= s.value <= 100.0, f"{s.metric_name}={s.value}"

    def test_values_sum_roughly_to_100(self):
        """L1 categories should sum to ~100% per timestamp (per AMD spec)."""
        samples = parse_uprof_pcm_output(self._load())
        # Group by timestamp
        by_ts: dict[float, dict[str, float]] = {}
        for s in samples:
            by_ts.setdefault(s.timestamp, {})[s.metric_name] = s.value
        for ts, metrics in by_ts.items():
            l1_sum = (
                metrics.get("Frontend_Bound", 0)
                + metrics.get("Backend_Bound", 0)
                + metrics.get("Bad_Speculation", 0)
                + metrics.get("Retiring", 0)
            )
            # AMD pipeline_util may not sum to exactly 100 (see uProf docs —
            # sub-slot accounting has a small rounding drift). Allow ±5%.
            assert 95.0 <= l1_sum <= 105.0, f"ts={ts} L1 sum={l1_sum}"

    def test_ipc_column_not_present_in_pipeline_util(self):
        """v5.2 pipeline_util does not include IPC (unlike some older versions).
        Our parser must still not emit IPC even if it appears in another group."""
        samples = parse_uprof_pcm_output(self._load())
        names = {s.metric_name for s in samples}
        assert "IPC" not in names


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

    def test_build_command_configured_path_missing(self):
        runner = UprofPcmRunner(UprofPcmOptions(uprof_pcm_path="/nonexistent/AMDuProfPcm"))
        with pytest.raises(RuntimeError, match="AMDuProfPcm not found at configured path"):
            runner.build_command("/tmp/x.csv", duration_seconds=10)

    def test_build_command_discovers_in_opt(self, tmp_path):
        """Binary discovery via /opt/AMDuProf_*/bin/ glob fallback."""
        fake_dir = tmp_path / "AMDuProf_Linux_x64_5.0.1630" / "bin"
        fake_dir.mkdir(parents=True)
        fake_bin = fake_dir / "AMDuProfPcm"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        with patch("topdown.collector.uprof_pcm.shutil.which", return_value=None), \
             patch("topdown.collector.uprof_pcm.Path") as mock_Path:
            mock_Path.return_value.glob.return_value = [fake_dir.parent]
            mock_Path.side_effect = lambda p=None: Path(p) if p is not None else None
            # Path("/opt").glob(...) → returns our tmp AMDuProf_* dir
            mock_root = MagicMock()
            mock_root.glob.return_value = [fake_dir.parent]
            mock_Path.side_effect = lambda x: mock_root if x == "/opt" else Path(x)
            runner = UprofPcmRunner(UprofPcmOptions())
            cmd = runner.build_command("/tmp/x.csv", duration_seconds=10)
            assert str(fake_bin) == cmd[0]


# ── run() subprocess wrapping ───────────────────────────────────────


class TestRunSubprocess:
    def _make_runner(self, tmp_path, **kwargs):
        fake = tmp_path / "AMDuProfPcm"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        kwargs.setdefault("uprof_pcm_path", str(fake))
        return UprofPcmRunner(UprofPcmOptions(**kwargs))

    def test_run_logs_pid_mode_and_falls_back_system_wide(self, tmp_path, caplog):
        """PIDs requested → logs warning + continues system-wide."""
        runner = self._make_runner(tmp_path, pids=[1234, 5678])
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ("", "")
        with patch("topdown.collector.uprof_pcm.subprocess.Popen", return_value=fake_proc), \
             patch("topdown.collector.uprof_pcm.tempfile.NamedTemporaryFile") as nt:
            nt.return_value.name = str(tmp_path / "out.csv")
            import logging
            with caplog.at_level(logging.WARNING, logger="topdown.collector.uprof_pcm"):
                runner.run(5)
            assert any("does not support per-PID" in r.message for r in caplog.records)

    def test_run_nonzero_returncode_raises_with_permission_hint(self, tmp_path):
        runner = self._make_runner(tmp_path)
        fake_proc = MagicMock()
        fake_proc.returncode = 126
        fake_proc.communicate.return_value = ("", "permission denied opening PMU")
        with patch("topdown.collector.uprof_pcm.subprocess.Popen", return_value=fake_proc), \
             patch("topdown.collector.uprof_pcm.tempfile.NamedTemporaryFile") as nt:
            nt.return_value.name = str(tmp_path / "out.csv")
            with pytest.raises(RuntimeError) as exc:
                runner.run(5)
            assert "root" in str(exc.value).lower() or "permission" in str(exc.value).lower()

    def test_run_popen_file_not_found_raises_install_hint(self, tmp_path):
        runner = self._make_runner(tmp_path)
        with patch(
            "topdown.collector.uprof_pcm.subprocess.Popen",
            side_effect=FileNotFoundError("no such file"),
        ), patch("topdown.collector.uprof_pcm.tempfile.NamedTemporaryFile") as nt:
            nt.return_value.name = str(tmp_path / "out.csv")
            with pytest.raises(RuntimeError, match="failed to launch"):
                runner.run(5)

    def test_run_and_parse_cleans_up_tempfile(self, tmp_path):
        """Happy path: run() produces a CSV, parser returns samples, file is deleted."""
        runner = self._make_runner(tmp_path)
        csv_path = tmp_path / "uprof_run.csv"
        csv_path.write_text(
            "Timestamp,Frontend_Bound,Retiring\n"
            "09:00:00.000,15.2,50.0\n"
            "09:00:01.000,14.8,52.0\n"
        )
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ("", "")
        nt_instance = MagicMock()
        nt_instance.name = str(csv_path)
        with patch("topdown.collector.uprof_pcm.subprocess.Popen", return_value=fake_proc), \
             patch("topdown.collector.uprof_pcm.tempfile.NamedTemporaryFile", return_value=nt_instance):
            samples = runner.run_and_parse(5)
        assert len(samples) == 4  # 2 metrics x 2 rows
        assert not csv_path.exists(), "tempfile should be cleaned up"
