"""CLI for native Apple profiles whose units differ from Linux slot TMA."""

import json
from pathlib import Path
import signal
import threading
from typing import Annotated, Optional

import typer

from topdown.collector.apple import AppleProfileError, doctor, export_profile, record_profile
from topdown.apple_gpu_cli import app as gpu_app

app = typer.Typer(help="Count, capture, export and report native Apple silicon profiles; slot TMA remains unavailable.",
                  no_args_is_help=True)

app.add_typer(gpu_app, name="gpu")


@app.callback()
def _interrupt_on_termination(ctx: typer.Context):
    if threading.current_thread() is not threading.main_thread():
        return
    terminated = False
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        nonlocal terminated
        if not terminated:
            terminated = True
            raise KeyboardInterrupt

    def restore():
        signal.signal(signal.SIGTERM, previous)
        if terminated:
            raise typer.Exit(128 + signal.SIGTERM)

    ctx.call_on_close(restore)
    signal.signal(signal.SIGTERM, terminate)


def _store_capture(output, labels, db, process_name=None):
    from topdown.apple_storage import store_apple_result
    from topdown.collector.labels import parse_label_args
    from topdown.config import get_config
    from topdown.storage import get_backend

    try:
        parsed = parse_label_args(labels)
        backend = get_backend(get_config(db))
        try:
            return store_apple_result(backend, output, process_name, parsed).run_id
        finally:
            backend.close()
    except Exception as exc:
        raise AppleProfileError(f"Could not store Apple result; artifacts retained at {output}: {exc}") from exc


def _check_storage_options(store, label, db):
    if not store and (label or db):
        raise AppleProfileError("Use --store with --label or --db when capturing Apple results")
    from topdown.collector.labels import parse_label_args
    from topdown.apple_storage import validate_run_options

    validate_run_options(None, parse_label_args(label))


def _profile_summary(profile, output):
    count = "interval_count" if "interval_count" in profile else "sample_count"
    result = {"profile": str(output / "profile.json"), count: profile[count],
              "measurement_kind": profile["measurement_kind"], "tma": profile["tma"]}
    if count == "interval_count":
        result.update(gap_count=profile["gap_count"], coverage_complete=profile["coverage_complete"])
        if "scheduling_available" in profile:
            result["scheduling_available"] = profile["scheduling_available"]
    return result


@app.command(name="soc")
def soc_context(
    output: Annotated[Path, typer.Option(help="New directory for native firmware observations and snapshots")],
    duration: Annotated[float, typer.Option(help="Sampling interval in seconds; system/cluster scope", min=0.001, max=300)] = 1,
    store: Annotated[bool, typer.Option(help="Store the native result in the configured database")] = False,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L", help="Stored run label key=value")] = None,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path")] = None,
):
    """Read native cluster stall/bandwidth state distributions; all processes, no PID attribution."""
    from topdown.collector.apple_soc import sample_soc

    try:
        _check_storage_options(store, label, db)
        result = sample_soc(output, duration)
        run_id = _store_capture(output, label, db) if store and result["exit_code"] == 0 else None
    except (AppleProfileError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({**result, **({"run_id": run_id} if run_id else {})}, indent=2))
    raise typer.Exit(result["exit_code"])


@app.command(name="stat", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def stat_target(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="New directory for stat.json and target output")],
    timeout: Annotated[float, typer.Option(help="Seconds before stopping a launched target", min=0.001)] = 30,
    pid: Annotated[Optional[int], typer.Option(help="Observe an existing PID instead of launching a target", min=1)] = None,
    duration: Annotated[float, typer.Option(help="Observation interval with --pid, in seconds", min=0.001)] = 5,
    store: Annotated[bool, typer.Option(help="Store the native result in the configured database")] = False,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L", help="Stored run label key=value")] = None,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path")] = None,
):
    """Count instructions/cycles without root: stat --output run -- /path/program args, or --pid PID."""
    from topdown.collector.apple_stat import stat_pid, stat_process

    command = list(ctx.args)
    if command and command[0] == "--":
        command.pop(0)
    try:
        _check_storage_options(store, label, db)
        if pid is not None:
            if command:
                raise AppleProfileError("Choose either --pid or a command after '--'")
            if ctx.get_parameter_source("timeout").name == "COMMANDLINE":
                raise AppleProfileError("Use --duration with --pid; --timeout is for launched commands")
            result = stat_pid(output, pid, duration)
        else:
            if ctx.get_parameter_source("duration").name == "COMMANDLINE":
                raise AppleProfileError("Use --timeout for launched commands; --duration requires --pid")
            result = stat_process(output, command, timeout)
        run_id = _store_capture(output, label, db) if store and result["exit_code"] == 0 else None
    except (AppleProfileError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({**result, **({"run_id": run_id} if run_id else {})}, indent=2))
    raise typer.Exit(result["exit_code"])


@app.command(name="probe")
def probe_host(
    output: Annotated[Path, typer.Option(help="New directory for probe result and all raw artifacts")],
    mode: Annotated[str, typer.Option(help="counters, fixed, m1, m1-map, m1-fetch, cpu or time")] = "counters",
):
    """Compile/run a bounded probe and emit JSON with a stable status and exit code."""
    from topdown.collector.apple_probe import probe

    result = probe(output, mode)
    typer.echo(json.dumps(result, indent=2))
    raise typer.Exit(result["exit_code"])


@app.command(name="doctor")
def check():
    """Check installed tools without recording or changing host settings."""
    result = doctor()
    typer.echo(json.dumps(result, indent=2))
    if result.get("error") or not result.get("xctrace"):
        raise typer.Exit(1)


@app.command(name="report")
def report_hotspots(
    profile: Annotated[Optional[Path], typer.Argument(exists=True, help="Apple capture directory or exported profile.json")] = None,
    limit: Annotated[int, typer.Option(min=1, help="Maximum number of functions to display")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Emit a structured hotspot report")] = False,
    run_id: Annotated[Optional[str], typer.Option(help="Read a stored Apple run instead of a local file")] = None,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path for --run-id")] = None,
):
    """Rank function hotspots by recorded stack-sample frequency."""
    from topdown.apple_report import load_hotspots, print_hotspots

    try:
        if (profile is None) == (run_id is None) or (db and run_id is None):
            raise AppleProfileError("Choose a local profile or --run-id; --db requires --run-id")
        if profile is not None:
            report = load_hotspots(profile, limit)
        else:
            from topdown.apple_storage import stored_apple_hotspots
            from topdown.config import get_config
            from topdown.storage import get_backend

            backend = get_backend(get_config(db))
            try:
                report = stored_apple_hotspots(backend, run_id, limit)
            finally:
                backend.close()
    except AppleProfileError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        print_hotspots(report)


@app.command(name="store")
def store_result(
    path: Annotated[Path, typer.Argument(exists=True, help="Native Apple JSON file or capture directory")],
    process: Annotated[Optional[str], typer.Option(help="Process name for the stored run")] = None,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L", help="Stored run label key=value")] = None,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path")] = None,
):
    """Import native Apple results into SQLite or the configured PostgreSQL backend."""
    try:
        run_id = _store_capture(path, label, db, process)
    except AppleProfileError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({"run_id": run_id, "collector": "apple"}, indent=2))


@app.command(name="query")
def query_result(
    run_id: Annotated[str, typer.Argument(help="Stored Apple run ID")],
    section: Annotated[str, typer.Option(help="summary, samples, intervals, gaps, channels, leaf_sample_counts or encoders")] = "summary",
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path")] = None,
):
    """Read native Apple results without Linux TMA aggregation."""
    from topdown.apple_storage import query_apple_result
    from topdown.config import get_config
    from topdown.storage import get_backend

    backend = get_backend(get_config(db))
    try:
        result = query_apple_result(backend, run_id, section, offset, limit)
    except AppleProfileError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        backend.close()
    typer.echo(json.dumps(result, indent=2))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def record(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="New directory for raw trace, logs, metadata and profile.json")],
    mode: Annotated[str, typer.Option(help="counters (PMU), fixed (fixed PMU + stacks), m1/m1-map/m1-fetch (experimental events), trace (hardware trace), cpu, time")] = "counters",
    duration: Annotated[int, typer.Option(help="Maximum recording duration in seconds", min=1)] = 5,
    pid: Annotated[Optional[int], typer.Option(help="Override target PID from trace metadata", min=1)] = None,
    start_ns: Annotated[int, typer.Option(help="Inclusive trace-relative start, nanoseconds", min=0)] = 0,
    end_ns: Annotated[Optional[int], typer.Option(help="Exclusive trace-relative end, nanoseconds", min=1)] = None,
    store: Annotated[bool, typer.Option(help="Store the native result in the configured database")] = False,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L", help="Stored run label key=value")] = None,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path")] = None,
):
    """Record one launched program: topdown apple record --output run -- /path/program args."""
    command = list(ctx.args)
    if command and command[0] == "--":
        command.pop(0)
    try:
        _check_storage_options(store, label, db)
        profile = record_profile(output.resolve(), command, duration, mode, pid, start_ns, end_ns)
        run_id = _store_capture(output, label, db) if store else None
    except (AppleProfileError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({**_profile_summary(profile, output), **({"run_id": run_id} if run_id else {})}, indent=2))


@app.command(name="export")
def export_trace(
    trace: Annotated[Path, typer.Argument(exists=True, help="Existing Instruments .trace bundle")],
    output: Annotated[Path, typer.Option(help="New export directory")],
    mode: Annotated[str, typer.Option(help="counters, fixed, m1, m1-map, m1-fetch, trace, cpu or time")] = "counters",
    pid: Annotated[Optional[int], typer.Option(help="Only this PID; otherwise use trace target", min=1)] = None,
    run: Annotated[int, typer.Option(help="Trace run number", min=1)] = 1,
    start_ns: Annotated[int, typer.Option(min=0)] = 0,
    end_ns: Annotated[Optional[int], typer.Option(min=1)] = None,
):
    """Export native events/stacks; missing counters never fall back to timer samples."""
    if output.exists():
        typer.echo("Error: Choose a new export directory", err=True)
        raise typer.Exit(1)
    try:
        profile = export_profile(trace, output.resolve(), mode, pid, run, start_ns, end_ns)
    except (AppleProfileError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(_profile_summary(profile, output), indent=2))
