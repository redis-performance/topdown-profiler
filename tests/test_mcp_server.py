"""Tests for MCP server tools (unit tests calling handler functions directly)."""

import os
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from topdown.storage.sqlite_backend import SQLiteBackend
from topdown.storage.models import Run, Sample
from topdown.config import TopdownConfig


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
