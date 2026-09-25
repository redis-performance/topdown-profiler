"""Interpret the unit strings stored alongside TMA samples.

Collectors disagree on units. perf-stat (ARM) and uProf (AMD) store a bare
``"%"`` for every node. toplev (Intel) stores the denominator of each node,
e.g. ``"% Slots"``, ``"% Clocks"``, ``"% Stalls"``, and appends ``" <"`` when
the node is below its reporting threshold for that interval. Runs collected
before the parser stripped that marker still carry it in the database.

Only slot-denominated nodes are shares of the pipeline-slot funnel; a
``"% Clocks"`` child is not a fraction of its ``"% Slots"`` parent, so it can
neither be summed into the parent nor ranked or judged against slot shares.
"""

from __future__ import annotations

BELOW_THRESHOLD_MARKER = "<"

# Metrics that describe measurement quality, not where slots went.
# toplev's MUX is, per interval, the lowest event-multiplexing ratio across
# the event groups: MUX 3.0 means some group was counted ~3% of the interval
# and its values were scaled up from that.
QUALITY_METRICS = ("MUX",)

MUX_EXPLANATION = (
    "MUX is toplev's event-multiplexing ratio: per interval, the lowest share of time "
    "any event group was actually counted. Values from groups counted for less than the "
    "whole interval are scaled up, at every TMA level, so a low MUX means small "
    "differences between runs are noise."
)

DETAIL_TITLE = "Detail nodes (up to 2 per parent; fraction of their own denominator, not of pipeline slots)"
OTHER_DENOMINATOR_TITLE = "Other denominators, not comparable with slot shares (not judged)"
USEFUL_MIX_TITLE = "Useful-work mix (not judged)"
ESTIMATE_NOTE = "(est.) rows are toplev estimates of a slot share; siblings may add up to more than their parent."
# Deltas below display precision (0.1 pp) are "unchanged", not better or worse.
UNCHANGED_BELOW = 0.05
COMPOSITION_NOTE = (
    "Slot shares add up to 100%: one share falling makes others rise, so check throughput "
    "before reading a rise as a regression."
)

# Useful work: Retiring itself and the whole Light_Operations subtree. The
# Heavy_Operations subtree (e.g. Microcode_Sequencer) is retired work too, but
# TMA treats it as a cost, so it is ranked, marked and compared as one.
_USEFUL_ROOT = "Retiring"
_USEFUL_SUBTREE = "Retiring.Light_Operations"

_LEGACY_SLOT_UNITS = ("%_Slots", "slots")


def normalize_unit(unit: str | None) -> str:
    """Strip toplev's below-threshold marker: ``"% Slots <"`` -> ``"% Slots"``.

    Matches ``normalized_unit_sql`` for every input.
    """
    if not unit:
        return "%"
    return unit.rstrip(" " + BELOW_THRESHOLD_MARKER) or "%"


def is_below_threshold(unit: str | None) -> bool:
    return bool(unit) and unit.rstrip().endswith(BELOW_THRESHOLD_MARKER)


def is_slot_unit(unit: str | None) -> bool:
    """True if the value is a share of total pipeline slots."""
    u = normalize_unit(unit)
    return u == "%" or u in _LEGACY_SLOT_UNITS or u.startswith("% Slots")


def is_percent_unit(unit: str | None) -> bool:
    """True for any percentage-valued node, whatever its denominator."""
    u = normalize_unit(unit)
    return u.startswith("%") or u in _LEGACY_SLOT_UNITS


def unit_class(unit: str | None) -> str:
    """Units that measure the same thing compare equal: all slot shares are ``"slots"``."""
    return "slots" if is_slot_unit(unit) else normalize_unit(unit)


def is_quality_metric(metric_name: str) -> bool:
    return metric_name.upper() in QUALITY_METRICS


def is_useful_work(metric_name: str) -> bool:
    """Retiring and the Light_Operations subtree: slots spent on work worth doing."""
    return (
        metric_name == _USEFUL_ROOT or metric_name == _USEFUL_SUBTREE or metric_name.startswith(_USEFUL_SUBTREE + ".")
    )


def is_bottleneck_candidate(metric_name: str) -> bool:
    """False for quality metrics and useful work; everything else can be a bottleneck."""
    return bool(metric_name) and not is_quality_metric(metric_name) and not is_useful_work(metric_name)


def delta_direction(metric_name: str, unit: str | None) -> int:
    """+1 if an increase is better, -1 if worse, 0 if it has no better/worse reading.

    Only slot shares are judged. Retiring itself is better when higher; the
    Light_Operations subtree only redistributes useful slots, so it is
    neutral. Every other slot share, Heavy_Operations included, is a cost, so
    lower is better. Consistent with ``is_bottleneck_candidate``.
    """
    if not is_slot_unit(unit) or is_quality_metric(metric_name):
        return 0
    if metric_name == _USEFUL_ROOT:
        return 1
    if is_useful_work(metric_name):
        return 0
    return -1


def unit_suffix(unit: str | None) -> str:
    """Display suffix for a percentage.

    ``""`` for measured slot shares, ``" (est.)"`` for toplev's estimated or
    calculated ones ("% Slots_est", which can add up to more than their
    parent), ``" of Clocks"`` for other denominators.
    """
    if not is_percent_unit(unit):
        return ""
    u = normalize_unit(unit)
    if is_slot_unit(u):
        return " (est.)" if u.startswith("% Slots_") else ""
    denom = u[1:].strip()
    return f" of {denom}" if denom else ""


def format_value(value: float, unit: str | None) -> str:
    """``"40.4%"``, ``"28.2% of Stalls"``, or ``"1.25 GHz"`` for non-percentages."""
    if is_percent_unit(unit):
        return f"{value:.1f}%{unit_suffix(unit)}"
    return f"{value:.2f} {normalize_unit(unit)}"


def slot_parent_note(parent: str, parent_value: float | None) -> str:
    """Context for a detail node: its slot-share parent, which bounds its impact."""
    if not parent or parent_value is None:
        return ""
    return f"  (parent {parent.split('.')[-1]}: {parent_value:.1f}% of slots)"


def mux_caveat(mux: float) -> str:
    """Sentence to add when some events were not counted for the whole interval."""
    if mux >= 100.0:
        return ""
    share = f"{mux:.1f}" if mux < 1.0 else f"{mux:.0f}"
    scale = f" (~{100.0 / mux:.0f}x)" if mux > 0 else ""
    return (
        f"Some event groups were counted for only ~{share}% of an interval and their values "
        f"extrapolated{scale}, at any level; treat small differences as noise."
    )


def mux_note(mux: float) -> str:
    note = f"Data quality: MUX {mux:.1f}% (lowest per-interval event-multiplexing ratio)."
    caveat = mux_caveat(mux)
    return f"{note} {caveat}" if caveat else note


def format_delta(delta: float, unit: str | None) -> str:
    """``"+2.0 pp"`` for percentages (a difference of percentages), else the plain number."""
    return f"{delta:+.1f} pp" if is_percent_unit(unit) else f"{delta:+.2f}"


def delta_arrow(delta: float) -> str:
    return "↑" if delta > 0 else "↓" if delta < 0 else "="


def lowest_mux(metrics: list[dict]) -> float | None:
    """Lowest MUX: ``min_value`` from aggregated rows, else the raw sample values."""
    values = [
        m.get("min_value", m.get("value", m.get("avg_value")))
        for m in metrics
        if is_quality_metric(m.get("metric_name", ""))
    ]
    values = [v for v in values if v is not None]
    return min(values) if values else None


def node_query_empty_message(metric_name: str, min_pct: float, substring: bool) -> str:
    """What to say when a node lookup returns no rows."""
    if is_quality_metric(metric_name):
        return f"{metric_name} is a data-quality metric, not a TMA node; see `explain {metric_name}`."
    if substring:
        return f"No node named or containing '{metric_name}' reaches {min_pct}%."
    return f"No runs found where {metric_name} >= {min_pct}%."


def node_query_header(metric_name: str, min_pct: float, substring: bool) -> str:
    if substring:
        return (
            f"No node is named exactly '{metric_name}'; nodes containing it that reach {min_pct}% (in their own unit):"
        )
    return f"Runs where {metric_name} >= {min_pct}% (in the node's own unit):"


def truncated_note(limit: int) -> str:
    return f"Showing the first {limit}; narrow the time window or labels for more."


# SQL equivalents. ``pct`` is how a literal '%' must be written in the query
# text: "%" for SQLite, "%%" for psycopg queries with parameters.


def normalized_unit_sql(column: str, pct: str = "%") -> str:
    """SQL expression equal to ``normalize_unit(column)``."""
    return f"COALESCE(NULLIF(rtrim({column}, ' {BELOW_THRESHOLD_MARKER}'), ''), '{pct}')"


def percent_unit_sql(normalized: str, pct: str = "%") -> str:
    """SQL predicate equal to ``is_percent_unit`` on an already-normalized unit expression."""
    return f"(substr({normalized}, 1, 1) = '{pct}' OR {normalized} = 'slots')"


def slot_unit_sql(normalized: str, pct: str = "%") -> str:
    """SQL predicate equal to ``is_slot_unit`` on an already-normalized unit expression."""
    return f"({normalized} IN ('{pct}', '{pct}_Slots', 'slots') OR substr({normalized}, 1, 7) = '{pct} Slots')"


def bottleneck_candidate_sql(column: str) -> str:
    """SQL predicate equal to ``is_bottleneck_candidate(column)``."""
    return (
        f"({quality_metrics_sql(column)} AND {column} <> '' "
        f"AND {column} NOT IN ('{_USEFUL_ROOT}', '{_USEFUL_SUBTREE}') "
        f"AND substr({column}, 1, {len(_USEFUL_SUBTREE) + 1}) <> '{_USEFUL_SUBTREE}.')"
    )


def quality_metrics_sql(column: str) -> str:
    """SQL predicate equal to ``not is_quality_metric(column)`` (names are fixed identifiers)."""
    names = ", ".join(f"'{name}'" for name in QUALITY_METRICS)
    return f"upper({column}) NOT IN ({names})"
