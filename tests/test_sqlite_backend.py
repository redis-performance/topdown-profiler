"""Tests for SQLite storage backend."""

from datetime import datetime, timezone

from topdown.storage.models import Run, Sample


class TestSQLiteInsertAndRetrieve:
    def test_insert_and_get_run(self, sqlite_backend, sample_run):
        sqlite_backend.insert_run(sample_run)
        retrieved = sqlite_backend.get_run(sample_run.run_id)
        assert retrieved is not None
        assert retrieved.run_id == sample_run.run_id
        assert retrieved.process_name == "redis-server"
        assert retrieved.level == 2
        assert retrieved.labels["git_branch"] == "unstable"

    def test_insert_samples(self, sqlite_backend, sample_run):
        sqlite_backend.insert_run(sample_run)
        samples = [
            Sample(run_id=sample_run.run_id, timestamp=1.0, metric_name="Frontend_Bound", value=15.0, unit="%"),
            Sample(run_id=sample_run.run_id, timestamp=1.0, metric_name="Backend_Bound", value=45.0, unit="%"),
            Sample(run_id=sample_run.run_id, timestamp=1.0, metric_name="Retiring", value=30.0, unit="%"),
        ]
        count = sqlite_backend.insert_samples(samples)
        assert count == 3

        retrieved = sqlite_backend.get_samples(sample_run.run_id)
        assert len(retrieved) == 3

    def test_insert_empty_samples(self, sqlite_backend):
        assert sqlite_backend.insert_samples([]) == 0

    def test_get_nonexistent_run(self, sqlite_backend):
        assert sqlite_backend.get_run("nonexistent") is None


class TestSQLiteListRuns:
    def test_list_all(self, sqlite_backend, sample_run, sample_run_b):
        sqlite_backend.insert_run(sample_run)
        sqlite_backend.insert_run(sample_run_b)
        runs = sqlite_backend.list_runs()
        assert len(runs) == 2

    def test_filter_by_process(self, sqlite_backend, sample_run):
        sqlite_backend.insert_run(sample_run)
        other = Run(run_id="other-run", process_name="memtier_benchmark", level=1)
        sqlite_backend.insert_run(other)

        runs = sqlite_backend.list_runs(process_name="redis-server")
        assert len(runs) == 1
        assert runs[0].process_name == "redis-server"

    def test_filter_by_labels(self, sqlite_backend, sample_run, sample_run_b):
        sqlite_backend.insert_run(sample_run)
        sqlite_backend.insert_run(sample_run_b)

        # Filter by git_branch
        runs = sqlite_backend.list_runs(labels={"git_branch": "unstable"})
        assert len(runs) == 1
        assert runs[0].run_id == sample_run.run_id

        # Filter by multiple labels
        runs = sqlite_backend.list_runs(labels={"git_branch": "7.2", "topology": "oss-cluster"})
        assert len(runs) == 1
        assert runs[0].run_id == sample_run_b.run_id

    def test_filter_by_labels_no_match(self, sqlite_backend, sample_run):
        sqlite_backend.insert_run(sample_run)
        runs = sqlite_backend.list_runs(labels={"git_branch": "nonexistent"})
        assert len(runs) == 0

    def test_limit(self, sqlite_backend, sample_run, sample_run_b):
        sqlite_backend.insert_run(sample_run)
        sqlite_backend.insert_run(sample_run_b)
        runs = sqlite_backend.list_runs(limit=1)
        assert len(runs) == 1


class TestSQLiteAggregation:
    def _insert_run_with_samples(self, backend, run):
        backend.insert_run(run)
        samples = [
            Sample(run_id=run.run_id, timestamp=1.0, metric_name="Frontend_Bound", value=15.0, unit="%"),
            Sample(run_id=run.run_id, timestamp=1.0, metric_name="Backend_Bound", value=45.0, unit="%"),
            Sample(run_id=run.run_id, timestamp=1.0, metric_name="Backend_Bound.Memory_Bound", value=30.0, unit="%"),
            Sample(run_id=run.run_id, timestamp=1.0, metric_name="Backend_Bound.Memory_Bound.DRAM_Bound", value=18.0, unit="%"),
            Sample(run_id=run.run_id, timestamp=2.0, metric_name="Frontend_Bound", value=16.0, unit="%"),
            Sample(run_id=run.run_id, timestamp=2.0, metric_name="Backend_Bound", value=44.0, unit="%"),
            Sample(run_id=run.run_id, timestamp=2.0, metric_name="Backend_Bound.Memory_Bound", value=29.0, unit="%"),
            Sample(run_id=run.run_id, timestamp=2.0, metric_name="Backend_Bound.Memory_Bound.DRAM_Bound", value=20.0, unit="%"),
        ]
        backend.insert_samples(samples)

    def test_get_aggregated_metrics(self, sqlite_backend, sample_run):
        self._insert_run_with_samples(sqlite_backend, sample_run)
        metrics = sqlite_backend.get_aggregated_metrics(sample_run.run_id)
        assert len(metrics) == 4
        # Should be ordered by avg_value desc
        assert metrics[0]["metric_name"] == "Backend_Bound"
        assert metrics[0]["avg_value"] == 44.5

    def test_query_bottlenecks(self, sqlite_backend, sample_run):
        self._insert_run_with_samples(sqlite_backend, sample_run)
        bottlenecks = sqlite_backend.query_bottlenecks(process_name="redis-server", min_percentage=10.0)
        names = [b["metric_name"] for b in bottlenecks]
        assert "Backend_Bound" in names
        assert "Backend_Bound.Memory_Bound" in names
        assert "Backend_Bound.Memory_Bound.DRAM_Bound" in names
        assert "Frontend_Bound" in names

    def test_query_by_bottleneck(self, sqlite_backend, sample_run, sample_run_b):
        self._insert_run_with_samples(sqlite_backend, sample_run)

        # Run B has lower DRAM_Bound
        sqlite_backend.insert_run(sample_run_b)
        samples_b = [
            Sample(run_id=sample_run_b.run_id, timestamp=1.0, metric_name="Backend_Bound.Memory_Bound.DRAM_Bound", value=3.0, unit="%"),
        ]
        sqlite_backend.insert_samples(samples_b)

        # Query for DRAM_Bound >= 10%
        results = sqlite_backend.query_by_bottleneck("DRAM_Bound", min_pct=10.0)
        assert len(results) == 1
        assert results[0]["run_id"] == sample_run.run_id

    def test_query_by_bottleneck_with_labels(self, sqlite_backend, sample_run):
        self._insert_run_with_samples(sqlite_backend, sample_run)
        results = sqlite_backend.query_by_bottleneck(
            "DRAM_Bound",
            min_pct=10.0,
            labels={"git_branch": "unstable"},
        )
        assert len(results) == 1

        # Wrong label should return nothing
        results = sqlite_backend.query_by_bottleneck(
            "DRAM_Bound",
            min_pct=10.0,
            labels={"git_branch": "wrong"},
        )
        assert len(results) == 0


class TestSQLiteUpdateRun:
    def test_update_run(self, sqlite_backend, sample_run):
        sqlite_backend.insert_run(sample_run)
        now = datetime.now(timezone.utc)
        sqlite_backend.update_run(sample_run.run_id, now, 30.5)
        run = sqlite_backend.get_run(sample_run.run_id)
        assert run.duration_seconds == 30.5
        assert run.ended_at is not None
