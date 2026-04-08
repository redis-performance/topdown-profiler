"""Tests for bottleneck detection."""

from topdown.analysis.bottleneck import (
    Bottleneck,
    find_bottlenecks,
    find_deepest_bottlenecks,
    format_bottleneck_path,
    summarize_bottlenecks,
)


class TestFindBottlenecks:
    def test_finds_top_bottlenecks(self, sample_metrics):
        found = find_bottlenecks(sample_metrics, top_n=5, min_percentage=5.0)
        assert len(found) > 0
        assert found[0].value >= found[-1].value  # sorted desc

    def test_respects_min_percentage(self, sample_metrics):
        found = find_bottlenecks(sample_metrics, min_percentage=20.0)
        for b in found:
            assert b.value >= 20.0

    def test_respects_top_n(self, sample_metrics):
        found = find_bottlenecks(sample_metrics, top_n=2)
        assert len(found) <= 2

    def test_respects_max_level(self, sample_metrics):
        found = find_bottlenecks(sample_metrics, max_level=1)
        for b in found:
            assert b.level == 1

    def test_empty_input(self):
        assert find_bottlenecks([]) == []

    def test_no_match(self):
        metrics = [{"metric_name": "Retiring", "value": 2.0, "unit": "%"}]
        found = find_bottlenecks(metrics, min_percentage=50.0)
        assert found == []

    def test_skips_non_percentage(self):
        metrics = [
            {"metric_name": "IPC", "value": 3.2, "unit": "instructions/cycle"},
            {"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"},
        ]
        found = find_bottlenecks(metrics)
        assert len(found) == 1
        assert found[0].metric_name == "Backend_Bound"

    def test_bottleneck_dataclass(self, sample_metrics):
        found = find_bottlenecks(sample_metrics, top_n=1)
        b = found[0]
        assert isinstance(b, Bottleneck)
        assert b.metric_name
        assert b.path
        assert b.level >= 1
        d = b.to_dict()
        assert "metric_name" in d
        assert "path" in d


class TestFindDeepestBottlenecks:
    def test_finds_deepest_per_root(self, sample_metrics):
        found = find_deepest_bottlenecks(sample_metrics, min_percentage=5.0)
        # Should find at most one per L1 root
        roots = [b.metric_name.split(".")[0] for b in found]
        assert len(roots) == len(set(roots))

    def test_prefers_deeper_levels(self):
        metrics = [
            {"metric_name": "Backend_Bound", "value": 45.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Memory_Bound", "value": 30.0, "unit": "%"},
            {"metric_name": "Backend_Bound.Memory_Bound.DRAM_Bound", "value": 18.0, "unit": "%"},
        ]
        found = find_deepest_bottlenecks(metrics, min_percentage=5.0)
        backend_bottleneck = [b for b in found if b.metric_name.startswith("Backend")]
        assert len(backend_bottleneck) == 1
        assert "DRAM_Bound" in backend_bottleneck[0].metric_name


class TestFormatBottleneckPath:
    def test_single_level(self):
        assert format_bottleneck_path("Backend_Bound") == "Backend_Bound"

    def test_multi_level(self):
        assert format_bottleneck_path("Backend_Bound.Memory_Bound.L3_Bound") == "Backend_Bound -> Memory_Bound -> L3_Bound"


class TestSummarizeBottlenecks:
    def test_with_bottlenecks(self, sample_metrics):
        found = find_bottlenecks(sample_metrics, top_n=3)
        summary = summarize_bottlenecks(found)
        assert "Top bottlenecks:" in summary
        assert "%" in summary

    def test_empty(self):
        assert "No significant" in summarize_bottlenecks([])
