"""CLI entry point for topdown-profiler."""

import logging
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
def version():
    """Show version."""
    console.print(f"topdown-profiler {__version__}")


def app_main():
    """Entry point for pyproject.toml scripts."""
    app()


if __name__ == "__main__":
    app_main()
