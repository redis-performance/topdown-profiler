"""Optional cross-platform sampling commands with native Stax units."""

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from topdown.apple_cli import _interrupt_on_termination
from topdown.collector.apple import AppleProfileError
from topdown.collector.labels import parse_label_args
from topdown.config import get_config
from topdown.storage import get_backend

app = typer.Typer(help="Optional Stax CPU samples and off-CPU waits on macOS and Linux.", no_args_is_help=True)
app.callback()(_interrupt_on_termination)


def _emit(function, *args, **kwargs):
    try:
        result = function(*args, **kwargs)
        typer.echo(json.dumps(result, indent=2, allow_nan=False))
    except (AppleProfileError, OSError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _query(function, db, *args, **kwargs):
    backend = get_backend(get_config(db))
    try:
        return function(backend, *args, **kwargs)
    finally:
        backend.close()


@app.command()
def doctor():
    """Inspect optional executables and permission hints without installing anything."""
    from topdown.collector.stax import doctor as inspect

    _emit(inspect)


@app.command(name="import")
def import_result(
    path: Path,
    process_name: Annotated[Optional[str], typer.Option("--process-name")] = None,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L")] = None,
    db: Annotated[Optional[str], typer.Option("--db")] = None,
):
    """Import a server-local v3 .stax bundle or directory into the database."""
    from topdown.stax_storage import query_stax_result, store_stax_result

    def store(backend):
        run = store_stax_result(backend, path, process_name, parse_label_args(label))
        return query_stax_result(backend, run.run_id)

    _emit(_query, store, db)


@app.command()
def record(
    output: Annotated[Path, typer.Option("--output", "-o")],
    pid: Annotated[Optional[int], typer.Option("--pid")] = None,
    command: Annotated[
        Optional[list[str]], typer.Argument(help="Executable argv after --; owned target stops after recording")
    ] = None,
    duration: Annotated[int, typer.Option("--duration", "-d")] = 5,
    frequency: Annotated[int, typer.Option("--frequency", "-F")] = 99,
    store: Annotated[bool, typer.Option("--store")] = False,
    process_name: Annotated[Optional[str], typer.Option("--process-name")] = None,
    label: Annotated[Optional[list[str]], typer.Option("--label", "-L")] = None,
    db: Annotated[Optional[str], typer.Option("--db")] = None,
):
    """Attach to a PID or launch a command with a private Stax server; retain logs and archive."""
    from topdown.apple_storage import validate_run_options
    from topdown.collector.stax import record_profile
    from topdown.stax_archive import StaxError
    from topdown.stax_storage import query_stax_result, store_stax_result

    def capture():
        if not store and (label or db or process_name):
            raise StaxError("Use --store with --label, --db or --process-name")
        labels = parse_label_args(label)
        validate_run_options(process_name, labels)
        path, metadata = record_profile(
            pid=pid, command=command, output=output, duration_seconds=duration, frequency_hz=frequency
        )
        if not store:
            return {"archive": str(path), "capture": metadata}

        def save(backend):
            run = store_stax_result(backend, path, process_name, labels, metadata)
            return query_stax_result(backend, run.run_id)

        try:
            return _query(save, db)
        except Exception as exc:
            raise StaxError(f"Could not store Stax capture; artifacts retained at {output}: {exc}") from exc

    _emit(capture)


@app.command()
def query(
    run_id: str,
    section: Annotated[str, typer.Option("--section")] = "summary",
    offset: Annotated[int, typer.Option("--offset")] = 0,
    limit: Annotated[int, typer.Option("--limit")] = 100,
    db: Annotated[Optional[str], typer.Option("--db")] = None,
):
    """Read a stored sampling summary or one bounded page of native rows."""
    from topdown.stax_storage import query_stax_result

    _emit(_query, query_stax_result, db, run_id, section, offset, limit)


@app.command()
def hotspots(
    run_id: str,
    tid: Annotated[Optional[int], typer.Option("--tid")] = None,
    stack: Annotated[str, typer.Option("--stack")] = "user",
    sort: Annotated[str, typer.Option("--sort")] = "self",
    limit: Annotated[int, typer.Option("--limit")] = 20,
    db: Annotated[Optional[str], typer.Option("--db")] = None,
):
    """Rank archived user/kernel functions by self or inclusive stack sample counts."""
    from topdown.stax_storage import get_stax_hotspots

    _emit(_query, get_stax_hotspots, db, run_id, tid, stack, sort, limit)


@app.command()
def waits(
    run_id: str,
    tid: Annotated[Optional[int], typer.Option("--tid")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    db: Annotated[Optional[str], typer.Option("--db")] = None,
):
    """Group off-CPU interval durations by thread, stack and waking thread."""
    from topdown.stax_storage import get_stax_thread_waits

    _emit(_query, get_stax_thread_waits, db, run_id, tid, limit)


@app.command()
def targets(
    run_id: str,
    tid: Annotated[Optional[int], typer.Option("--tid")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    db: Annotated[Optional[str], typer.Option("--db")] = None,
):
    """Rank cooperating executor/Metal target spans by reported duration, separately from CPU samples."""
    from topdown.stax_storage import get_stax_target_spans

    _emit(_query, get_stax_target_spans, db, run_id, tid, limit)
