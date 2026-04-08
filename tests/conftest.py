"""Shared test fixtures."""

import pytest
from pathlib import Path

from topdown.storage.sqlite_backend import SQLiteBackend
from topdown.storage.models import Run

TEST_DATA_DIR = Path(__file__).parent / "test_data"


@pytest.fixture
def sqlite_backend(tmp_path):
    """SQLite backend using temp directory."""
    db = SQLiteBackend(db_path=tmp_path / "test.db")
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def sample_run():
    return Run(
        run_id="test-run-001",
        process_name="redis-server",
        level=2,
        labels={
            "git_branch": "unstable",
            "test_name": "set-get-100",
            "topology": "oss-standalone",
            "arch": "x86_64",
            "kernel_version": "6.17.0-19-generic",
        },
    )


@pytest.fixture
def sample_run_b():
    return Run(
        run_id="test-run-002",
        process_name="redis-server",
        level=2,
        labels={
            "git_branch": "7.2",
            "test_name": "hset-hget",
            "topology": "oss-cluster",
            "arch": "x86_64",
            "kernel_version": "6.17.0-19-generic",
        },
    )


@pytest.fixture
def sample_metrics():
    """Simulated aggregated L2 toplev output."""
    return [
        {"metric_name": "Frontend_Bound", "value": 15.0, "unit": "%"},
        {"metric_name": "Backend_Bound", "value": 45.0, "unit": "%"},
        {"metric_name": "Backend_Bound.Memory_Bound", "value": 30.0, "unit": "%"},
        {"metric_name": "Backend_Bound.Memory_Bound.L3_Bound", "value": 23.0, "unit": "%"},
        {"metric_name": "Backend_Bound.Memory_Bound.DRAM_Bound", "value": 5.0, "unit": "%"},
        {"metric_name": "Backend_Bound.Core_Bound", "value": 15.0, "unit": "%"},
        {"metric_name": "Bad_Speculation", "value": 10.0, "unit": "%"},
        {"metric_name": "Retiring", "value": 30.0, "unit": "%"},
    ]
