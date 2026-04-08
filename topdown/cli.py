"""CLI entry point for topdown-profiler."""

import re
import time
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from topdown import __version__
from topdown.config import get_config
from topdown.storage import get_backend
from topdown.storage.models import Run, Sample
from topdown.collector.labels import collect_auto_labels, merge_labels, parse_label_args

app = typer.Typer(
    name="topdown",
    help="Intel Top-Down Microarchitecture Analysis collector and query tool.",
    no_args_is_help=True,
)
console = Console()


def parse_duration(s: str) -> int:
    """Parse duration string like '30s', '5m', '1h' to seconds."""
    m = re.match(r"^(\d+)(s|m|h)$", s.strip())
    if not m:
        raise typer.BadParameter(f"Invalid duration '{s}', expected format: 30s, 5m, 1h")
    value, unit = int(m.group(1)), m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


def parse_time_window(s: str) -> float:
    """Parse time window like '24h', '7d', '30m' to hours."""
    m = re.match(r"^(\d+)(m|h|d)$", s.strip())
    if not m:
        raise typer.BadParameter(f"Invalid time window '{s}', expected format: 30m, 24h, 7d")
    value, unit = int(m.group(1)), m.group(2)
    return value * {"m": 1 / 60, "h": 1.0, "d": 24.0}[unit]


@app.command()
def collect(
    process: Annotated[str, typer.Option("--process", "-p", help="Process name to profile")],
    level: Annotated[int, typer.Option("--level", "-l", help="TMA level (1-6)")] = 2,
    duration: Annotated[str, typer.Option("--duration", "-d", help="Duration (e.g. 30s, 5m)")] = "30s",
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L", help="Label key=value")] = None,
    system_wide: Annotated[bool, typer.Option("--system-wide", help="System-wide profiling")] = False,
    db_path: Annotated[Optional[str], typer.Option("--db", help="Database path")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
):
    """Collect Top-Down analysis data for a process."""
    from topdown.collector.toplev import ToplevRunner, ToplevOptions, check_toplev_available, check_perf_permissions
    from topdown.collector.process_resolver import resolve_pids

    duration_secs = parse_duration(duration)
    user_labels = parse_label_args(label)
    config = get_config(db_path)

    # Pre-flight checks
    if not check_toplev_available(config.toplev_path):
        console.print(f"[red]Error:[/red] toplev not found at '{config.toplev_path}'")
        console.print("Install: pip install pmu-tools  or  export TOPDOWN_TOPLEV_PATH=/path/to/toplev.py")
        raise typer.Exit(1)

    ok, msg = check_perf_permissions()
    if not ok:
        console.print(f"[red]Error:[/red] {msg}")
        raise typer.Exit(1)

    # Resolve PIDs
    if not system_wide:
        pids = resolve_pids(process)
        if not pids:
            console.print(f"[red]Error:[/red] No process found matching '{process}'")
            raise typer.Exit(1)
        console.print(f"Found {len(pids)} PID(s) for '{process}': {pids}")
    else:
        pids = []

    # Collect auto labels
    auto_labels = collect_auto_labels(process, pids, level, config.toplev_path)
    all_labels = merge_labels(auto_labels, user_labels)

    # Create run record
    run = Run(
        process_name=process,
        level=level,
        system_wide=system_wide,
        labels=all_labels,
    )

    # Run toplev
    options = ToplevOptions(
        level=level,
        pids=pids if pids else None,
        system_wide=system_wide,
    )
    runner = ToplevRunner(config.toplev_path, options)

    console.print(f"Collecting level {level} data for {duration}...")
    start_time = time.time()

    try:
        toplev_samples = runner.run_and_parse(duration_secs)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    elapsed = time.time() - start_time
    run.ended_at = datetime.now(timezone.utc)
    run.duration_seconds = elapsed

    if not toplev_samples:
        console.print("[yellow]Warning:[/yellow] No samples collected. Check toplev output.")
        raise typer.Exit(1)

    # Store results
    backend = get_backend(config)
    try:
        backend.insert_run(run)

        samples = [
            Sample(
                run_id=run.run_id,
                timestamp=s.timestamp or 0.0,
                cpu=s.cpu,
                metric_name=s.metric_name,
                value=s.value,
                unit=s.unit,
                status=s.status,
            )
            for s in toplev_samples
        ]
        count = backend.insert_samples(samples)
        backend.update_run(run.run_id, run.ended_at, run.duration_seconds)

        if json_output:
            import json

            result = {
                "run_id": run.run_id,
                "samples": count,
                "duration": elapsed,
                "labels": all_labels,
            }
            console.print_json(json.dumps(result))
        else:
            console.print(f"[green]Done.[/green] Run ID: {run.run_id}")
            console.print(f"  Samples: {count} | Duration: {elapsed:.1f}s")
            console.print(f"  Labels: {len(all_labels)} ({len(user_labels)} user-supplied)")

    finally:
        backend.close()


@app.command(name="list")
def list_runs(
    process: Annotated[Optional[str], typer.Option("--process", "-p", help="Filter by process name")] = None,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L", help="Filter by label key=value")] = None,
    last: Annotated[str, typer.Option("--last", help="Time window (e.g. 24h, 7d)")] = "24h",
    limit: Annotated[int, typer.Option("--limit", help="Max runs to show")] = 20,
    db_path: Annotated[Optional[str], typer.Option("--db", help="Database path")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
):
    """List recent profiling runs."""
    last_hours = parse_time_window(last)
    filter_labels = parse_label_args(label)
    config = get_config(db_path)
    backend = get_backend(config)

    try:
        runs = backend.list_runs(
            process_name=process,
            labels=filter_labels if filter_labels else None,
            last_hours=last_hours,
            limit=limit,
        )

        if not runs:
            console.print("No runs found.")
            return

        if json_output:
            import json

            data = [
                {
                    "run_id": r.run_id,
                    "started_at": r.started_at.isoformat(),
                    "process_name": r.process_name,
                    "level": r.level,
                    "duration": r.duration_seconds,
                    "labels": r.labels,
                }
                for r in runs
            ]
            console.print_json(json.dumps(data))
            return

        table = Table(title=f"Recent Runs (last {last})")
        table.add_column("Run ID", style="cyan", max_width=12)
        table.add_column("Started", style="green")
        table.add_column("Process", style="yellow")
        table.add_column("Level")
        table.add_column("Duration")
        table.add_column("Key Labels", max_width=50)

        for r in runs:
            # Show a subset of interesting labels
            interesting = {
                k: v
                for k, v in r.labels.items()
                if k in ("git_branch", "git_hash", "test_name", "topology", "build_variant", "client_tool")
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

    finally:
        backend.close()


@app.command()
def query(
    process: Annotated[Optional[str], typer.Option("--process", "-p", help="Filter by process name")] = None,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L", help="Filter by label key=value")] = None,
    last: Annotated[str, typer.Option("--last", help="Time window (e.g. 24h, 7d)")] = "24h",
    run_id: Annotated[Optional[str], typer.Option("--run-id", "-r", help="Specific run ID")] = None,
    bottlenecks: Annotated[bool, typer.Option("--bottlenecks", "-b", help="Show top bottlenecks")] = False,
    tree: Annotated[bool, typer.Option("--tree", "-t", help="Show full TMA tree")] = False,
    funnel: Annotated[bool, typer.Option("--funnel", "-f", help="VTune-style pipeline slot funnel")] = False,
    bottleneck: Annotated[Optional[str], typer.Option("--bottleneck", "-B", help="Find runs with this bottleneck")] = None,
    min_pct: Annotated[float, typer.Option("--min-pct", help="Minimum percentage threshold")] = 5.0,
    top_n: Annotated[int, typer.Option("--top-n", help="Number of bottlenecks to show")] = 10,
    level: Annotated[Optional[int], typer.Option("--level", "-l", help="Max TMA level for tree/funnel")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db", help="Database path")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    csv_output: Annotated[bool, typer.Option("--csv", help="Output as CSV")] = False,
):
    """Query stored Top-Down analysis data."""
    from topdown.analysis.topdown_tree import build_tree
    from topdown.analysis.bottleneck import find_bottlenecks, find_deepest_bottlenecks
    from topdown.analysis.funnel import build_funnel
    from topdown.output.terminal import print_bottlenecks, print_tree, print_funnel, print_by_bottleneck_results
    from topdown.output.export import export_json, export_csv

    filter_labels = parse_label_args(label)
    config = get_config(db_path)
    backend = get_backend(config)

    try:
        # Mode: find runs matching a specific bottleneck
        if bottleneck:
            results = backend.query_by_bottleneck(
                metric_name=bottleneck,
                min_pct=min_pct,
                labels=filter_labels if filter_labels else None,
                last_hours=parse_time_window(last),
            )
            if not results:
                console.print(f"No runs found where {bottleneck} >= {min_pct}%")
                return
            if json_output:
                console.print_json(export_json(results))
            elif csv_output:
                console.print(export_csv(results))
            else:
                print_by_bottleneck_results(results)
            return

        # For tree/bottleneck/funnel modes, we need a specific run
        if run_id:
            run = backend.get_run(run_id)
            if not run:
                console.print(f"[red]Error:[/red] Run '{run_id}' not found")
                raise typer.Exit(1)
        else:
            # Get most recent matching run
            runs = backend.list_runs(
                process_name=process,
                labels=filter_labels if filter_labels else None,
                last_hours=parse_time_window(last),
                limit=1,
            )
            if not runs:
                console.print("No runs found matching filters.")
                return
            run = runs[0]

        metrics = backend.get_aggregated_metrics(run.run_id)
        if not metrics:
            console.print(f"No metrics found for run {run.run_id[:12]}")
            return

        console.print(f"[dim]Run: {run.run_id[:12]} | {run.process_name} | L{run.level} | {run.started_at.strftime('%Y-%m-%d %H:%M')}[/dim]")

        if funnel:
            result = build_funnel(metrics, max_level=level or 3)
            if json_output:
                console.print_json(export_json(result.to_dict()))
            else:
                print_funnel(result)

        elif tree:
            tree_root = build_tree(metrics)
            if json_output:
                console.print_json(export_json(tree_root.to_dict()))
            else:
                print_tree(tree_root, title=f"TMA Tree - {run.process_name}")

        elif bottlenecks:
            found = find_bottlenecks(metrics, top_n=top_n, min_percentage=min_pct, max_level=level)
            if json_output:
                console.print_json(export_json([b.to_dict() for b in found]))
            elif csv_output:
                console.print(export_csv([b.to_dict() for b in found]))
            else:
                print_bottlenecks(found)

        else:
            # Default: show deepest bottlenecks (most actionable)
            found = find_deepest_bottlenecks(metrics, top_n=top_n, min_percentage=min_pct)
            if json_output:
                console.print_json(export_json([b.to_dict() for b in found]))
            else:
                print_bottlenecks(found, title="Deepest Bottlenecks")

    finally:
        backend.close()


@app.command()
def compare(
    run_a: Annotated[Optional[str], typer.Argument(help="First run ID (baseline)")] = None,
    run_b: Annotated[Optional[str], typer.Argument(help="Second run ID (comparison)")] = None,
    label_a: Annotated[Optional[list[str]], typer.Option("--label-a", help="Labels for baseline run")] = None,
    label_b: Annotated[Optional[list[str]], typer.Option("--label-b", help="Labels for comparison run")] = None,
    process: Annotated[Optional[str], typer.Option("--process", "-p", help="Process name filter")] = None,
    threshold: Annotated[float, typer.Option("--threshold", help="Min delta to show")] = 1.0,
    db_path: Annotated[Optional[str], typer.Option("--db", help="Database path")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
):
    """Compare two profiling runs and show deltas."""
    from topdown.analysis.compare import compare_runs as do_compare
    from topdown.output.terminal import print_comparison
    from topdown.output.export import export_json

    config = get_config(db_path)
    backend = get_backend(config)

    try:
        # Resolve run A
        if run_a:
            found_a = backend.get_run(run_a)
            if not found_a:
                console.print(f"[red]Error:[/red] Run '{run_a}' not found")
                raise typer.Exit(1)
        elif label_a:
            labels = parse_label_args(label_a)
            runs = backend.list_runs(process_name=process, labels=labels, limit=1)
            if not runs:
                console.print("[red]Error:[/red] No run found matching --label-a")
                raise typer.Exit(1)
            found_a = runs[0]
        else:
            console.print("[red]Error:[/red] Provide run IDs or --label-a / --label-b")
            raise typer.Exit(1)

        # Resolve run B
        if run_b:
            found_b = backend.get_run(run_b)
            if not found_b:
                console.print(f"[red]Error:[/red] Run '{run_b}' not found")
                raise typer.Exit(1)
        elif label_b:
            labels = parse_label_args(label_b)
            runs = backend.list_runs(process_name=process, labels=labels, limit=1)
            if not runs:
                console.print("[red]Error:[/red] No run found matching --label-b")
                raise typer.Exit(1)
            found_b = runs[0]
        else:
            console.print("[red]Error:[/red] Provide run IDs or --label-a / --label-b")
            raise typer.Exit(1)

        metrics_a = backend.get_aggregated_metrics(found_a.run_id)
        metrics_b = backend.get_aggregated_metrics(found_b.run_id)

        deltas = do_compare(metrics_a, metrics_b, threshold=threshold)

        if not deltas:
            console.print("No significant differences found.")
            return

        if json_output:
            console.print_json(export_json([d.to_dict() for d in deltas]))
        else:
            label_str_a = found_a.run_id[:12]
            label_str_b = found_b.run_id[:12]
            print_comparison(deltas, label_a=label_str_a, label_b=label_str_b)

    finally:
        backend.close()


@app.command()
def explain(
    metric: Annotated[str, typer.Argument(help="Metric name (e.g. 'Backend_Bound.Memory_Bound' or 'DRAM_Bound')")],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
):
    """Explain a TMA metric with causes and tuning hints."""
    from topdown.knowledge.metrics import get_metric_info
    from topdown.output.terminal import print_metric_explanation
    from topdown.output.export import export_json

    info = get_metric_info(metric)
    if not info:
        console.print(f"[red]Error:[/red] Unknown metric '{metric}'")
        console.print("Use a full path like 'Backend_Bound.Memory_Bound' or a leaf name like 'DRAM_Bound'")
        raise typer.Exit(1)

    if json_output:
        console.print_json(export_json({"metric": metric, **info}))
    else:
        print_metric_explanation(metric, info)


@app.command()
def agent(
    process: Annotated[str, typer.Option("--process", "-p", help="Process name to profile")],
    level: Annotated[int, typer.Option("--level", "-l", help="TMA level (1-6)")] = 2,
    every: Annotated[str, typer.Option("--every", "-e", help="Collection interval (e.g. 5m, 1h)")] = "5m",
    duration: Annotated[str, typer.Option("--duration", "-d", help="Per-collection duration")] = "30s",
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L", help="Label key=value")] = None,
    db_path: Annotated[Optional[str], typer.Option("--db", help="Database path")] = None,
):
    """Run as continuous collection agent (daemon mode)."""
    from topdown.service.agent import CollectionAgent
    from topdown.collector.toplev import check_toplev_available, check_perf_permissions

    config = get_config(db_path)

    if not check_toplev_available(config.toplev_path):
        console.print(f"[red]Error:[/red] toplev not found at '{config.toplev_path}'")
        raise typer.Exit(1)

    ok, msg = check_perf_permissions()
    if not ok:
        console.print(f"[red]Error:[/red] {msg}")
        raise typer.Exit(1)

    interval_secs = parse_duration(every)
    duration_secs = parse_duration(duration)
    user_labels = parse_label_args(label)

    console.print(f"Starting agent: process={process} level={level} every={every} duration={duration}")
    console.print("Press Ctrl+C to stop.\n")

    agent_instance = CollectionAgent(
        process_name=process,
        level=level,
        interval_seconds=interval_secs,
        duration_seconds=duration_secs,
        config=config,
        custom_labels=user_labels,
    )
    agent_instance.run()


@app.command(name="install-service")
def install_service(
    process: Annotated[str, typer.Option("--process", "-p", help="Process name")],
    level: Annotated[int, typer.Option("--level", "-l", help="TMA level")] = 2,
    every: Annotated[str, typer.Option("--every", "-e", help="Collection interval")] = "5m",
    duration: Annotated[str, typer.Option("--duration", "-d", help="Per-collection duration")] = "30s",
    service_name: Annotated[str, typer.Option("--service-name", help="Systemd service name")] = "topdown-agent",
    preview: Annotated[bool, typer.Option("--preview", help="Show unit file without installing")] = False,
):
    """Generate and install a systemd unit file for continuous collection."""
    from topdown.service.systemd import generate_unit_file, install_service as do_install

    unit_content = generate_unit_file(
        process_name=process,
        level=level,
        every=every,
        duration=duration,
        service_name=service_name,
    )

    if preview:
        console.print(unit_content)
        return

    try:
        path = do_install(unit_content, service_name=service_name)
        console.print(f"[green]Installed:[/green] {path}")
        console.print(f"  Start: sudo systemctl start {service_name}")
        console.print(f"  Logs:  journalctl -u {service_name} -f")
    except PermissionError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command(name="mcp-serve")
def mcp_serve(
    transport: Annotated[str, typer.Option("--transport", help="Transport: stdio or http")] = "stdio",
    host: Annotated[str, typer.Option("--host", help="HTTP host")] = "localhost",
    port: Annotated[int, typer.Option("--port", help="HTTP port")] = 8000,
):
    """Start MCP server for AI-assisted querying."""
    from topdown.mcp_server import run_server
    run_server(transport=transport, host=host, port=port)


@app.command()
def version():
    """Show version."""
    console.print(f"topdown-profiler {__version__}")


def app_main():
    """Entry point for pyproject.toml scripts."""
    app()


if __name__ == "__main__":
    app_main()
