"""VTune-style pipeline slot funnel analysis.

Shows where 100% of pipeline slots go: what percentage is useful work (Retiring)
vs wasted in Frontend_Bound, Bad_Speculation, Backend_Bound — then drills down.

toplev reports some nodes against other denominators ("% Clocks", "% Stalls",
...). Those rows are labelled with their denominator and never drawn on the
slot scale, because they do not add up to their parent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from topdown.analysis.topdown_tree import TreeNode, build_tree, sort_children
from topdown.units import (
    COMPOSITION_NOTE,
    ESTIMATE_NOTE,
    delta_direction,
    format_delta,
    format_value,
    is_percent_unit,
    is_useful_work,
    is_slot_unit,
    lowest_mux,
    mux_caveat,
    mux_note,
    unit_class,
    unit_suffix,
)

# compare_funnel marks a slot-share delta of at least this many points.
DELTA_MARK_THRESHOLD = 3.0


@dataclass
class FunnelEntry:
    """One row in the funnel: a metric and its share of total pipeline slots."""

    metric_name: str
    path: str
    value: float
    unit: str
    level: int
    indent: int = 0
    is_useful: bool = False  # True for useful work: Retiring and the Light_Operations subtree

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
    mux: float | None = None  # lowest per-interval toplev MUX, if reported

    def to_dict(self) -> dict:
        return {
            "total_slots": self.total_slots,
            "useful_work_pct": self.useful_work_pct,
            "wasted_pct": self.wasted_pct,
            "mux": self.mux,
            "entries": [e.to_dict() for e in self.entries],
        }

    @property
    def has_retiring(self) -> bool:
        return any(e.metric_name == "Retiring" for e in self.entries)

    @property
    def has_other_units(self) -> bool:
        return any(not is_slot_unit(e.unit) for e in self.entries)


def build_funnel(metrics: list[dict], max_level: int = 3) -> FunnelResult:
    """Build a pipeline slot funnel from metrics.

    The funnel shows the hierarchical breakdown of where pipeline slots go,
    from Level 1 categories down to max_level.

    Args:
        metrics: List of dicts with metric_name, value/avg_value, unit
        max_level: Maximum TMA level to drill down to
    """
    tree = build_tree(metrics)
    result = FunnelResult(mux=lowest_mux(metrics))
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
        if not is_percent_unit(node.unit):
            return  # e.g. a toplev "Metric" row: not part of the slot breakdown
        is_useful = is_useful_work(node.full_path)
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

    for child in sort_children(node):
        _walk_funnel(child, entries, max_level, indent + 1 if node.full_path else 0)


def _walk_names(node: TreeNode) -> list[TreeNode]:
    """Depth-first, funnel order (slot shares first, then by value)."""
    out = [node] if node.full_path else []
    for child in sort_children(node):
        out.extend(_walk_names(child))
    return out


def format_funnel_text(result: FunnelResult) -> str:
    """Format funnel as ASCII text with bars."""
    useful, wasted = (
        (f"{result.useful_work_pct:.1f}%", f"{result.wasted_pct:.1f}%") if result.has_retiring else ("—", "—")
    )
    lines = [
        "Pipeline Slots Funnel (100% total)",
        f"  Useful work (Retiring): {useful}",
        f"  Wasted:                 {wasted}",
        "",
    ]

    width = max((len(unit_suffix(e.unit)) for e in result.entries), default=0)
    for entry in result.entries:
        prefix = "  " * entry.indent
        suffix = unit_suffix(entry.unit)
        # Only slot shares get a bar: other denominators aren't on that scale.
        # Scale: 50% = 25 chars; slot shares only, and none for an exact 0.
        bar = "" if suffix or entry.value <= 0 else "█" * max(1, int(entry.value / 2))

        if entry.is_useful:
            marker = "✓"
        else:
            marker = "✗"

        lines.append(f"  {prefix}{entry.path:<35} {entry.value:6.1f}%{suffix:<{width}}  {bar} {marker}")

    notes = funnel_notes(result)
    if notes:
        lines.extend(["", *notes])
    return "\n".join(lines)


def funnel_notes(result: FunnelResult) -> list[str]:
    """Caveats to print under a funnel: mixed denominators and multiplexing."""
    notes = []
    if any(unit_suffix(e.unit) == " (est.)" for e in result.entries):
        notes.append(ESTIMATE_NOTE)
    if result.has_other_units:
        notes.append(
            "Rows marked 'of Clocks', 'of Stalls', ... are fractions of that "
            "denominator, not of pipeline slots; they do not add up to their parent."
        )
    if result.mux is not None:
        notes.append(mux_note(result.mux))
    return notes


def format_funnel_comparison(
    result_a: FunnelResult,
    result_b: FunnelResult,
    label_a: str = "Baseline",
    label_b: str = "Comparison",
) -> str:
    """Format two funnels side-by-side with delta column.

    Output looks like:

    Pipeline Slots (100%)             Baseline    Comparison    Delta
    ├── Frontend_Bound                  23.2%        25.9%   +2.8 pp
    ├── Bad_Speculation                  7.7%         2.3%   -5.4 pp ▲
    ├── Backend_Bound                   16.3%        16.7%   +0.4 pp
    │   ├── Memory_Bound                13.0%        13.9%   +0.9 pp
    │   └── Core_Bound                   3.3%         2.8%   -0.5 pp
    └── Retiring                        52.9%        55.1%   +2.2 pp

    ▲ marks a slot-share change of at least 3 points for the better, ▼ for
    the worse. Children of Retiring only redistribute useful work and rows
    measured against another denominator carry it as a suffix; neither is
    marked.
    """
    # Index entries by metric_name for lookup
    map_a = {e.metric_name: e for e in result_a.entries}
    map_b = {e.metric_name: e for e in result_b.entries}

    # Row order: both runs' nodes merged into one tree and walked like a
    # funnel, so a row that exists in only one run still sits under its own
    # parent (and slot shares still come before other denominators).
    # A comes last, so for nodes in both runs the baseline's values set the order.
    union = [{"metric_name": e.metric_name, "value": e.value, "unit": e.unit}
             for e in [*result_b.entries, *result_a.entries]]
    all_names = [n.full_path for n in _walk_names(build_tree(union))]

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

    marked = False
    for name in all_names:
        entry_a = map_a.get(name)
        entry_b = map_b.get(name)
        ref = entry_a or entry_b
        suffix = unit_suffix(ref.unit)

        col_a = f"{entry_a.value:.1f}%" if entry_a else "  —  "
        col_b = f"{entry_b.value:.1f}%" if entry_b else "  —  "
        indicator = ""
        if not (entry_a and entry_b):
            delta_str = "—"
        elif unit_class(entry_a.unit) != unit_class(entry_b.unit):
            col_a, col_b = format_value(entry_a.value, entry_a.unit), format_value(entry_b.value, entry_b.unit)
            delta_str = "—"
            suffix = " (units differ)"
        else:
            delta = entry_b.value - entry_a.value
            delta_str = format_delta(delta, ref.unit)
            better_when = delta_direction(name, ref.unit)
            if better_when and abs(delta) >= DELTA_MARK_THRESHOLD:
                indicator = " ▲" if delta * better_when > 0 else " ▼"
                marked = True

        # Pad the metric name + tree prefix to fixed width
        name_col = f"{_tree_prefix(ref.indent)}{ref.path}"
        lines.append(f"{name_col:<35} {col_a:>9}  {col_b:>10}  {delta_str:>8}{indicator}{suffix}")

    # Summary: only computed when both sides report Retiring.
    lines.append("─" * 75)
    if result_a.has_retiring and result_b.has_retiring:
        delta_useful = result_b.useful_work_pct - result_a.useful_work_pct
        useful_mark = ""
        if abs(delta_useful) >= DELTA_MARK_THRESHOLD:
            useful_mark = " ▲" if delta_useful > 0 else " ▼"
            marked = True
        lines.append(
            f"{'Useful work (Retiring)':<35} {result_a.useful_work_pct:8.1f}%  "
            f"{result_b.useful_work_pct:9.1f}%  {delta_useful:+6.1f} pp{useful_mark}"
        )
    else:
        col_a = f"{result_a.useful_work_pct:.1f}%" if result_a.has_retiring else "—"
        col_b = f"{result_b.useful_work_pct:.1f}%" if result_b.has_retiring else "—"
        lines.append(f"{'Useful work (Retiring)':<35} {col_a:>9}  {col_b:>10}  {'—':>8}")
    if marked:
        lines.append(f"▲ better / ▼ worse: slot-share change of at least {DELTA_MARK_THRESHOLD:.0f} points")
        lines.append(COMPOSITION_NOTE)
    if result_a.mux is not None or result_b.mux is not None:
        mux_a = f"{result_a.mux:.1f}%" if result_a.mux is not None else "—"
        mux_b = f"{result_b.mux:.1f}%" if result_b.mux is not None else "—"
        lines.append(f"{'Data quality: MUX':<35} {mux_a:>9}  {mux_b:>10}")
        caveat = mux_caveat(min(m for m in (result_a.mux, result_b.mux) if m is not None))
        if caveat:
            lines.append(caveat)

    return "\n".join(lines)
