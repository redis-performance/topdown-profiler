"""Tests for VTune-style funnel analysis."""

from topdown.analysis.funnel import build_funnel, format_funnel_text, FunnelResult


class TestBuildFunnel:
    def test_basic_funnel(self, sample_metrics):
        result = build_funnel(sample_metrics, max_level=2)
        assert isinstance(result, FunnelResult)
        assert result.total_slots == 100.0
        assert result.useful_work_pct > 0
        assert result.wasted_pct > 0
        assert result.useful_work_pct + result.wasted_pct == 100.0

    def test_entries_populated(self, sample_metrics):
        result = build_funnel(sample_metrics, max_level=3)
        assert len(result.entries) > 0
        names = [e.metric_name for e in result.entries]
        assert "Frontend_Bound" in names
        assert "Backend_Bound" in names
        assert "Retiring" in names

    def test_retiring_is_useful(self, sample_metrics):
        result = build_funnel(sample_metrics, max_level=2)
        retiring = [e for e in result.entries if "Retiring" in e.metric_name]
        for e in retiring:
            assert e.is_useful

    def test_non_retiring_is_waste(self, sample_metrics):
        result = build_funnel(sample_metrics, max_level=1)
        wasted = [e for e in result.entries if not e.is_useful]
        for e in wasted:
            assert e.metric_name != "Retiring"

    def test_max_level_limits_depth(self, sample_metrics):
        result_l1 = build_funnel(sample_metrics, max_level=1)
        result_l3 = build_funnel(sample_metrics, max_level=3)
        assert len(result_l1.entries) < len(result_l3.entries)

    def test_to_dict(self, sample_metrics):
        result = build_funnel(sample_metrics)
        d = result.to_dict()
        assert "total_slots" in d
        assert "entries" in d
        assert "useful_work_pct" in d

    def test_empty_input(self):
        result = build_funnel([])
        assert result.useful_work_pct == 0
        assert result.wasted_pct == 100.0


class TestFormatFunnelText:
    def test_produces_output(self, sample_metrics):
        result = build_funnel(sample_metrics)
        text = format_funnel_text(result)
        assert "Pipeline Slots Funnel" in text
        assert "Useful work" in text
        assert "Wasted" in text
        assert "█" in text
