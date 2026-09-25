"""Tests for MCP server tools (unit tests calling handler functions directly)."""

import pytest
from unittest.mock import patch

from topdown.storage.sqlite_backend import SQLiteBackend
from topdown.storage.models import Run, Sample
from topdown.config import TopdownConfig

from tests.conftest import INTEL_RUN_A, INTEL_RUN_B, load_intel_runs


@pytest.fixture
def populated_db(tmp_path):
    """Create a populated SQLite database for MCP tests."""
    db_path = tmp_path / "mcp_test.db"
    backend = SQLiteBackend(db_path=db_path)
    backend.initialize()

    # Run 1: redis-server with high DRAM_Bound
    run1 = Run(
        run_id="mcp-run-001",
        process_name="redis-server",
        level=3,
        labels={
            "git_branch": "unstable",
            "test_name": "set-get-100",
            "topology": "oss-standalone",
            "build_variant": "release",
        },
    )
    backend.insert_run(run1)
    samples1 = [
        Sample(run_id="mcp-run-001", timestamp=1.0, metric_name="Frontend_Bound", value=12.0, unit="%"),
        Sample(run_id="mcp-run-001", timestamp=1.0, metric_name="Backend_Bound", value=48.0, unit="%"),
        Sample(run_id="mcp-run-001", timestamp=1.0, metric_name="Backend_Bound.Memory_Bound", value=32.0, unit="%"),
        Sample(run_id="mcp-run-001", timestamp=1.0, metric_name="Backend_Bound.Memory_Bound.DRAM_Bound", value=22.0, unit="%"),
        Sample(run_id="mcp-run-001", timestamp=1.0, metric_name="Backend_Bound.Core_Bound", value=16.0, unit="%"),
        Sample(run_id="mcp-run-001", timestamp=1.0, metric_name="Bad_Speculation", value=10.0, unit="%"),
        Sample(run_id="mcp-run-001", timestamp=1.0, metric_name="Retiring", value=30.0, unit="%"),
    ]
    backend.insert_samples(samples1)

    # Run 2: redis-server debug build
    run2 = Run(
        run_id="mcp-run-002",
        process_name="redis-server",
        level=3,
        labels={
            "git_branch": "unstable",
            "test_name": "set-get-100",
            "topology": "oss-standalone",
            "build_variant": "debug",
        },
    )
    backend.insert_run(run2)
    samples2 = [
        Sample(run_id="mcp-run-002", timestamp=1.0, metric_name="Frontend_Bound", value=18.0, unit="%"),
        Sample(run_id="mcp-run-002", timestamp=1.0, metric_name="Backend_Bound", value=42.0, unit="%"),
        Sample(run_id="mcp-run-002", timestamp=1.0, metric_name="Backend_Bound.Memory_Bound", value=28.0, unit="%"),
        Sample(run_id="mcp-run-002", timestamp=1.0, metric_name="Backend_Bound.Memory_Bound.DRAM_Bound", value=15.0, unit="%"),
        Sample(run_id="mcp-run-002", timestamp=1.0, metric_name="Backend_Bound.Core_Bound", value=14.0, unit="%"),
        Sample(run_id="mcp-run-002", timestamp=1.0, metric_name="Bad_Speculation", value=12.0, unit="%"),
        Sample(run_id="mcp-run-002", timestamp=1.0, metric_name="Retiring", value=28.0, unit="%"),
    ]
    backend.insert_samples(samples2)

    backend.close()
    return db_path


@pytest.fixture
def mock_config(populated_db):
    """Mock get_config to use our test database."""
    config = TopdownConfig(db_path=populated_db)

    def _mock_get_config(*args, **kwargs):
        return config

    return config, _mock_get_config


class TestQueryBottlenecksTool:
    def test_query_bottlenecks(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import query_bottlenecks
            result = query_bottlenecks(process_name="redis-server")
            assert "Backend_Bound" in result
            assert "mcp-run-002" in result or "mcp-run-001" in result  # most recent

    def test_query_bottlenecks_with_labels(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import query_bottlenecks
            result = query_bottlenecks(labels={"build_variant": "release"})
            assert "mcp-run-001" in result


class TestQueryByBottleneckTool:
    def test_finds_dram_bound_runs(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import query_by_bottleneck
            result = query_by_bottleneck(metric_name="DRAM_Bound", min_pct=10.0)
            assert "mcp-run-001" in result  # 22%
            assert "mcp-run-002" in result  # 15%

    def test_threshold_filters(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import query_by_bottleneck
            result = query_by_bottleneck(metric_name="DRAM_Bound", min_pct=20.0)
            assert "mcp-run-001" in result  # 22% passes
            assert "mcp-run-002" not in result  # 15% doesn't pass


class TestGetFunnelTool:
    def test_funnel_for_run(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import get_funnel
            result = get_funnel(run_id="mcp-run-001")
            assert "Pipeline Slots Funnel" in result
            assert "Useful work" in result


class TestCompareRunsTool:
    def test_compare_two_runs(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import compare_runs
            result = compare_runs(run_id_a="mcp-run-001", run_id_b="mcp-run-002")
            assert "Comparing" in result or "↑" in result or "↓" in result or "Improvement" in result or "Regression" in result

    def test_compare_nonexistent(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import compare_runs
            result = compare_runs(run_id_a="nonexistent", run_id_b="mcp-run-001")
            assert "not found" in result


class TestCompareByLabelsTool:
    def test_compare_release_vs_debug(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import compare_by_labels
            result = compare_by_labels(
                label_a={"build_variant": "release"},
                label_b={"build_variant": "debug"},
                process_name="redis-server",
            )
            assert "release" in result or "debug" in result or "Comparing" in result


class TestExplainMetricTool:
    def test_explain_known_metric(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import explain_metric
            result = explain_metric(metric_name="Backend_Bound.Memory_Bound.DRAM_Bound")
            assert "DRAM" in result
            assert "Tuning Hints" in result or "tuning" in result.lower()

    def test_explain_unknown_metric(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import explain_metric
            result = explain_metric(metric_name="Totally_Fake")
            assert "Unknown" in result


class TestListRunsTool:
    def test_list_all(self, mock_config):
        config, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import list_profiling_runs
            result = list_profiling_runs()
            assert "mcp-run" in result
            assert "redis-server" in result


class TestIntelRunThroughTools:
    """An Intel-style run (toplev units) through every read tool.

    Tools print run IDs that other tools must accept, so they print them whole.
    """

    RUN_A = INTEL_RUN_A
    RUN_B = INTEL_RUN_B

    @pytest.fixture
    def intel_db(self, mock_config, populated_db):
        backend = SQLiteBackend(db_path=populated_db)
        load_intel_runs(backend)
        backend.close()
        _, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            yield

    def test_listed_id_feeds_get_funnel(self, intel_db):
        from topdown.mcp_server import get_funnel, list_profiling_runs
        assert self.RUN_A in list_profiling_runs(labels={"test_name": "intel-run"})
        funnel = get_funnel(run_id=self.RUN_A)
        assert self.RUN_A in funnel
        assert "Data quality: MUX 3.0%" in funnel

    def test_query_bottlenecks_ranks_slots_and_lists_details(self, intel_db):
        from topdown.mcp_server import query_bottlenecks
        lines = query_bottlenecks(labels={"variant": self.RUN_A[:4]}).splitlines()
        assert lines[0].startswith(f"Run: {self.RUN_A} |")
        start = lines.index("Top bottlenecks:") + 1
        assert lines[start:lines.index("", start)] == [
            "  Backend_Bound: 40.0%",
            "  Backend_Bound -> Core_Bound: 30.0%",
            "  Backend_Bound -> Memory_Bound: 10.0%",
        ]
        assert (
            "  Backend_Bound -> Core_Bound -> Serializing_Operation: 61.0% of Clocks"
            "  (parent Core_Bound: 30.0% of slots)"
        ) in lines
        assert lines[-1].startswith("Data quality: MUX 3.0%")

    def test_query_by_bottleneck_names_node_and_unit(self, intel_db):
        from topdown.mcp_server import query_by_bottleneck
        lines = query_by_bottleneck(metric_name="L1_Bound", min_pct=5.0).splitlines()
        assert lines[0] == "Runs where L1_Bound >= 5.0% (in the node's own unit):"
        assert any(ln.startswith(f"  {self.RUN_A} | Backend_Bound.Memory_Bound.L1_Bound 12.0% of Stalls |")
                   for ln in lines)

    def test_query_by_bottleneck_says_when_it_falls_back(self, intel_db):
        from topdown.mcp_server import query_by_bottleneck
        text = query_by_bottleneck(metric_name="Serializing", min_pct=5.0)
        assert text.startswith(
            "No node is named exactly 'Serializing'; nodes containing it that reach 5.0% (in their own unit):"
        )

    def test_existing_node_below_threshold_does_not_fall_back(self, intel_db):
        from topdown.mcp_server import query_by_bottleneck
        assert query_by_bottleneck(metric_name="Core_Bound", min_pct=35.0) == (
            "No runs found where Core_Bound >= 35.0%."
        )
        assert query_by_bottleneck(metric_name="Nonexistent") == "No node named or containing 'Nonexistent' reaches 5.0%."
        assert query_by_bottleneck(metric_name="MUX").startswith("MUX is a data-quality metric, not a TMA node")

    def test_query_by_bottleneck_requires_a_name(self, intel_db):
        from topdown.mcp_server import query_by_bottleneck
        assert query_by_bottleneck(metric_name="").startswith("metric_name is required")

    def test_compare_tools_print_full_ids(self, intel_db):
        from topdown.mcp_server import compare_funnel, compare_runs
        funnel = compare_funnel(run_id_a=self.RUN_A, run_id_b=self.RUN_B, level=2)
        assert funnel.splitlines()[0].startswith(f"A: {self.RUN_A} |")
        assert funnel.splitlines()[1].startswith(f"B: {self.RUN_B} |")
        runs = compare_runs(run_id_a=self.RUN_A, run_id_b=self.RUN_B)
        assert runs.splitlines()[0] == f"Comparing {self.RUN_A} (baseline) vs {self.RUN_B}"
        assert "  ↓ Backend_Bound.Core_Bound: 30.0% -> 20.0% (-10.0 pp)" in runs.splitlines()
        assert runs.splitlines()[-1].startswith("Data quality: MUX 3.0%")

    def test_compare_without_differences_still_reports_mux(self, intel_db):
        from topdown.mcp_server import compare_runs
        text = compare_runs(run_id_a=self.RUN_A, run_id_b=self.RUN_A)
        assert text.splitlines()[0] == "No significant differences found."
        assert text.splitlines()[-1].startswith("Data quality: MUX 3.0%")

    def test_query_bottlenecks_with_only_detail_nodes(self, mock_config, populated_db):
        backend = SQLiteBackend(db_path=populated_db)
        backend.insert_run(Run(run_id="detail-only", process_name="redis-server", level=3,
                               labels={"test_name": "detail-only"}))
        backend.insert_samples([
            Sample(run_id="detail-only", timestamp=1.0, metric_name="Retiring", value=90.0, unit="%"),
            Sample(run_id="detail-only", timestamp=1.0, metric_name="Retiring.Uop_Mix", value=40.0, unit="% Uops"),
        ])
        backend.close()
        _, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import query_bottlenecks
            text = query_bottlenecks(labels={"test_name": "detail-only"})
        assert "Top bottlenecks" not in text
        assert "Retiring -> Uop_Mix: 40.0% of Uops  (parent Retiring: 90.0% of slots)" in text

    def test_compare_by_labels_reports_mux(self, intel_db):
        from topdown.mcp_server import compare_by_labels
        text = compare_by_labels(label_a={"variant": self.RUN_A[:4]}, label_b={"variant": self.RUN_B[:4]})
        assert text.splitlines()[-1].startswith("Data quality: MUX 3.0%")

    def test_query_by_bottleneck_says_when_results_are_capped(self, mock_config, populated_db):
        from topdown.storage.base import DEFAULT_NODE_LIMIT

        backend = SQLiteBackend(db_path=populated_db)
        for _ in range(DEFAULT_NODE_LIMIT + 1):
            run = Run(process_name="redis-server", level=1, labels={})
            backend.insert_run(run)
            backend.insert_samples([Sample(run_id=run.run_id, timestamp=1.0, metric_name="Frontend_Bound",
                                           value=90.0, unit="%")])
        backend.close()
        _, mock_get = mock_config
        with patch("topdown.mcp_server.get_config", mock_get):
            from topdown.mcp_server import query_by_bottleneck
            text = query_by_bottleneck(metric_name="Frontend_Bound", min_pct=80.0)
        assert text.splitlines()[-1] == f"Showing the first {DEFAULT_NODE_LIMIT}; narrow the time window or labels for more."
        assert len([ln for ln in text.splitlines() if "Frontend_Bound" in ln and " | " in ln]) == DEFAULT_NODE_LIMIT

    def test_explain_mux(self, intel_db):
        from topdown.mcp_server import explain_metric
        assert "event-multiplexing" in explain_metric(metric_name="MUX")

    def test_run_tree_reports_mux(self, intel_db):
        from topdown.mcp_server import run_tree
        text = run_tree(run_id=self.RUN_A)
        assert "Serializing_Operation" in text
        assert text.splitlines()[-1].startswith("Data quality: MUX 3.0%")
