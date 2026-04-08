"""Tests for run comparison."""

from topdown.analysis.compare import compare_runs, MetricDelta, summarize_comparison


METRICS_A = [
    {"metric_name": "Frontend_Bound", "value": 15.0, "unit": "%"},
    {"metric_name": "Backend_Bound", "value": 45.0, "unit": "%"},
    {"metric_name": "Backend_Bound.Memory_Bound", "value": 30.0, "unit": "%"},
    {"metric_name": "Bad_Speculation", "value": 10.0, "unit": "%"},
    {"metric_name": "Retiring", "value": 30.0, "unit": "%"},
]

METRICS_B = [
    {"metric_name": "Frontend_Bound", "value": 12.0, "unit": "%"},
    {"metric_name": "Backend_Bound", "value": 40.0, "unit": "%"},
    {"metric_name": "Backend_Bound.Memory_Bound", "value": 25.0, "unit": "%"},
    {"metric_name": "Bad_Speculation", "value": 8.0, "unit": "%"},
    {"metric_name": "Retiring", "value": 40.0, "unit": "%"},
]


class TestCompareRuns:
    def test_finds_deltas(self):
        deltas = compare_runs(METRICS_A, METRICS_B, threshold=1.0)
        assert len(deltas) > 0
        assert all(isinstance(d, MetricDelta) for d in deltas)

    def test_sorted_by_abs_delta(self):
        deltas = compare_runs(METRICS_A, METRICS_B, threshold=0.0)
        for i in range(len(deltas) - 1):
            assert abs(deltas[i].delta) >= abs(deltas[i + 1].delta)

    def test_direction_for_bound_metrics(self):
        deltas = compare_runs(METRICS_A, METRICS_B, threshold=0.0)
        # Backend_Bound went from 45 to 40 — that's improved (lower is better)
        backend = next(d for d in deltas if d.metric_name == "Backend_Bound")
        assert backend.direction == "improved"
        assert backend.delta == -5.0

    def test_direction_for_retiring(self):
        deltas = compare_runs(METRICS_A, METRICS_B, threshold=0.0)
        retiring = next(d for d in deltas if d.metric_name == "Retiring")
        # Retiring went from 30 to 40 — that's improved (higher is better)
        assert retiring.direction == "improved"
        assert retiring.delta == 10.0

    def test_threshold_filters(self):
        deltas_strict = compare_runs(METRICS_A, METRICS_B, threshold=5.0)
        deltas_loose = compare_runs(METRICS_A, METRICS_B, threshold=1.0)
        assert len(deltas_strict) <= len(deltas_loose)

    def test_identical_runs(self):
        deltas = compare_runs(METRICS_A, METRICS_A, threshold=1.0)
        assert len(deltas) == 0

    def test_empty_inputs(self):
        assert compare_runs([], []) == []
        assert compare_runs(METRICS_A, []) == []

    def test_to_dict(self):
        deltas = compare_runs(METRICS_A, METRICS_B, threshold=1.0)
        d = deltas[0].to_dict()
        assert "metric_name" in d
        assert "delta" in d
        assert "direction" in d

    def test_delta_percent(self):
        deltas = compare_runs(METRICS_A, METRICS_B, threshold=0.0)
        retiring = next(d for d in deltas if d.metric_name == "Retiring")
        # 30 -> 40: delta_percent = (10/30)*100 = 33.3%
        assert abs(retiring.delta_percent - 33.33) < 1.0


class TestSummarizeComparison:
    def test_with_deltas(self):
        deltas = compare_runs(METRICS_A, METRICS_B, threshold=1.0)
        summary = summarize_comparison(deltas)
        assert "Improvement" in summary or "Regression" in summary or "↑" in summary or "↓" in summary

    def test_no_deltas(self):
        summary = summarize_comparison([])
        assert "No significant" in summary
