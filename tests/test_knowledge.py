"""Tests for knowledge base."""

from topdown.knowledge.metrics import (
    METRICS_KB,
    get_metric_info,
    list_all_metrics,
    get_children,
    get_parent,
)
from topdown.knowledge.methodology import get_methodology


class TestMetricsKB:
    def test_has_all_l1_nodes(self):
        l1 = [name for name, info in METRICS_KB.items() if info.get("level") == 1]
        names = {n.split(".")[-1] for n in l1}
        assert names == {"Frontend_Bound", "Bad_Speculation", "Backend_Bound", "Retiring"}

    def test_has_l2_nodes(self):
        l2 = [name for name, info in METRICS_KB.items() if info.get("level") == 2]
        assert len(l2) == 8

    def test_has_deep_nodes(self):
        # At least some L4+ nodes
        deep = [name for name, info in METRICS_KB.items() if info.get("level", 0) >= 4]
        assert len(deep) >= 30

    def test_total_count(self):
        assert len(METRICS_KB) >= 100  # 120 in full superset

    def test_all_have_description(self):
        for name, info in METRICS_KB.items():
            assert "description" in info, f"{name} missing description"
            assert len(info["description"]) > 10, f"{name} has too short description"

    def test_all_have_level(self):
        for name, info in METRICS_KB.items():
            assert "level" in info, f"{name} missing level"
            assert 1 <= info["level"] <= 6, f"{name} has invalid level {info['level']}"

    def test_all_have_tuning_hints(self):
        for name, info in METRICS_KB.items():
            assert "tuning_hints" in info, f"{name} missing tuning_hints"
            assert isinstance(info["tuning_hints"], list)

    def test_all_have_typical_causes(self):
        for name, info in METRICS_KB.items():
            assert "typical_causes" in info, f"{name} missing typical_causes"
            assert isinstance(info["typical_causes"], list)

    def test_consistent_paths(self):
        """Metric names should be dot-separated, matching the parent chain."""
        for name, info in METRICS_KB.items():
            parts = name.split(".")
            assert len(parts) == info["level"], f"{name} has {len(parts)} parts but level {info['level']}"


class TestGetMetricInfo:
    def test_exact_match(self):
        info = get_metric_info("Backend_Bound")
        assert info is not None
        assert "description" in info

    def test_full_path(self):
        info = get_metric_info("Backend_Bound.Memory_Bound.DRAM_Bound")
        assert info is not None
        assert "DRAM" in info["description"] or "memory" in info["description"].lower()

    def test_partial_match(self):
        info = get_metric_info("DRAM_Bound")
        assert info is not None

    def test_unknown_metric(self):
        assert get_metric_info("Totally_Fake_Metric") is None

    def test_case_matters(self):
        # Exact case match
        assert get_metric_info("Backend_Bound") is not None


class TestGetChildren:
    def test_l1_children(self):
        children = get_children("Backend_Bound")
        assert len(children) == 2
        child_names = {c.split(".")[-1] for c in children}
        assert child_names == {"Memory_Bound", "Core_Bound"}

    def test_l2_children(self):
        children = get_children("Backend_Bound.Memory_Bound")
        assert len(children) >= 5  # L1, L2, L3, DRAM, Store, possibly CXL

    def test_leaf_has_no_children(self):
        # Find a leaf node
        leaf = None
        for name, info in METRICS_KB.items():
            children = get_children(name)
            if not children:
                leaf = name
                break
        assert leaf is not None

    def test_nonexistent_parent(self):
        assert get_children("Nonexistent") == []


class TestGetParent:
    def test_l1_has_no_parent(self):
        assert get_parent("Backend_Bound") is None

    def test_l2_parent(self):
        assert get_parent("Backend_Bound.Memory_Bound") == "Backend_Bound"

    def test_deep_parent(self):
        assert get_parent("Backend_Bound.Memory_Bound.DRAM_Bound") == "Backend_Bound.Memory_Bound"


class TestListAllMetrics:
    def test_returns_sorted(self):
        names = list_all_metrics()
        assert names == sorted(names)

    def test_count(self):
        assert len(list_all_metrics()) >= 100


class TestMethodology:
    def test_returns_text(self):
        text = get_methodology()
        assert len(text) > 100
        assert "Top-Down" in text
        assert "Frontend" in text
        assert "Backend" in text
        assert "Retiring" in text
