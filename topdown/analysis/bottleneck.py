"""Identify and rank TMA bottlenecks.

Only slot-denominated nodes are ranked against each other. toplev also
reports nodes as a fraction of clocks, stalls or uops ("% Clocks", ...);
those are not comparable with slot shares, so ``find_detail_bottlenecks``
returns them separately, each with the slot-share parent that bounds its
real impact.
"""

from __future__ import annotations

from dataclasses import dataclass

from topdown.units import (
    DETAIL_TITLE,
    format_value,
    is_bottleneck_candidate,
    is_percent_unit,
    is_quality_metric,
    is_slot_unit,
    mux_note,
    normalize_unit,
    slot_parent_note,
)


@dataclass
class Bottleneck:
    """A ranked bottleneck from TMA analysis."""

    metric_name: str
    path: str  # Human-readable: "Backend_Bound -> Memory_Bound -> L3_Bound"
    value: float
    unit: str
    level: int
    # For detail (non-slot) nodes: the slot-share parent that bounds them.
    slot_ancestor: str = ""
    slot_ancestor_value: float | None = None

    @property
    def kind(self) -> str:
        return "slot" if is_slot_unit(self.unit) else "detail"

    def to_dict(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "path": self.path,
            "value": self.value,
            "unit": self.unit,
            "level": self.level,
            "kind": self.kind,
            "slot_ancestor": self.slot_ancestor,
            "slot_ancestor_value": self.slot_ancestor_value,
        }


def _candidates(metrics: list[dict], include_useful: bool) -> list[tuple[str, float, str]]:
    """(name, value, normalized unit) for percentage nodes that can be bottlenecks."""
    out = []
    for m in metrics:
        name = m.get("metric_name", "")
        if not name or is_quality_metric(name):
            continue
        if not include_useful and not is_bottleneck_candidate(name):
            continue
        unit = normalize_unit(m.get("unit", "%"))
        if not is_percent_unit(unit):
            continue
        out.append((name, m.get("value", m.get("avg_value", 0.0)), unit))
    return out


def _make(name: str, value: float, unit: str) -> Bottleneck:
    return Bottleneck(
        metric_name=name,
        path=format_bottleneck_path(name),
        value=value,
        unit=unit,
        level=name.count(".") + 1,
    )


def find_bottlenecks(
    metrics: list[dict],
    top_n: int = 10,
    min_percentage: float = 5.0,
    max_level: int | None = None,
    include_useful: bool = False,
) -> list[Bottleneck]:
    """Find the top-N slot-share nodes above threshold.

    Args:
        metrics: List of dicts with metric_name, value (or avg_value), unit keys
        top_n: Maximum number of bottlenecks to return
        min_percentage: Minimum value threshold
        max_level: Only include metrics up to this TMA level
        include_useful: Also rank useful work (Retiring, Light_Operations and below)
    """
    bottlenecks = []
    for name, value, unit in _candidates(metrics, include_useful):
        if not is_slot_unit(unit) or value < min_percentage:
            continue
        b = _make(name, value, unit)
        if max_level is not None and b.level > max_level:
            continue
        bottlenecks.append(b)

    bottlenecks.sort(key=lambda b: (-b.value, b.metric_name))
    return bottlenecks[:top_n]


def find_detail_bottlenecks(
    metrics: list[dict],
    top_n: int = 5,
    min_percentage: float = 5.0,
    max_level: int | None = None,
    per_parent: int = 2,
) -> list[Bottleneck]:
    """Nodes measured against clocks/stalls/uops whose parent is a slot share.

    Only the first non-slot level is considered: deeper nodes are fractions of
    a parent that is itself not a slot share (e.g. MEM_Latency under a
    DRAM_Bound at 0% of stalls), so their size says nothing about slots. The
    parent must itself reach ``min_percentage``.
    Ordered by the parent's slot share, at most ``per_parent`` per parent, so a
    large "% Clocks" value under a small parent doesn't outrank real bottlenecks.
    """
    slot_values = {name: value for name, value, unit in _candidates(metrics, include_useful=True) if is_slot_unit(unit)}
    details = []
    for name, value, unit in _candidates(metrics, include_useful=False):
        parent = name.rpartition(".")[0]
        if is_slot_unit(unit) or value < min_percentage or slot_values.get(parent, -1.0) < min_percentage:
            continue
        b = _make(name, value, unit)
        if max_level is not None and b.level > max_level:
            continue
        b.slot_ancestor = parent
        b.slot_ancestor_value = slot_values[parent]
        details.append(b)

    details.sort(key=lambda b: (-b.slot_ancestor_value, -b.value, b.metric_name))
    kept: list[Bottleneck] = []
    per: dict[str, int] = {}
    for b in details:
        if per.get(b.slot_ancestor, 0) < per_parent:
            per[b.slot_ancestor] = per.get(b.slot_ancestor, 0) + 1
            kept.append(b)
    return kept[:top_n]


def find_deepest_bottlenecks(
    metrics: list[dict],
    top_n: int = 5,
    min_percentage: float = 5.0,
) -> list[Bottleneck]:
    """Drill each Level-2 branch down its largest slot-share path.

    From each Level-2 node above the threshold (e.g. Backend_Bound.Memory_Bound,
    Frontend_Bound.Fetch_Latency), repeatedly step into the largest slot-share
    child that is still above the threshold, and report where that stops. A
    Level-1 node is only reported when none of its branches reach the threshold.
    """
    slot = {
        name: (value, unit)
        for name, value, unit in _candidates(metrics, include_useful=False)
        if is_slot_unit(unit) and value >= min_percentage
    }
    children: dict[str, list[str]] = {}
    for name in slot:
        children.setdefault(name.rpartition(".")[0], []).append(name)

    starts = [n for n in slot if n.count(".") == 1]
    roots_with_branches = {n.split(".")[0] for n in starts}
    starts += [n for n in slot if "." not in n and n not in roots_with_branches]

    deepest = []
    for node in starts:
        while children.get(node):
            node = min(children[node], key=lambda c: (-slot[c][0], c))  # ties: first by name
        deepest.append(_make(node, *slot[node]))

    deepest.sort(key=lambda b: (-b.value, b.metric_name))
    return deepest[:top_n]


def format_bottleneck_path(metric_name: str) -> str:
    """Convert 'Backend_Bound.Memory_Bound.L3_Bound' to 'Backend_Bound -> Memory_Bound -> L3_Bound'."""
    return " -> ".join(metric_name.split("."))


def summarize_bottlenecks(
    bottlenecks: list[Bottleneck],
    details: list[Bottleneck] | None = None,
    mux: float | None = None,
) -> str:
    """Generate human-readable summary text."""
    lines = []
    if bottlenecks:
        lines.append("Top bottlenecks:")
        lines.extend(f"  {b.path}: {format_value(b.value, b.unit)}" for b in bottlenecks)
    elif not details:
        lines.append("No significant bottlenecks detected.")
    if details:
        if lines:
            lines.append("")
        lines.append(f"{DETAIL_TITLE}:")
        for b in details:
            note = slot_parent_note(b.slot_ancestor, b.slot_ancestor_value)
            lines.append(f"  {b.path}: {format_value(b.value, b.unit)}{note}")
    if mux is not None:
        lines.extend(["", mux_note(mux)])
    return "\n".join(lines)
