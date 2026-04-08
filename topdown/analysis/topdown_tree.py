"""Build and traverse the TMA top-down hierarchy from flat metric lists."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TreeNode:
    """A node in the TMA top-down hierarchy."""

    name: str
    full_path: str
    value: float = 0.0
    unit: str = "%"
    status: str = ""
    children: dict[str, TreeNode] = field(default_factory=dict)

    @property
    def level(self) -> int:
        return self.full_path.count(".") + 1 if self.full_path else 0

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    def to_dict(self) -> dict:
        """Recursive dict representation."""
        result = {
            "name": self.name,
            "full_path": self.full_path,
            "value": self.value,
            "unit": self.unit,
            "level": self.level,
        }
        if self.status:
            result["status"] = self.status
        if self.children:
            result["children"] = {k: v.to_dict() for k, v in self.children.items()}
        return result

    def walk(self) -> list[TreeNode]:
        """Depth-first walk of all nodes including self."""
        nodes = [self]
        for child in self.children.values():
            nodes.extend(child.walk())
        return nodes


def build_tree(metrics: list[dict]) -> TreeNode:
    """Build a tree from a flat list of metrics.

    Input: [{"metric_name": "Backend_Bound.Memory_Bound.L3_Bound", "value": 23.0, "unit": "%", ...}, ...]
    Output: TreeNode root with nested children.
    """
    root = TreeNode(name="Pipeline Slots", full_path="", value=100.0, unit="%")

    for metric in metrics:
        name = metric.get("metric_name", "")
        if not name:
            continue

        parts = name.split(".")
        current = root

        for i, part in enumerate(parts):
            if part not in current.children:
                path = ".".join(parts[: i + 1])
                current.children[part] = TreeNode(name=part, full_path=path)
            current = current.children[part]

        # Set the value on the leaf/target node
        current.value = metric.get("value", metric.get("avg_value", 0.0))
        current.unit = metric.get("unit", "%")
        current.status = metric.get("status", "")

    return root


def get_node(root: TreeNode, path: str) -> TreeNode | None:
    """Get a specific node by its dot-separated path."""
    if not path:
        return root

    parts = path.split(".")
    current = root
    for part in parts:
        if part not in current.children:
            return None
        current = current.children[part]
    return current


def get_level_nodes(root: TreeNode, level: int) -> list[TreeNode]:
    """Get all nodes at a specific TMA level."""
    return [n for n in root.walk() if n.level == level and n.full_path]


def format_tree_text(node: TreeNode, indent: int = 0, max_level: int | None = None) -> str:
    """Format tree as indented text with values."""
    lines = []

    if node.full_path:  # Skip root
        prefix = "  " * indent
        bar = _value_bar(node.value)
        status_str = f" [{node.status}]" if node.status else ""
        lines.append(f"{prefix}{node.name:<40} {node.value:6.1f}{node.unit}  {bar}{status_str}")

    if max_level is not None and node.level >= max_level:
        return "\n".join(lines)

    for child in sorted(node.children.values(), key=lambda c: c.value, reverse=True):
        lines.append(format_tree_text(child, indent + 1, max_level))

    return "\n".join(lines)


def _value_bar(value: float, max_width: int = 30) -> str:
    """Simple ASCII bar for a percentage value."""
    if value <= 0:
        return ""
    filled = min(int(value / 100.0 * max_width), max_width)
    return "█" * filled
