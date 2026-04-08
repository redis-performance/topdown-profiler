"""Rich terminal output formatting."""

from rich.console import Console
from rich.table import Table
from rich.tree import Tree as RichTree
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown

from topdown.analysis.topdown_tree import TreeNode
from topdown.analysis.bottleneck import Bottleneck
from topdown.analysis.funnel import FunnelResult
from topdown.analysis.compare import MetricDelta
from topdown.storage.models import Run

console = Console()


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


def print_bottlenecks(bottlenecks: list[Bottleneck], title: str = "Top-Down Bottlenecks"):
    """Print bottlenecks as a Rich table with color-coded values."""
    table = Table(title=title, show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Path", style="white")
    table.add_column("Value", justify="right", width=8)
    table.add_column("Bar", width=30)
    table.add_column("Level", justify="center", width=5)

    for i, b in enumerate(bottlenecks, 1):
        color = color_for_value(b.value)
        bar_len = max(1, int(b.value / 100.0 * 30))
        bar = Text("█" * bar_len, style=color)
        value_text = Text(f"{b.value:.1f}%", style=color)
        table.add_row(str(i), b.path, value_text, bar, f"L{b.level}")

    console.print(table)


def print_tree(node: TreeNode, title: str = "TMA Hierarchy"):
    """Print tree using Rich Tree widget."""
    rich_tree = RichTree(f"[bold]{title}[/bold]")
    _add_tree_children(rich_tree, node)
    console.print(rich_tree)


def _add_tree_children(rich_parent, node: TreeNode):
    """Recursively add children to a Rich tree."""
    for child in sorted(node.children.values(), key=lambda c: c.value, reverse=True):
        color = color_for_value(child.value)
        label = f"[{color}]{child.name}[/{color}]  {child.value:.1f}%"
        if child.status:
            label += f"  [{color}][{child.status}][/{color}]"
        branch = rich_parent.add(label)
        _add_tree_children(branch, child)


def print_funnel(result: FunnelResult, title: str = "Pipeline Slots Funnel"):
    """Print VTune-style funnel analysis."""
    console.print(Panel(
        f"[bold]Total Pipeline Slots: 100%[/bold]\n"
        f"  [green]Useful work (Retiring): {result.useful_work_pct:.1f}%[/green]\n"
        f"  [red]Wasted: {result.wasted_pct:.1f}%[/red]",
        title=title,
    ))

    table = Table(show_header=True, show_lines=False, padding=(0, 1))
    table.add_column("Metric", style="white")
    table.add_column("Share", justify="right", width=8)
    table.add_column("Distribution", width=40)
    table.add_column("", width=2)

    for entry in result.entries:
        indent = "  " * entry.indent
        name = f"{indent}{entry.path}"

        color = "green" if entry.is_useful else color_for_value(entry.value)
        value_text = Text(f"{entry.value:.1f}%", style=color)

        bar_len = max(0, int(entry.value / 2.5))  # Scale so 100% = 40 chars
        bar = Text("█" * bar_len, style=color)

        marker = Text("✓", style="green") if entry.is_useful else Text("✗", style="red")

        table.add_row(name, value_text, bar, marker)

    console.print(table)


def print_comparison(deltas: list[MetricDelta], label_a: str = "Run A", label_b: str = "Run B"):
    """Print comparison table with direction indicators."""
    table = Table(title=f"Comparison: {label_a} vs {label_b}")
    table.add_column("Metric")
    table.add_column(label_a, justify="right", width=8)
    table.add_column(label_b, justify="right", width=8)
    table.add_column("Delta", justify="right", width=10)
    table.add_column("Direction", width=10)
    table.add_column("Level", justify="center", width=5)

    for d in deltas:
        color = color_for_direction(d.direction)
        arrow = "↑" if d.delta > 0 else "↓" if d.delta < 0 else "="
        direction_text = Text(f"{arrow} {d.direction}", style=color)
        delta_text = Text(f"{d.delta:+.1f}%", style=color)

        table.add_row(
            d.metric_name,
            f"{d.value_a:.1f}%",
            f"{d.value_b:.1f}%",
            delta_text,
            direction_text,
            f"L{d.level}",
        )

    console.print(table)


def print_runs_table(runs: list[Run], title: str = "Profiling Runs"):
    """Print list of runs as a table."""
    table = Table(title=title)
    table.add_column("Run ID", style="cyan", max_width=12)
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
            r.run_id[:12],
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
    """Print runs that match a specific bottleneck query."""
    table = Table(title="Runs Matching Bottleneck Query")
    table.add_column("Run ID", style="cyan", max_width=12)
    table.add_column("Started")
    table.add_column("Process")
    table.add_column("Metric")
    table.add_column("Avg Value", justify="right")
    table.add_column("Labels", max_width=60)

    for r in results:
        import json
        labels = r.get("labels", "{}")
        if isinstance(labels, str):
            labels = json.loads(labels)
        interesting = {
            k: v for k, v in labels.items()
            if k in (
                "git_branch", "git_hash", "test_name", "topology",
                "build_variant", "client_tool",
            )
        }
        label_str = ", ".join(f"{k}={v}" for k, v in interesting.items()) if interesting else "-"

        color = color_for_value(r.get("avg_value", 0))
        value_text = Text(f"{r.get('avg_value', 0):.1f}%", style=color)

        table.add_row(
            str(r.get("run_id", ""))[:12],
            str(r.get("started_at", ""))[:16],
            r.get("process_name", ""),
            r.get("metric_name", ""),
            value_text,
            label_str,
        )

    console.print(table)
