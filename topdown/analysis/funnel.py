"""VTune-style pipeline slot funnel analysis.

Shows where 100% of pipeline slots go: what percentage is useful work (Retiring)
vs wasted in Frontend_Bound, Bad_Speculation, Backend_Bound — then drills down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from topdown.analysis.topdown_tree import TreeNode, build_tree, get_level_nodes


@dataclass
class FunnelEntry:
    """One row in the funnel: a metric and its share of total pipeline slots."""

    metric_name: str
    path: str
    value: float
    unit: str
    level: int
    indent: int = 0
    is_useful: bool = False  # True for Retiring and its children

    def to_dict(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "path": self.path,
            "value": self.value,
            "unit": self.unit,
            "level": self.level,
            "is_useful": self.is_useful,
        }


@dataclass
class FunnelResult:
    """Complete funnel analysis result."""

    total_slots: float = 100.0
    entries: list[FunnelEntry] = field(default_factory=list)
    useful_work_pct: float = 0.0
    wasted_pct: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_slots": self.total_slots,
            "useful_work_pct": self.useful_work_pct,
            "wasted_pct": self.wasted_pct,
            "entries": [e.to_dict() for e in self.entries],
        }


def build_funnel(metrics: list[dict], max_level: int = 3) -> FunnelResult:
    """Build a pipeline slot funnel from metrics.

    The funnel shows the hierarchical breakdown of where pipeline slots go,
    from Level 1 categories down to max_level.

    Args:
        metrics: List of dicts with metric_name, value/avg_value, unit
        max_level: Maximum TMA level to drill down to
    """
    tree = build_tree(metrics)
    result = FunnelResult()
    entries = []

    # Walk the tree depth-first, collecting entries up to max_level
    _walk_funnel(tree, entries, max_level, indent=0)

    result.entries = entries

    # Calculate useful vs wasted
    retiring = next((e for e in entries if e.metric_name == "Retiring"), None)
    result.useful_work_pct = retiring.value if retiring else 0.0
    result.wasted_pct = result.total_slots - result.useful_work_pct

    return result


def _walk_funnel(node: TreeNode, entries: list[FunnelEntry], max_level: int, indent: int):
    """Recursively walk tree and build funnel entries."""
    if node.full_path:  # Skip root
        is_useful = node.full_path.startswith("Retiring")
        entries.append(
            FunnelEntry(
                metric_name=node.full_path,
                path=node.name,
                value=node.value,
                unit=node.unit,
                level=node.level,
                indent=indent,
                is_useful=is_useful,
            )
        )

    if node.level >= max_level:
        return

    # Sort children by value descending for consistent output
    for child in sorted(node.children.values(), key=lambda c: c.value, reverse=True):
        _walk_funnel(child, entries, max_level, indent + 1 if node.full_path else 0)


def format_funnel_text(result: FunnelResult) -> str:
    """Format funnel as ASCII text with bars."""
    lines = [
        f"Pipeline Slots Funnel (100% total)",
        f"  Useful work (Retiring): {result.useful_work_pct:.1f}%",
        f"  Wasted:                 {result.wasted_pct:.1f}%",
        "",
    ]

    for entry in result.entries:
        prefix = "  " * entry.indent
        bar_len = max(1, int(entry.value / 2))  # Scale: 50% = 25 chars
        bar = "█" * bar_len

        if entry.is_useful:
            marker = "✓"
        else:
            marker = "✗"

        lines.append(f"  {prefix}{entry.path:<35} {entry.value:6.1f}%  {bar} {marker}")

    return "\n".join(lines)
