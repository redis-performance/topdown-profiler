"""Rich terminal output formatting."""

from rich.console import Console
from rich.table import Table
from rich.tree import Tree as RichTree
from rich.panel import Panel
from rich.text import Text

from topdown.analysis.topdown_tree import TreeNode, sort_children
from topdown.analysis.bottleneck import Bottleneck
from topdown.analysis.funnel import FunnelResult, funnel_notes
from topdown.analysis.compare import MetricDelta
from topdown.storage.models import Run
from topdown.units import (
    COMPOSITION_NOTE,
    delta_arrow,
    format_delta,
    format_value,
    is_slot_unit,
    mux_note,
    slot_parent_note,
    unit_suffix,
)

console = Console()

# Values on another denominator are not on the slot scale the colors encode.
NEUTRAL = "white"
# Narrowest useful funnel bar; below this the Distribution column is dropped.
MIN_BAR_WIDTH = 10


def color_for_value(value: float) -> str:
    """Get Rich color based on percentage value (higher = worse for bottlenecks)."""
    if value >= 50:
        return "bold red"
    elif value >= 25:
        return "red"
    elif value >= 15:
        return "yellow"
    elif value >= 5:
        return "cyan"
    return "green"


def color_for_direction(direction: str) -> str:
    """Get color for comparison direction."""
    if direction == "regressed":
        return "red"
    elif direction == "improved":
        return "green"
    return "dim"


def value_style(value: float, unit: str | None) -> str:
    return color_for_value(value) if is_slot_unit(unit) else NEUTRAL


def print_mux_note(mux: float | None):
    if mux is not None:
        console.print(f"[dim]{mux_note(mux)}[/dim]")


def print_bottlenecks(bottlenecks: list[Bottleneck], title: str = "Top-Down Bottlenecks"):
    """Print bottlenecks as a Rich table with color-coded values.

    The bar column only takes what is left once the Path column has room for
    its longest node name, and bars are scaled to it (100% = the full column),
    as in ``print_funnel``.
    """
    paths = [b.path + slot_parent_note(b.slot_ancestor, b.slot_ancestor_value) for b in bottlenecks]
    values = [format_value(b.value, b.unit) for b in bottlenecks]
    longest_word = max([len("Path"), *(len(w) for p in paths for w in p.split())])
    value_w = max([len("Value"), *map(len, values)])
    # 5 columns: 6 border characters plus 1 cell of padding on each side; "#" is 3, "Level" 5.
    bar_w = min(30, console.width - (6 + 2 * 5) - 3 - longest_word - value_w - 5)
    with_bars = any(is_slot_unit(b.unit) for b in bottlenecks) and bar_w >= MIN_BAR_WIDTH

    table = Table(title=title, show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Path", style="white")
    table.add_column("Value", justify="right", no_wrap=True)
    if with_bars:
        table.add_column("Bar", width=bar_w, no_wrap=True)
    table.add_column("Level", justify="center", width=5)

    for i, (b, path, value) in enumerate(zip(bottlenecks, paths, values), 1):
        color = value_style(b.value, b.unit)
        row = [str(i), path, Text(value, style=color)]
        if with_bars:
            # Bars are on the slot scale; other denominators don't get one.
            bar_len = round(min(b.value, 100.0) / 100.0 * bar_w) if is_slot_unit(b.unit) else 0
            row.append(Text("█" * bar_len, style=color))
        table.add_row(*row, f"L{b.level}")

    console.print(table)


def print_tree(node: TreeNode, title: str = "TMA Hierarchy"):
    """Print tree using Rich Tree widget."""
    rich_tree = RichTree(f"[bold]{title}[/bold]")
    _add_tree_children(rich_tree, node)
    console.print(rich_tree)


def _add_tree_children(rich_parent, node: TreeNode):
    """Recursively add children to a Rich tree."""
    for child in sort_children(node):
        color = value_style(child.value, child.unit)
        label = f"[{color}]{child.name}[/{color}]  {format_value(child.value, child.unit)}"
        if child.status:
            label += f"  [{color}][{child.status}][/{color}]"
        branch = rich_parent.add(label)
        _add_tree_children(branch, child)


def print_funnel(result: FunnelResult, title: str = "Pipeline Slots Funnel"):
    """Print VTune-style funnel analysis.

    Column widths are computed from the rows so node names never truncate, and
    bars are scaled to whatever width is left (100% = the full column); the
    Distribution column is dropped when fewer than ``MIN_BAR_WIDTH`` cells remain.
    """
    useful, wasted = (
        (f"{result.useful_work_pct:.1f}%", f"{result.wasted_pct:.1f}%") if result.has_retiring else ("—", "—")
    )
    console.print(Panel(
        f"[bold]Total Pipeline Slots: 100%[/bold]\n"
        f"  [green]Useful work (Retiring): {useful}[/green]\n"
        f"  [red]Wasted: {wasted}[/red]",
        title=title,
    ))

    names = [f"{'  ' * e.indent}{e.path}" for e in result.entries]
    values = [format_value(e.value, e.unit) for e in result.entries]
    name_w = max([len("Metric"), *map(len, names)])
    value_w = max([len("Share"), *map(len, values)])
    # 4 columns: 5 border characters plus 1 cell of padding on each side.
    bar_w = console.width - (5 + 2 * 4) - name_w - value_w - 1
    with_bars = bar_w >= MIN_BAR_WIDTH

    table = Table(show_header=True, show_lines=False, padding=(0, 1))
    table.add_column("Metric", style="white", overflow="fold")
    table.add_column("Share", justify="right")
    if with_bars:
        table.add_column("Distribution", no_wrap=True, width=bar_w)
    table.add_column("", width=1)

    for entry, name, value in zip(result.entries, names, values):
        color = "green" if entry.is_useful else value_style(entry.value, entry.unit)
        row = [name, Text(value, style=color)]
        if with_bars:
            # Only slot shares are on the bar scale.
            bar_len = round(min(entry.value, 100.0) / 100.0 * bar_w) if is_slot_unit(entry.unit) else 0
            row.append(Text("█" * bar_len, style=color))
        row.append(Text("✓", style="green") if entry.is_useful else Text("✗", style="red"))
        table.add_row(*row)

    console.print(table)
    for note in funnel_notes(result):
        console.print(f"[dim]{note}[/dim]")


def print_comparison(deltas: list[MetricDelta], label_a: str = "Run A", label_b: str = "Run B"):
    """Print comparison table with direction indicators.

    Run IDs go in the title so the value columns stay narrow enough for the
    metric names on an 80-column terminal.
    """
    table = Table(title=f"Comparison: A = {label_a}\nvs B = {label_b}")
    table.add_column("Metric", min_width=26, overflow="fold")
    table.add_column("A", justify="right", no_wrap=True)
    table.add_column("B", justify="right", no_wrap=True)
    table.add_column("Delta", justify="right", no_wrap=True)
    table.add_column("Direction", no_wrap=True)

    for d in deltas:
        color = color_for_direction(d.direction)
        table.add_row(
            d.metric_name + unit_suffix(d.unit),
            f"{d.value_a:.1f}%" if unit_suffix(d.unit) else format_value(d.value_a, d.unit),
            f"{d.value_b:.1f}%" if unit_suffix(d.unit) else format_value(d.value_b, d.unit),
            Text(format_delta(d.delta, d.unit), style=color),
            Text(f"{delta_arrow(d.delta)} {d.direction}", style=color),
        )

    console.print(table)
    if any(d.direction in ("improved", "regressed") for d in deltas):
        console.print(f"[dim]{COMPOSITION_NOTE}[/dim]")


def print_runs_table(runs: list[Run], title: str = "Profiling Runs"):
    """Print list of runs as a table."""
    table = Table(title=title)
    table.add_column("Run ID", style="cyan", no_wrap=True)
    table.add_column("Started", style="green")
    table.add_column("Process", style="yellow")
    table.add_column("Level")
    table.add_column("Duration")
    table.add_column("Key Labels", max_width=60)

    for r in runs:
        interesting = {
            k: v
            for k, v in r.labels.items()
            if k in (
                "git_branch", "git_hash", "test_name", "topology",
                "build_variant", "client_tool", "compiler",
            )
        }
        label_str = ", ".join(f"{k}={v}" for k, v in interesting.items()) if interesting else "-"

        table.add_row(
            r.run_id,
            r.started_at.strftime("%Y-%m-%d %H:%M"),
            r.process_name,
            str(r.level),
            f"{r.duration_seconds:.1f}s",
            label_str,
        )

    console.print(table)


def print_metric_explanation(metric_name: str, info: dict):
    """Print metric explanation with panels."""
    console.print(Panel(
        f"[bold]{metric_name}[/bold]\n\n"
        f"[white]{info.get('description', 'No description available.')}[/white]",
        title="Description",
    ))

    causes = info.get("typical_causes", [])
    if causes:
        cause_text = "\n".join(f"  • {c}" for c in causes)
        console.print(Panel(cause_text, title="Typical Causes"))

    hints = info.get("tuning_hints", [])
    if hints:
        hints_text = "\n".join(f"  • {h}" for h in hints)
        console.print(Panel(hints_text, title="Tuning Hints"))

    level = info.get("level")
    parent = info.get("parent")
    if level or parent:
        meta = f"Level: {level}" if level else ""
        if parent:
            meta += f"  |  Parent: {parent}"
        console.print(f"[dim]{meta}[/dim]")


def print_by_bottleneck_results(results: list[dict]):
    """Print runs that match a node query: one two-line record per run.

    Records rather than a table: a full run ID, a node path, a value and labels
    don't fit one row of an 80-column terminal, and the ID must stay copyable.
    """
    import json

    for r in results:
        labels = r.get("labels", "{}")
        if isinstance(labels, str):
            labels = json.loads(labels)
        interesting = {
            k: v for k, v in labels.items()
            if k in ("git_branch", "git_hash", "test_name", "topology", "build_variant", "client_tool")
        }
        label_str = ", ".join(f"{k}={v}" for k, v in interesting.items()) if interesting else "-"
        value = Text(format_value(r.get("avg_value", 0), r.get("unit")), style=value_style(r.get("avg_value", 0),
                                                                                       r.get("unit")))
        line = Text.assemble((str(r.get("run_id", "")), "cyan"), "  ", value, "  ", r.get("metric_name", ""))
        console.print(line)
        console.print(
            f"    {str(r.get('started_at', ''))[:16]} {r.get('process_name', '')}; {label_str}",
            style="dim",
            markup=False,
        )
