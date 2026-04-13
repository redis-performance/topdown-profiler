"""Tests for the make_runner factory and resolve_collector."""

from unittest.mock import patch

from topdown.collector import make_runner, resolve_collector
from topdown.collector.toplev import ToplevRunner
from topdown.collector.perf_stat import PerfStatRunner
from topdown.config import TopdownConfig


class TestResolveCollector:
    """Tests for resolve_collector auto-detection."""

    @patch("platform.machine", return_value="x86_64")
    def test_x86_64_defaults_to_toplev(self, _mock):
        config = TopdownConfig()
        assert resolve_collector(config) == "toplev"

    @patch("platform.machine", return_value="aarch64")
    def test_aarch64_defaults_to_perf_stat(self, _mock):
        config = TopdownConfig()
        assert resolve_collector(config) == "perf_stat"

    def test_explicit_override_perf_stat(self):
        config = TopdownConfig(collector="perf_stat")
        assert resolve_collector(config) == "perf_stat"

    def test_explicit_override_toplev(self):
        config = TopdownConfig(collector="toplev")
        assert resolve_collector(config) == "toplev"

    @patch("platform.machine", return_value="i686")
    def test_i686_defaults_to_toplev(self, _mock):
        config = TopdownConfig()
        assert resolve_collector(config) == "toplev"


class TestMakeRunner:
    """Tests for make_runner factory."""

    @patch("platform.machine", return_value="x86_64")
    def test_x86_64_returns_toplev_runner(self, _mock):
        config = TopdownConfig()
        runner = make_runner(config, pids=[1234], system_wide=False, level=2)
        assert isinstance(runner, ToplevRunner)

    @patch("platform.machine", return_value="aarch64")
    def test_aarch64_returns_perf_stat_runner(self, _mock):
        config = TopdownConfig()
        runner = make_runner(config, pids=[1234], system_wide=False, level=2)
        assert isinstance(runner, PerfStatRunner)

    def test_explicit_perf_stat_on_x86(self):
        config = TopdownConfig(collector="perf_stat")
        runner = make_runner(config, pids=[1234], system_wide=False, level=2)
        assert isinstance(runner, PerfStatRunner)

    def test_explicit_toplev_on_arm(self):
        config = TopdownConfig(collector="toplev")
        runner = make_runner(config, pids=[1234], system_wide=False, level=2)
        assert isinstance(runner, ToplevRunner)

    @patch("platform.machine", return_value="aarch64")
    def test_level_accepted_for_perf_stat(self, _mock):
        """Level > 1 should not error on ARM, just log info."""
        config = TopdownConfig()
        runner = make_runner(config, pids=[1234], system_wide=False, level=3)
        assert isinstance(runner, PerfStatRunner)

    @patch("platform.machine", return_value="x86_64")
    def test_toplev_runner_gets_level(self, _mock):
        config = TopdownConfig()
        runner = make_runner(config, pids=[1234], system_wide=False, level=4)
        assert isinstance(runner, ToplevRunner)
        assert runner.options.level == 4

    @patch("platform.machine", return_value="x86_64")
    def test_toplev_runner_gets_pids(self, _mock):
        config = TopdownConfig()
        runner = make_runner(config, pids=[111, 222], system_wide=False, level=2)
        assert isinstance(runner, ToplevRunner)
        assert runner.options.pids == [111, 222]

    @patch("platform.machine", return_value="aarch64")
    def test_perf_stat_runner_gets_pids(self, _mock):
        config = TopdownConfig()
        runner = make_runner(config, pids=[333], system_wide=False, level=1)
        assert isinstance(runner, PerfStatRunner)
        assert runner.options.pids == [333]
