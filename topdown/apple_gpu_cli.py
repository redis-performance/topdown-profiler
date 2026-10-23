"""Capture and inspect native Apple GPU replay profiles."""

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from topdown.collector.apple import AppleProfileError

app = typer.Typer(help="Capture Metal controls, profile .gputrace replays, and report native GPU limiters (M3/A17 Pro+).")


def _capture(output, store, label, db, collector):
    from topdown.apple_cli import _check_storage_options, _store_capture

    try:
        _check_storage_options(store, label, db)
        result = collector()
        run_id = _store_capture(output, label, db) if store else None
    except KeyboardInterrupt as exc:
        typer.echo(f"GPU collection cancelled; artifacts retained at {output}", err=True)
        raise typer.Exit(130) from exc
    except (AppleProfileError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({"profile": str(output / "profile.json"), "device": result["device"],
                           "encoder_count": len(result["encoders"]), "timeline": result["timeline"],
                           **({"run_id": run_id} if run_id else {})}, indent=2))


@app.command()
def record(
    output: Annotated[Path, typer.Option(help="New directory for capture, native counters and logs")],
    mode: Annotated[str, typer.Option(help="compute, alu, memory or stream Metal control")] = "compute",
    passes: Annotated[int, typer.Option(min=1, max=500)] = 4,
    items: Annotated[int, typer.Option(min=1, max=16777216)] = 16384,
    execution: Annotated[str, typer.Option(help="overlapping or serial replay")] = "overlapping",
    gpu_state: Annotated[str, typer.Option(help="Replay state: default, low, medium or high")] = "default",
    store: Annotated[bool, typer.Option(help="Store native GPU results in the configured database")] = False,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L")] = None,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path")] = None,
):
    """Compile, capture and replay a checksum-verified Metal control on Apple silicon with Xcode."""
    from topdown.collector.apple_gpu import record as collect

    _capture(output, store, label, db, lambda: collect(output, mode, passes, items, execution, gpu_state))


@app.command()
def profile(
    trace: Annotated[Path, typer.Argument(exists=True, help="Existing Metal .gputrace capture")],
    output: Annotated[Path, typer.Option(help="New directory for replay counters and logs")],
    execution: Annotated[str, typer.Option(help="overlapping or serial replay")] = "overlapping",
    gpu_state: Annotated[str, typer.Option(help="Replay state: default, low, medium or high")] = "default",
    store: Annotated[bool, typer.Option(help="Store native GPU results in the configured database")] = False,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L")] = None,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path")] = None,
):
    """Replay an existing workload capture and collect its native per-encoder counters."""
    from topdown.collector.apple_gpu import collect

    _capture(output, store, label, db, lambda: collect(trace, output, execution, gpu_state))


@app.command()
def report(
    profile: Annotated[Optional[Path], typer.Argument(help="GPU profile.json or capture directory")] = None,
    run_id: Annotated[Optional[str], typer.Option(help="Stored Apple GPU run ID")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 20,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    db: Annotated[Optional[str], typer.Option(help="SQLite database path")] = None,
):
    """Rank GPU encoder costs and native overlapping limiters; works offline on any OS."""
    from topdown.apple_gpu_report import render, summarize

    try:
        if (profile is None) == (run_id is None) or (db is not None and run_id is None):
            raise ValueError("Choose a local GPU profile or --run-id; --db requires --run-id")
        if profile is not None:
            from topdown.collector.apple_files import decode_json_object, read_regular_bytes

            path = profile / "profile.json" if profile.is_dir() else profile
            result = summarize(decode_json_object(read_regular_bytes(path)), limit, offset)
        else:
            from topdown.apple_storage import stored_apple_gpu_report
            from topdown.config import get_config
            from topdown.storage import get_backend

            backend = get_backend(get_config(db))
            try:
                result = stored_apple_gpu_report(backend, run_id, limit, offset)
            finally:
                backend.close()
    except (AppleProfileError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2) if as_json else render(result))
