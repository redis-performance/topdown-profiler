"""VTune-style pipeline slot funnel analysis.

Shows where 100% of pipeline slots go: what percentage is useful work (Retiring)
vs wasted in Frontend_Bound, Bad_Speculation, Backend_Bound — then drills down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from topdown.analysis.topdown_tree import TreeNode, build_tree


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
        "Pipeline Slots Funnel (100% total)",
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


def format_funnel_comparison(
    result_a: FunnelResult,
    result_b: FunnelResult,
    label_a: str = "Baseline",
    label_b: str = "Comparison",
) -> str:
    """Format two funnels side-by-side with delta column.

    Output looks like:

    Pipeline Slots (100%)             Baseline    Comparison    Delta
    ├── Frontend_Bound                  23.2%        25.9%     +2.8%
    ├── Bad_Speculation                  7.7%         2.3%     -5.4%  ▼
    ├── Backend_Bound                   16.3%        16.7%     +0.4%
    │   ├── Memory_Bound                13.0%        13.9%     +0.9%
    │   └── Core_Bound                   3.3%         2.8%     -0.5%
    └── Retiring                        52.9%        55.1%     +2.2%  ▲
    """
    # Index entries by metric_name for lookup
    map_a = {e.metric_name: e for e in result_a.entries}
    map_b = {e.metric_name: e for e in result_b.entries}

    # Union of all metric names, preserving the tree order from A
    all_names = []
    seen = set()
    for e in result_a.entries:
        if e.metric_name not in seen:
            all_names.append(e.metric_name)
            seen.add(e.metric_name)
    for e in result_b.entries:
        if e.metric_name not in seen:
            all_names.append(e.metric_name)
            seen.add(e.metric_name)

    # Build tree connectors from indentation
    def _tree_prefix(indent: int, is_last: bool = False) -> str:
        if indent == 0:
            return ""
        parts = "│   " * (indent - 1)
        return parts + ("└── " if is_last else "├── ")

    # Header
    lines = [
        f"Pipeline Slots (100%)             {label_a:>10}  {label_b:>10}    Delta",
        "─" * 75,
    ]

    # Determine which entries are last children at each indent level
    indent_counts: dict[int, int] = {}
    indent_seen: dict[int, int] = {}
    for name in all_names:
        entry = map_a.get(name) or map_b.get(name)
        if entry:
            indent_counts[entry.indent] = indent_counts.get(entry.indent, 0) + 1
            indent_seen[entry.indent] = 0

    for name in all_names:
        entry_a = map_a.get(name)
        entry_b = map_b.get(name)
        ref = entry_a or entry_b

        val_a = entry_a.value if entry_a else 0.0
        val_b = entry_b.value if entry_b else 0.0
        delta = val_b - val_a

        # Delta indicator
        if abs(delta) >= 3.0:
            indicator = " ▲" if ref.is_useful and delta > 0 else (" ▼" if not ref.is_useful and delta < 0 else " ▲" if delta < 0 else " ▼")
        elif abs(delta) >= 1.5:
            indicator = ""
        else:
            indicator = ""

        indent = ref.indent
        tree = _tree_prefix(indent)
        metric_label = ref.path

        col_a = f"{val_a:.1f}%" if entry_a else "  —  "
        col_b = f"{val_b:.1f}%" if entry_b else "  —  "
        delta_str = f"{delta:+.1f}%"

        # Pad the metric name + tree prefix to fixed width
        name_col = f"{tree}{metric_label}"
        lines.append(f"{name_col:<35} {col_a:>9}  {col_b:>10}  {delta_str:>8}{indicator}")

    # Summary
    delta_useful = result_b.useful_work_pct - result_a.useful_work_pct
    lines.append("─" * 75)
    lines.append(
        f"{'Useful work (Retiring)':<35} {result_a.useful_work_pct:8.1f}%  {result_b.useful_work_pct:9.1f}%  {delta_useful:+7.1f}%"
        + (" ▲" if delta_useful > 1.0 else "")
    )

    return "\n".join(lines)
