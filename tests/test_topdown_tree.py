"""Tests for TMA tree building and traversal."""

from topdown.analysis.topdown_tree import (
    TreeNode,
    build_tree,
    format_tree_text,
    get_level_nodes,
    get_node,
)


class TestBuildTree:
    def test_builds_from_flat_metrics(self, sample_metrics):
        tree = build_tree(sample_metrics)
        assert tree.name == "Pipeline Slots"
        assert len(tree.children) == 4  # Frontend, Backend, Bad_Spec, Retiring

    def test_nested_hierarchy(self, sample_metrics):
        tree = build_tree(sample_metrics)
        backend = tree.children["Backend_Bound"]
        assert backend.value == 45.0
        assert "Memory_Bound" in backend.children
        memory = backend.children["Memory_Bound"]
        assert memory.value == 30.0
        assert "L3_Bound" in memory.children

    def test_level_property(self, sample_metrics):
        tree = build_tree(sample_metrics)
        assert tree.level == 0  # root
        assert tree.children["Backend_Bound"].level == 1
        assert tree.children["Backend_Bound"].children["Memory_Bound"].level == 2

    def test_empty_input(self):
        tree = build_tree([])
        assert tree.name == "Pipeline Slots"
        assert len(tree.children) == 0

    def test_single_metric(self):
        tree = build_tree([{"metric_name": "Retiring", "value": 55.0, "unit": "%"}])
        assert "Retiring" in tree.children
        assert tree.children["Retiring"].value == 55.0

    def test_deep_hierarchy(self):
        metrics = [
            {"metric_name": "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss.Load_STLB_Miss_4K", "value": 2.1, "unit": "%"},
        ]
        tree = build_tree(metrics)
        node = get_node(tree, "Backend_Bound.Memory_Bound.L1_Bound.DTLB_Load.Load_STLB_Miss.Load_STLB_Miss_4K")
        assert node is not None
        assert node.value == 2.1
        assert node.level == 6

    def test_to_dict(self, sample_metrics):
        tree = build_tree(sample_metrics)
        d = tree.to_dict()
        assert d["name"] == "Pipeline Slots"
        assert "children" in d
        assert "Backend_Bound" in d["children"]

    def test_walk(self, sample_metrics):
        tree = build_tree(sample_metrics)
        all_nodes = tree.walk()
        assert len(all_nodes) >= 8  # root + 8 metrics (some intermediate nodes auto-created)

    def test_is_leaf(self, sample_metrics):
        tree = build_tree(sample_metrics)
        assert not tree.children["Backend_Bound"].is_leaf
        l3 = get_node(tree, "Backend_Bound.Memory_Bound.L3_Bound")
        assert l3.is_leaf


class TestGetNode:
    def test_get_existing_node(self, sample_metrics):
        tree = build_tree(sample_metrics)
        node = get_node(tree, "Backend_Bound.Memory_Bound")
        assert node is not None
        assert node.value == 30.0

    def test_get_nonexistent(self, sample_metrics):
        tree = build_tree(sample_metrics)
        assert get_node(tree, "Nonexistent.Path") is None

    def test_get_root(self, sample_metrics):
        tree = build_tree(sample_metrics)
        assert get_node(tree, "") is tree


class TestGetLevelNodes:
    def test_level_1(self, sample_metrics):
        tree = build_tree(sample_metrics)
        l1 = get_level_nodes(tree, 1)
        names = {n.name for n in l1}
        assert names == {"Frontend_Bound", "Backend_Bound", "Bad_Speculation", "Retiring"}

    def test_level_2(self, sample_metrics):
        tree = build_tree(sample_metrics)
        l2 = get_level_nodes(tree, 2)
        names = {n.name for n in l2}
        assert "Memory_Bound" in names
        assert "Core_Bound" in names


class TestFormatTreeText:
    def test_produces_output(self, sample_metrics):
        tree = build_tree(sample_metrics)
        text = format_tree_text(tree)
        assert "Backend_Bound" in text
        assert "Memory_Bound" in text
        assert "45.0" in text

    def test_max_level(self, sample_metrics):
        tree = build_tree(sample_metrics)
        text = format_tree_text(tree, max_level=1)
        assert "Backend_Bound" in text
        # L2 nodes shouldn't appear
        assert "Memory_Bound" not in text
