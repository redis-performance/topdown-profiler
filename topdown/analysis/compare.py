"""Compare two profiling runs and show deltas."""

from __future__ import annotations

from dataclasses import dataclass

from topdown.units import (
    COMPOSITION_NOTE,
    OTHER_DENOMINATOR_TITLE,
    UNCHANGED_BELOW,
    USEFUL_MIX_TITLE,
    delta_arrow,
    delta_direction,
    format_delta,
    format_value,
    is_quality_metric,
    is_slot_unit,
    normalize_unit,
    unit_class,
)

# Rows listed per group by summarize_comparison.
SUMMARY_ROWS = 5


@dataclass
class MetricDelta:
    """Delta between two runs for a single metric."""

    metric_name: str
    value_a: float
    value_b: float
    delta: float  # value_b - value_a
    delta_percent: float | None  # relative change as percentage; None when value_a is 0
    direction: str  # "improved", "regressed", "unchanged", or "changed" (not judged)
    unit: str
    level: int

    def to_dict(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "value_a": self.value_a,
            "value_b": self.value_b,
            "delta": self.delta,
            "delta_percent": self.delta_percent,
            "direction": self.direction,
            "unit": self.unit,
            "level": self.level,
        }


def compare_runs(
    metrics_a: list[dict],
    metrics_b: list[dict],
    threshold: float = 1.0,
) -> list[MetricDelta]:
    """Compare metrics from two runs.

    Args:
        metrics_a: First run's metrics (baseline)
        metrics_b: Second run's metrics (comparison)
        threshold: Minimum absolute delta to include

    Returns:
        List of MetricDelta sorted by abs(delta) descending.
    """
    # Build lookup dicts
    lookup_a = _build_lookup(metrics_a)
    lookup_b = _build_lookup(metrics_b)

    # Find common metrics
    common_names = set(lookup_a.keys()) & set(lookup_b.keys())

    deltas = []
    for name in common_names:
        val_a = lookup_a[name]["value"]
        val_b = lookup_b[name]["value"]
        unit = lookup_a[name]["unit"]
        if unit_class(unit) != unit_class(lookup_b[name]["unit"]):
            continue  # different denominators (e.g. another microarchitecture)

        delta = val_b - val_a
        if abs(delta) < threshold:
            continue

        # Relative change
        delta_pct = (delta / val_a * 100) if val_a != 0 else None

        # Only slot shares have a better/worse reading; see delta_direction.
        better_when = delta_direction(name, unit)
        if abs(delta) < UNCHANGED_BELOW:
            direction = "unchanged"
        elif better_when == 0:
            direction = "changed"
        else:
            direction = "improved" if delta * better_when > 0 else "regressed"

        deltas.append(
            MetricDelta(
                metric_name=name,
                value_a=val_a,
                value_b=val_b,
                delta=delta,
                delta_percent=delta_pct,
                direction=direction,
                unit=unit,
                level=name.count(".") + 1,
            )
        )

    # Slot shares first: a clocks/stalls delta isn't comparable with them.
    # Name breaks ties so the order doesn't depend on set iteration.
    deltas.sort(key=lambda d: (not is_slot_unit(d.unit), -abs(d.delta), d.metric_name))
    return deltas


def summarize_comparison(deltas: list[MetricDelta]) -> str:
    """Generate human-readable comparison summary."""
    if not deltas:
        return "No significant differences found."

    improved = [d for d in deltas if d.direction == "improved"]
    regressed = [d for d in deltas if d.direction == "regressed"]
    changed = [d for d in deltas if d.direction == "changed"]
    other_denominators = [d for d in changed if not is_slot_unit(d.unit)]
    useful_mix = [d for d in changed if is_slot_unit(d.unit)]

    lines = []
    for title, group in (
        ("Regressions", regressed),
        ("Improvements", improved),
        (OTHER_DENOMINATOR_TITLE, other_denominators),
        (USEFUL_MIX_TITLE, useful_mix),
    ):
        if group:
            lines.append(f"{title} ({len(group)}):")
            lines.extend(_delta_line(d) for d in group[:SUMMARY_ROWS])
            if len(group) > SUMMARY_ROWS:
                lines.append(f"  ... +{len(group) - SUMMARY_ROWS} more")
    if regressed or improved:
        lines.append(COMPOSITION_NOTE)
    if not lines:
        return "No significant differences found."

    return "\n".join(lines)


def _delta_line(d: MetricDelta) -> str:
    return (
        f"  {delta_arrow(d.delta)} {d.metric_name}: "
        f"{format_value(d.value_a, d.unit)} -> {format_value(d.value_b, d.unit)} "
        f"({format_delta(d.delta, d.unit)})"
    )


def _build_lookup(metrics: list[dict]) -> dict[str, dict]:
    """Build name -> metric dict. For duplicates, use the one with avg_value or last."""
    lookup = {}
    for m in metrics:
        name = m.get("metric_name", "")
        if not name or is_quality_metric(name):
            continue
        value = m.get("value", m.get("avg_value", 0.0))
        lookup[name] = {**m, "value": value, "unit": normalize_unit(m.get("unit", "%"))}
    return lookup
