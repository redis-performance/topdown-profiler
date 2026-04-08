"""Compare two profiling runs and show deltas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MetricDelta:
    """Delta between two runs for a single metric."""

    metric_name: str
    value_a: float
    value_b: float
    delta: float  # value_b - value_a
    delta_percent: float  # relative change as percentage
    direction: str  # "improved", "regressed", "unchanged"
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


# For most TMA metrics, lower is better (less stalled).
# Exception: Retiring — higher is better (more useful work).
_HIGHER_IS_BETTER = {"Retiring", "Light_Operations", "Heavy_Operations"}


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
        unit = lookup_a[name].get("unit", "%")

        delta = val_b - val_a
        if abs(delta) < threshold:
            continue

        # Relative change
        delta_pct = (delta / val_a * 100) if val_a != 0 else 0.0

        # Determine direction
        root = name.split(".")[0]
        leaf = name.split(".")[-1]
        higher_is_better = root in _HIGHER_IS_BETTER or leaf in _HIGHER_IS_BETTER

        if abs(delta) < threshold:
            direction = "unchanged"
        elif higher_is_better:
            direction = "improved" if delta > 0 else "regressed"
        else:
            direction = "improved" if delta < 0 else "regressed"

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

    deltas.sort(key=lambda d: abs(d.delta), reverse=True)
    return deltas


def summarize_comparison(deltas: list[MetricDelta]) -> str:
    """Generate human-readable comparison summary."""
    if not deltas:
        return "No significant differences found."

    improved = [d for d in deltas if d.direction == "improved"]
    regressed = [d for d in deltas if d.direction == "regressed"]

    lines = []
    if regressed:
        lines.append(f"Regressions ({len(regressed)}):")
        for d in regressed[:5]:
            lines.append(f"  ↑ {d.metric_name}: {d.value_a:.1f}% -> {d.value_b:.1f}% ({d.delta:+.1f}%)")
    if improved:
        lines.append(f"Improvements ({len(improved)}):")
        for d in improved[:5]:
            lines.append(f"  ↓ {d.metric_name}: {d.value_a:.1f}% -> {d.value_b:.1f}% ({d.delta:+.1f}%)")

    return "\n".join(lines)


def _build_lookup(metrics: list[dict]) -> dict[str, dict]:
    """Build name -> metric dict. For duplicates, use the one with avg_value or last."""
    lookup = {}
    for m in metrics:
        name = m.get("metric_name", "")
        if not name:
            continue
        value = m.get("value", m.get("avg_value", 0.0))
        lookup[name] = {**m, "value": value}
    return lookup
