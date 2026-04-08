"""Identify and rank TMA bottlenecks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bottleneck:
    """A ranked bottleneck from TMA analysis."""

    metric_name: str
    path: str  # Human-readable: "Backend_Bound -> Memory_Bound -> L3_Bound"
    value: float
    unit: str
    level: int

    def to_dict(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "path": self.path,
            "value": self.value,
            "unit": self.unit,
            "level": self.level,
        }


def find_bottlenecks(
    metrics: list[dict],
    top_n: int = 10,
    min_percentage: float = 5.0,
    max_level: int | None = None,
) -> list[Bottleneck]:
    """Find the top-N bottleneck metrics above threshold.

    Args:
        metrics: List of dicts with metric_name, value (or avg_value), unit keys
        top_n: Maximum number of bottlenecks to return
        min_percentage: Minimum value threshold
        max_level: Only include metrics up to this TMA level
    """
    bottlenecks = []
    for m in metrics:
        name = m.get("metric_name", "")
        value = m.get("value", m.get("avg_value", 0.0))
        unit = m.get("unit", "%")

        if not name:
            continue

        # Only percentage-based metrics are bottlenecks
        if unit not in ("%", "%_Slots", "slots"):
            continue

        if value < min_percentage:
            continue

        level = name.count(".") + 1
        if max_level is not None and level > max_level:
            continue

        bottlenecks.append(
            Bottleneck(
                metric_name=name,
                path=format_bottleneck_path(name),
                value=value,
                unit=unit,
                level=level,
            )
        )

    # Sort by value descending
    bottlenecks.sort(key=lambda b: b.value, reverse=True)
    return bottlenecks[:top_n]


def find_deepest_bottlenecks(
    metrics: list[dict],
    top_n: int = 5,
    min_percentage: float = 5.0,
) -> list[Bottleneck]:
    """Find the deepest (most specific) bottleneck for each L1 category.

    For each L1 category (Frontend_Bound, Backend_Bound, etc.), drill down
    to the deepest child that exceeds the threshold.
    """
    # Group by L1 root
    by_root: dict[str, list[dict]] = {}
    for m in metrics:
        name = m.get("metric_name", "")
        if not name:
            continue
        root = name.split(".")[0]
        by_root.setdefault(root, []).append(m)

    deepest = []
    for root, root_metrics in by_root.items():
        # Sort by level desc, then value desc
        candidates = sorted(
            root_metrics,
            key=lambda m: (m["metric_name"].count("."), m.get("value", m.get("avg_value", 0.0))),
            reverse=True,
        )
        for m in candidates:
            value = m.get("value", m.get("avg_value", 0.0))
            unit = m.get("unit", "%")
            if value >= min_percentage and unit in ("%", "%_Slots", "slots"):
                name = m["metric_name"]
                deepest.append(
                    Bottleneck(
                        metric_name=name,
                        path=format_bottleneck_path(name),
                        value=value,
                        unit=unit,
                        level=name.count(".") + 1,
                    )
                )
                break  # Only the deepest per root

    deepest.sort(key=lambda b: b.value, reverse=True)
    return deepest[:top_n]


def format_bottleneck_path(metric_name: str) -> str:
    """Convert 'Backend_Bound.Memory_Bound.L3_Bound' to 'Backend_Bound -> Memory_Bound -> L3_Bound'."""
    return " -> ".join(metric_name.split("."))


def summarize_bottlenecks(bottlenecks: list[Bottleneck]) -> str:
    """Generate human-readable summary text."""
    if not bottlenecks:
        return "No significant bottlenecks detected."

    lines = ["Top bottlenecks:"]
    for b in bottlenecks:
        lines.append(f"  {b.path}: {b.value:.1f}{b.unit}")
    return "\n".join(lines)
