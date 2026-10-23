"""MCP (Model Context Protocol) server for topdown-profiler.

Exposes TMA collection, querying, and analysis as MCP tools and resources
for AI-assisted CPU and GPU performance analysis.

Auth modes (via TOPDOWN_AUTH_MODE env var):
- "none" (default): no auth, suitable for stdio transport / local use
- "api-key": bearer token auth, use `topdown setup` to generate a key
- "oauth": JWT validation against an external authorization server
"""


from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import StrictFloat, StrictInt

from topdown.config import get_config
from topdown.storage import get_backend

_MCP_INSTRUCTIONS = (
    "You are connected to the topdown-profiler MCP server. "
    "This tool collects, stores, and queries Top-Down Microarchitecture "
    "Analysis (TMA) data using pmu-tools/toplev on Intel or perf stat "
    "--topdown on ARM Neoverse. Use the available tools to profile "
    "processes, query bottlenecks, compare runs, and explain CPU "
    "performance metrics. Apple silicon uses native results: use apple_doctor, "
    "collect_apple_profile or import_apple_result, query_apple_result, and get_apple_hotspots. "
    "Apple runs share the run list and labels, while retaining native units, scope and integer precision. "
    "Apple bottleneck shares and sample-frequency hotspots are separate from Linux slot TMA."
    " For Apple GPU replay use collect_apple_gpu_profile (packaged Metal controls), profile_apple_gpu_trace "
    "(existing server-local .gputrace), query_apple_result(section='encoders'), and get_apple_gpu_report. "
    "Replay requires Apple silicon M3/A17 Pro+ and Xcode. GPU limiters overlap and may exceed 100%; "
    "retain native sampling coverage and compare matched replay settings."
)


def _create_mcp(host: str = "localhost", port: int = 8000) -> FastMCP:
    """Create FastMCP instance, with auth if configured."""
    from topdown.auth import AuthConfig, get_mcp_auth_kwargs

    auth_config = AuthConfig.from_env()
    auth_kwargs = get_mcp_auth_kwargs(auth_config, host=host, port=port)

    return FastMCP(
        "topdown-profiler",
        instructions=_MCP_INSTRUCTIONS,
        host=host,
        port=port,
        **auth_kwargs,
    )


# Default instance (no auth) for tool/resource registration.
# Replaced at server start time if auth is enabled.
mcp = FastMCP("topdown-profiler", instructions=_MCP_INSTRUCTIONS)


def _comparison_mux_note(metrics_a: list[dict], metrics_b: list[dict]) -> str:
    """MUX note for a comparison, using the noisier of the two runs."""
    from topdown.units import lowest_mux, mux_note

    muxes = [m for m in (lowest_mux(metrics_a), lowest_mux(metrics_b)) if m is not None]
    return f"\n\n{mux_note(min(muxes))}" if muxes else ""


def _get_backend():
    config = get_config()
    return get_backend(config)


def _apple_run_guidance(*runs):
    if any(run.labels.get("collector") == "apple" for run in runs):
        report = "get_apple_gpu_report" if any(run.labels.get("processor") == "gpu" for run in runs) else "get_apple_hotspots"
        return (f"This run contains native Apple measurements. Use query_apple_result or {report}; "
                "Linux slot-TMA analysis and comparisons are unavailable for Apple results.")
    return None


def _apple_tool(*, capture=False):
    from topdown.collector.apple_runtime import worker

    def register(function):
        mcp.tool()(worker(function, capture=capture))
        return function
    return register


# ──────────────────────────── Tools ────────────────────────────


@_apple_tool()
def apple_doctor() -> dict:
    """Inspect Apple profiling capabilities on the MCP server host without recording a workload."""
    from topdown.collector.apple import doctor

    return doctor()


@_apple_tool()
def import_apple_result(path: str, process_name: str | None = None, labels: dict[str, str] | None = None) -> dict:
    """Store a native Apple profile.json/stat.json/soc.json file or capture directory from the server host.

    Retains counters as integers and keeps native Apple categories, scopes and trace gaps.
    Each import creates a labeled run, visible in list_profiling_runs. Does not create Linux TMA samples.
    """
    from pathlib import Path
    from topdown.apple_storage import query_apple_result as query, store_apple_result
    from topdown.collector.apple import AppleProfileError

    backend = _get_backend()
    try:
        run = store_apple_result(backend, Path(path), process_name, labels)
        return query(backend, run.run_id)
    except (AppleProfileError, OSError, ValueError) as exc:
        return {"error": str(exc)}
    finally:
        backend.close()


@_apple_tool(capture=True)
def collect_apple_profile(
    mode: str = "stat", command: list[str] | None = None, pid: StrictInt | None = None,
    duration_seconds: StrictFloat = 5, process_name: str | None = None, labels: dict[str, str] | None = None,
) -> dict:
    """Capture and store native Apple results on this MCP server's macOS host.

    stat accepts exactly one executable argv list or existing PID. For a PID, duration is the
    observation interval; for a launched stat command it is the timeout. fixed/counters/cpu/time/trace
    and experimental m1/m1-map/m1-fetch modes launch a command, require Instruments and whole seconds.
    soc accepts no target and observes all processes at system/cluster scope. Duration is bounded to
    300 seconds. Failed captures retain local artifacts and are not automatically stored.
    """
    import uuid
    from pathlib import Path
    from topdown.apple_storage import collect_apple_result, query_apple_result as query, store_apple_result, validate_run_options

    root = Path(get_config().db_path).parent / "apple-captures"
    output = root / str(uuid.uuid4())
    try:
        validate_run_options(process_name, labels)
        root.mkdir(parents=True, exist_ok=True)
        path = collect_apple_result(output, mode, command, pid, duration_seconds)
        from topdown.collector.apple_runtime import check_cancelled

        check_cancelled()
        backend = _get_backend()
        try:
            run = store_apple_result(backend, path, process_name, labels)
            return {**query(backend, run.run_id), "artifacts": str(output)}
        finally:
            backend.close()
    except Exception as exc:
        return {"error": str(exc), "artifacts": str(output)}


@_apple_tool()
def query_apple_result(run_id: str, section: str = "summary", offset: StrictInt = 0, limit: StrictInt = 100) -> dict:
    """Read a stored native Apple result by run ID, on any server OS.

    summary returns native totals/categories, scope and provenance without bulk observations.
    Read samples, intervals, gaps, channels, leaf_sample_counts or GPU encoders in pages (limit 1–1000).
    Native integer precision, GPU counter strings, histogram units and unavailable Intel TMA are retained.
    """
    from topdown.apple_storage import query_apple_result as query
    from topdown.collector.apple import AppleProfileError

    backend = _get_backend()
    try:
        return query(backend, run_id, section, offset, limit)
    except AppleProfileError as exc:
        return {"error": str(exc)}
    finally:
        backend.close()


@_apple_tool()
def get_apple_hotspots(run_id: str, limit: StrictInt = 20) -> dict:
    """Rank stored Apple stack samples by function frequency; shares are sample fractions, not CPU-time/TMA percentages."""
    from topdown.apple_storage import stored_apple_hotspots
    from topdown.collector.apple import AppleProfileError

    backend = _get_backend()
    try:
        return stored_apple_hotspots(backend, run_id, limit)
    except AppleProfileError as exc:
        return {"error": str(exc)}
    finally:
        backend.close()


def _collect_gpu(trace, mode, passes, items, execution, process_name, labels, gpu_state="default"):
    import uuid
    from pathlib import Path
    from topdown.apple_storage import query_apple_result as query, store_apple_result, validate_run_options
    from topdown.collector.apple_gpu import collect, record

    output = Path(get_config().db_path).parent / "apple-gpu-captures" / str(uuid.uuid4())
    try:
        validate_run_options(process_name, labels)
        if trace is None:
            record(output, mode, passes, items, execution, gpu_state)
        else:
            collect(Path(trace), output, execution, gpu_state)
        from topdown.collector.apple_runtime import check_cancelled

        check_cancelled()
        backend = _get_backend()
        try:
            run = store_apple_result(backend, output / "profile.json", process_name, labels)
            return {**query(backend, run.run_id), "artifacts": str(output)}
        finally:
            backend.close()
    except Exception as exc:
        return {"error": str(exc), "artifacts": str(output)}


@_apple_tool(capture=True)
def collect_apple_gpu_profile(
    mode: str = "compute", passes: StrictInt = 4, items: StrictInt = 16384, execution: str = "overlapping",
    process_name: str | None = None, labels: dict[str, str] | None = None,
    gpu_state: Literal["default", "low", "medium", "high"] = "default",
) -> dict:
    """Capture a checksum-verified packaged Metal control, replay it, and store native GPU counters.

    Runs on the MCP server's Apple silicon macOS host with Xcode (replay requires M3/A17 Pro+).
    Modes: compute, alu (reduced loop overhead), memory, stream (64 floats/thread).
    Bounds: passes 1–500, items 1–16777216, product <=268435456; stream items divisible by 64.
    Compilation/capture timeouts are 90/60 seconds; replay budget 300 seconds plus session cleanup.
    Replay execution is overlapping or serial; gpu_state selects default/low/medium/high replay state.
    State selection does not control the original capture. Failed captures retain artifacts.
    """
    return _collect_gpu(None, mode, passes, items, execution, process_name, labels, gpu_state)


@_apple_tool(capture=True)
def profile_apple_gpu_trace(
    path: str, execution: str = "overlapping", process_name: str | None = None, labels: dict[str, str] | None = None,
    gpu_state: Literal["default", "low", "medium", "high"] = "default",
) -> dict:
    """Replay an existing server-local .gputrace and store its native GPU counters (M3/A17 Pro+, Xcode).

    Only the captured Metal workload is replayed; this does not launch or profile an arbitrary process.
    Retains native counters, overlapping limiters and sampling coverage. Replay budget: 300 seconds.
    gpu_state selects default/low/medium/high; native min/max labels remain unchanged in the timeline.
    """
    return _collect_gpu(path, None, None, None, execution, process_name, labels, gpu_state)


@_apple_tool()
def get_apple_gpu_report(run_id: str, limit: StrictInt = 20, offset: StrictInt = 0) -> dict:
    """Rank stored Apple GPU encoders by replay cost with native limiter values and sampling coverage.

    Works on any server OS. Pages up to 1000 encoders without renormalizing their original cost shares.
    New captures include timeline bandwidth, occupancy, active-core and F32 metrics with native units
    and explicit observation availability; their definitions remain in the stored native result.
    Timeline counters can include other GPU activity; sampled cores and consistent-state flags are retained.
    Native limiters overlap and values above 100% are retained and flagged; these are not CPU slot TMA.
    """
    from topdown.apple_storage import stored_apple_gpu_report
    from topdown.collector.apple import AppleProfileError

    backend = _get_backend()
    try:
        return stored_apple_gpu_report(backend, run_id, limit, offset)
    except AppleProfileError as exc:
        return {"error": str(exc)}
    finally:
        backend.close()


@mcp.tool()
def collect_topdown(
    process_name: str,
    level: int = 2,
    duration_seconds: int = 30,
    system_wide: bool = False,
    labels: dict[str, str] | None = None,
) -> str:
    """Collect Top-Down Microarchitecture Analysis data for a process.

    Uses pmu-tools/toplev on Intel or perf stat --topdown on ARM Neoverse.
    Returns a summary of the collection run with top bottlenecks.

    Args:
        process_name: Process name to profile (e.g. 'redis-server')
        level: TMA analysis level 1-6 (default 2, ARM is L1 only)
        duration_seconds: How long to collect data
        system_wide: If true, profile all CPUs system-wide
        labels: Optional dict of labels (e.g. {"git_branch": "unstable", "test_name": "set-get-100"})
    """
    import time
    from datetime import datetime, timezone
    from topdown.collector import make_runner, resolve_collector
    from topdown.collector.toplev import check_toplev_available, check_perf_permissions
    from topdown.collector.process_resolver import resolve_pids
    from topdown.collector.labels import collect_auto_labels, merge_labels
    from topdown.storage.models import Run, Sample
    from topdown.analysis.bottleneck import find_bottlenecks, find_detail_bottlenecks, summarize_bottlenecks
    from topdown.units import lowest_mux

    config = get_config()
    collector = resolve_collector(config)

    if collector == "apple":
        from topdown.collector.apple import APPLE_COLLECTION_MESSAGE

        return APPLE_COLLECTION_MESSAGE + " Use collect_apple_profile to capture and store native Apple results through MCP."

    ok, msg = check_perf_permissions()
    if not ok:
        return f"Error: {msg}"

    if collector == "toplev":
        if not check_toplev_available(config.toplev_path):
            return f"Error: toplev not found at '{config.toplev_path}'. Install pmu-tools."
    else:
        from topdown.collector.perf_stat import check_perf_topdown_supported
        ok, msg = check_perf_topdown_supported()
        if not ok:
            return f"Error: {msg}"

    pids = []
    if not system_wide:
        pids = resolve_pids(process_name)
        if not pids:
            return f"Error: No process found matching '{process_name}'"

    auto_labels = collect_auto_labels(
        process_name, pids, level,
        collector=collector,
        toplev_path=config.toplev_path,
    )
    all_labels = merge_labels(auto_labels, labels or {})

    run = Run(process_name=process_name, level=level, system_wide=system_wide, labels=all_labels)
    runner = make_runner(config, pids=pids or None, system_wide=system_wide, level=level)

    start_time = time.time()
    try:
        toplev_samples = runner.run_and_parse(duration_seconds)
    except RuntimeError as e:
        return f"Error running collection: {e}"

    elapsed = time.time() - start_time
    run.ended_at = datetime.now(timezone.utc)
    run.duration_seconds = elapsed

    if not toplev_samples:
        return "Warning: No samples collected. Check toplev configuration."

    backend = _get_backend()
    try:
        backend.insert_run(run)
        samples = [
            Sample(
                run_id=run.run_id, timestamp=s.timestamp or 0.0, cpu=s.cpu,
                metric_name=s.metric_name, value=s.value, unit=s.unit, status=s.status,
            )
            for s in toplev_samples
        ]
        backend.insert_samples(samples)
        backend.update_run(run.run_id, run.ended_at, run.duration_seconds)

        metrics = backend.get_aggregated_metrics(run.run_id)
        summary = summarize_bottlenecks(
            find_bottlenecks(metrics, top_n=5), find_detail_bottlenecks(metrics, top_n=5), lowest_mux(metrics)
        )

        return (
            f"Collection complete.\n"
            f"Run ID: {run.run_id}\n"
            f"Process: {process_name} | Level: {level} | Duration: {elapsed:.1f}s\n"
            f"Samples: {len(samples)}\n"
            f"Labels: {len(all_labels)}\n\n"
            f"{summary}"
        )
    finally:
        backend.close()


@mcp.tool()
def query_bottlenecks(
    process_name: str | None = None,
    labels: dict[str, str] | None = None,
    last_hours: float = 24.0,
    min_percentage: float = 5.0,
    top_n: int = 10,
) -> str:
    """Query stored Top-Down data to find ranked CPU bottlenecks.

    Args:
        process_name: Filter by process name
        labels: Filter by labels (e.g. {"git_branch": "unstable", "test_name": "set-get-100"})
        last_hours: Time window in hours
        min_percentage: Minimum bottleneck percentage to include
        top_n: Maximum number of results
    """
    from topdown.analysis.bottleneck import (
        find_bottlenecks,
        find_detail_bottlenecks,
        summarize_bottlenecks,
    )
    from topdown.units import lowest_mux, mux_note

    backend = _get_backend()
    try:
        runs = backend.list_runs(process_name=process_name, labels=labels, last_hours=last_hours, limit=1)
        if not runs:
            return "No runs found matching filters."

        run = runs[0]
        if guidance := _apple_run_guidance(run):
            return guidance
        metrics = backend.get_aggregated_metrics(run.run_id)
        found = find_bottlenecks(metrics, top_n=top_n, min_percentage=min_percentage)
        details = find_detail_bottlenecks(metrics, top_n=top_n, min_percentage=min_percentage)

        if not found and not details:
            message = f"No bottlenecks above {min_percentage}% found."
            mux = lowest_mux(metrics)
            return message + (f"\n{mux_note(mux)}" if mux is not None else "")

        lines = [
            f"Run: {run.run_id} | {run.process_name} | L{run.level} | {run.started_at.strftime('%Y-%m-%d %H:%M')}",
            f"Labels: {', '.join(f'{k}={v}' for k, v in run.labels.items() if k in ('git_branch', 'test_name', 'topology', 'build_variant'))}",
            "",
            summarize_bottlenecks(found, details, lowest_mux(metrics)),
        ]
        return "\n".join(lines)
    finally:
        backend.close()


@mcp.tool()
def query_by_bottleneck(
    metric_name: str,
    min_pct: float = 5.0,
    labels: dict[str, str] | None = None,
    last_hours: float = 24.0,
) -> str:
    """Find which benchmarks/runs hit a specific TMA bottleneck.

    Returns runs where the specified metric exceeds the threshold, with their labels.

    Args:
        metric_name: TMA node name, matched exactly at any depth (e.g. 'DRAM_Bound',
            'L3_Bound', 'Backend_Bound.Memory_Bound'). If no node has that exact name,
            falls back to a case-insensitive substring match and says so.
        min_pct: Minimum value, in the node's own unit (slots, clocks, stalls...)
        labels: Optional label filters
        last_hours: Time window in hours
    """
    import json

    from topdown.storage.base import DEFAULT_NODE_LIMIT, MATCH_SUBSTRING
    from topdown.units import format_value, node_query_empty_message, node_query_header, truncated_note

    if not metric_name:
        return "metric_name is required (e.g. 'DRAM_Bound')."
    backend = _get_backend()
    try:
        results, match, truncated = backend.find_runs_by_node(
            metric_name, min_pct=min_pct, labels=labels, last_hours=last_hours,
        )
        substring = match == MATCH_SUBSTRING
        if not results:
            return node_query_empty_message(metric_name, min_pct, substring)

        lines = [node_query_header(metric_name, min_pct, substring) + "\n"]
        for r in results:
            run_labels = r.get("labels", "{}")
            if isinstance(run_labels, str):
                run_labels = json.loads(run_labels)
            interesting = {k: v for k, v in run_labels.items()
                          if k in ("git_branch", "git_hash", "test_name", "topology", "build_variant", "client_tool")}
            label_str = ", ".join(f"{k}={v}" for k, v in interesting.items())
            lines.append(
                f"  {r['run_id']} | {r.get('metric_name', '')} "
                f"{format_value(r.get('avg_value', 0), r.get('unit'))} | "
                f"{r.get('process_name', '')} | {label_str}"
            )
        if truncated:
            lines.append("\n" + truncated_note(DEFAULT_NODE_LIMIT))
        return "\n".join(lines)
    finally:
        backend.close()


@mcp.tool()
def get_funnel(
    run_id: str | None = None,
    process_name: str | None = None,
    labels: dict[str, str] | None = None,
    level: int = 3,
) -> str:
    """Get VTune-style pipeline slot funnel showing where 100% of CPU slots go.

    Shows the hierarchical breakdown: what percentage is useful work (Retiring)
    vs wasted in Frontend_Bound, Bad_Speculation, Backend_Bound.

    Args:
        run_id: Specific run ID (or use process_name/labels to find latest)
        process_name: Filter by process name
        labels: Filter by labels
        level: Max TMA level to drill down to (1-6)
    """
    from topdown.analysis.funnel import build_funnel, format_funnel_text

    backend = _get_backend()
    try:
        if run_id:
            run = backend.get_run(run_id)
            if not run:
                return f"Run '{run_id}' not found."
        else:
            runs = backend.list_runs(process_name=process_name, labels=labels, limit=1)
            if not runs:
                return "No runs found matching filters."
            run = runs[0]

        if guidance := _apple_run_guidance(run):
            return guidance
        metrics = backend.get_aggregated_metrics(run.run_id)
        if not metrics:
            return "No metrics found for this run."

        result = build_funnel(metrics, max_level=level)
        header = f"Run: {run.run_id} | {run.process_name} | L{run.level}\n\n"
        return header + format_funnel_text(result)
    finally:
        backend.close()


@mcp.tool()
def compare_funnel(
    run_id_a: str,
    run_id_b: str,
    label_a: str = "Baseline",
    label_b: str = "Comparison",
    level: int = 2,
) -> str:
    """Compare two runs as a side-by-side TMA funnel with deltas.

    Shows the full pipeline slot breakdown for both runs in columns,
    with a delta column highlighting what changed. This is the best
    view for understanding how an optimization shifted CPU behavior.

    Args:
        run_id_a: Baseline run ID
        run_id_b: Comparison run ID
        label_a: Column label for baseline (default "Baseline")
        label_b: Column label for comparison (default "Comparison")
        level: Max TMA level to drill down to (1-6, default 2)
    """
    from topdown.analysis.funnel import build_funnel, format_funnel_comparison

    backend = _get_backend()
    try:
        run_a = backend.get_run(run_id_a)
        run_b = backend.get_run(run_id_b)
        if not run_a:
            return f"Run '{run_id_a}' not found."
        if not run_b:
            return f"Run '{run_id_b}' not found."

        if guidance := _apple_run_guidance(run_a, run_b):
            return guidance
        metrics_a = backend.get_aggregated_metrics(run_a.run_id)
        metrics_b = backend.get_aggregated_metrics(run_b.run_id)

        funnel_a = build_funnel(metrics_a, max_level=level)
        funnel_b = build_funnel(metrics_b, max_level=level)

        header = (
            f"A: {run_a.run_id} | {run_a.labels.get('git_branch', '?')} | "
            f"{run_a.labels.get('test_name', '?')}\n"
            f"B: {run_b.run_id} | {run_b.labels.get('git_branch', '?')} | "
            f"{run_b.labels.get('test_name', '?')}\n\n"
        )
        return header + format_funnel_comparison(funnel_a, funnel_b, label_a, label_b)
    finally:
        backend.close()


@mcp.tool()
def compare_runs(run_id_a: str, run_id_b: str) -> str:
    """Compare two profiling runs and show metric deltas.

    Shows what got better/worse between two runs.

    Args:
        run_id_a: First run ID (baseline)
        run_id_b: Second run ID (comparison)
    """
    from topdown.analysis.compare import compare_runs as do_compare, summarize_comparison

    backend = _get_backend()
    try:
        run_a = backend.get_run(run_id_a)
        run_b = backend.get_run(run_id_b)
        if not run_a:
            return f"Run '{run_id_a}' not found."
        if not run_b:
            return f"Run '{run_id_b}' not found."

        if guidance := _apple_run_guidance(run_a, run_b):
            return guidance
        metrics_a = backend.get_aggregated_metrics(run_a.run_id)
        metrics_b = backend.get_aggregated_metrics(run_b.run_id)

        deltas = do_compare(metrics_a, metrics_b)
        if not deltas:
            return "No significant differences found." + _comparison_mux_note(metrics_a, metrics_b)

        return (
            f"Comparing {run_a.run_id} (baseline) vs {run_b.run_id}\n\n"
            + summarize_comparison(deltas)
            + _comparison_mux_note(metrics_a, metrics_b)
        )
    finally:
        backend.close()


@mcp.tool()
def compare_by_labels(
    label_a: dict[str, str],
    label_b: dict[str, str],
    process_name: str | None = None,
) -> str:
    """Compare the latest runs matching two different label sets.

    Useful for A/B comparisons like release vs debug, or branch A vs branch B.

    Args:
        label_a: Labels for baseline run (e.g. {"build_variant": "release"})
        label_b: Labels for comparison run (e.g. {"build_variant": "debug"})
        process_name: Optional process name filter
    """
    from topdown.analysis.compare import compare_runs as do_compare, summarize_comparison

    backend = _get_backend()
    try:
        runs_a = backend.list_runs(process_name=process_name, labels=label_a, limit=1)
        runs_b = backend.list_runs(process_name=process_name, labels=label_b, limit=1)

        if not runs_a:
            return f"No run found matching labels: {label_a}"
        if not runs_b:
            return f"No run found matching labels: {label_b}"

        run_a, run_b = runs_a[0], runs_b[0]
        if guidance := _apple_run_guidance(run_a, run_b):
            return guidance
        metrics_a = backend.get_aggregated_metrics(run_a.run_id)
        metrics_b = backend.get_aggregated_metrics(run_b.run_id)

        deltas = do_compare(metrics_a, metrics_b)
        if not deltas:
            return "No significant differences found." + _comparison_mux_note(metrics_a, metrics_b)

        label_str_a = ", ".join(f"{k}={v}" for k, v in label_a.items())
        label_str_b = ", ".join(f"{k}={v}" for k, v in label_b.items())
        return (
            f"Comparing [{label_str_a}] vs [{label_str_b}]\n\n"
            + summarize_comparison(deltas)
            + _comparison_mux_note(metrics_a, metrics_b)
        )
    finally:
        backend.close()


@mcp.tool()
def explain_metric(metric_name: str) -> str:
    """Explain a TMA metric: what it measures, typical causes, and tuning hints.

    Works with full paths (e.g. 'Backend_Bound.Memory_Bound.DRAM_Bound')
    or leaf names (e.g. 'DRAM_Bound').

    Args:
        metric_name: TMA metric name to explain
    """
    from topdown.knowledge import get_metric_info
    from topdown.units import MUX_EXPLANATION, is_quality_metric

    if is_quality_metric(metric_name):
        return f"## {metric_name}\nData-quality indicator, not a TMA node.\n\n{MUX_EXPLANATION}"
    info = get_metric_info(metric_name)
    if not info:
        return f"Unknown metric '{metric_name}'. Use a full path like 'Backend_Bound.Memory_Bound' or a leaf name like 'DRAM_Bound'."

    lines = [
        f"## {metric_name}",
        f"Level: {info.get('level', '?')} | Parent: {info.get('parent', 'None')}",
        "",
        info.get("description", "No description available."),
        "",
    ]

    causes = info.get("typical_causes", [])
    if causes:
        lines.append("### Typical Causes")
        for c in causes:
            lines.append(f"  - {c}")
        lines.append("")

    hints = info.get("tuning_hints", [])
    if hints:
        lines.append("### Tuning Hints")
        for h in hints:
            lines.append(f"  - {h}")

    return "\n".join(lines)


@mcp.tool()
def list_profiling_runs(
    process_name: str | None = None,
    labels: dict[str, str] | None = None,
    last_hours: float = 24.0,
) -> str:
    """List recent Top-Down profiling runs.

    Args:
        process_name: Filter by process name
        labels: Filter by labels
        last_hours: Time window in hours (default 24)
    """
    backend = _get_backend()
    try:
        runs = backend.list_runs(process_name=process_name, labels=labels, last_hours=last_hours, limit=20)
        if not runs:
            return "No runs found."

        lines = ["Recent profiling runs:\n"]
        for r in runs:
            interesting = {k: v for k, v in r.labels.items()
                          if k in ("git_branch", "test_name", "topology", "build_variant")}
            label_str = ", ".join(f"{k}={v}" for k, v in interesting.items()) if interesting else ""
            kind = f"Apple {r.labels.get('measurement_kind', 'native')}" if r.labels.get("collector") == "apple" else f"L{r.level}"
            lines.append(
                f"  {r.run_id} | {r.started_at.strftime('%Y-%m-%d %H:%M')} | "
                f"{r.process_name} | {kind} | {r.duration_seconds:.1f}s"
                + (f" | {label_str}" if label_str else "")
            )
        return "\n".join(lines)
    finally:
        backend.close()


# ──────────────────────────── Resources ────────────────────────────


@mcp.resource("topdown://runs/{run_id}/tree")
def run_tree(run_id: str) -> str:
    """Full hierarchical TMA tree for a specific run."""
    from topdown.analysis.topdown_tree import build_tree, format_tree_text

    backend = _get_backend()
    try:
        metrics = backend.get_aggregated_metrics(run_id)
        if not metrics:
            return f"No metrics for run {run_id}"
        from topdown.units import lowest_mux, mux_note

        text = format_tree_text(build_tree(metrics))
        mux = lowest_mux(metrics)
        return f"{text}\n\n{mux_note(mux)}" if mux is not None else text
    finally:
        backend.close()


@mcp.resource("topdown://metrics")
def all_metrics() -> str:
    """List of all known TMA metrics with short descriptions.

    Selects the Intel or AMD knowledge base automatically via
    ``/proc/cpuinfo`` vendor_id.
    """
    from topdown.knowledge import active_vendor, list_all_metrics, get_metric_info

    vendor = active_vendor()
    names = list_all_metrics()
    header = f"All known TMA metrics (vendor={vendor}):\n"
    lines = [header]
    for name in names:
        info = get_metric_info(name)
        if not isinstance(info, dict) or "description" not in info:
            continue
        desc = info.get("description", "")[:80]
        lines.append(f"  L{info.get('level', '?')} {name}: {desc}")
    return "\n".join(lines)


@mcp.resource("topdown://methodology")
def methodology() -> str:
    """CPU/GPU profiling methodology, including native Apple scopes and measurement limits."""
    from topdown.knowledge.methodology import get_methodology
    return get_methodology()


# ──────────────────────────── Server entry ────────────────────────────


def _register_tools_and_resources(target: FastMCP):
    """Re-register all tools and resources on a new FastMCP instance."""
    from topdown.collector.apple_runtime import worker

    # Tools
    target.tool()(collect_topdown)
    target.tool()(worker(apple_doctor))
    target.tool()(worker(import_apple_result))
    target.tool()(worker(collect_apple_profile, capture=True))
    target.tool()(worker(query_apple_result))
    target.tool()(worker(get_apple_hotspots))
    target.tool()(worker(collect_apple_gpu_profile, capture=True))
    target.tool()(worker(profile_apple_gpu_trace, capture=True))
    target.tool()(worker(get_apple_gpu_report))
    target.tool()(query_bottlenecks)
    target.tool()(query_by_bottleneck)
    target.tool()(get_funnel)
    target.tool()(compare_funnel)
    target.tool()(compare_runs)
    target.tool()(compare_by_labels)
    target.tool()(explain_metric)
    target.tool()(list_profiling_runs)
    # Resources
    target.resource("topdown://runs/{run_id}/tree")(run_tree)
    target.resource("topdown://metrics")(all_metrics)
    target.resource("topdown://methodology")(methodology)


def run_server(transport: str = "stdio", host: str = "localhost", port: int = 8000):
    """Start the MCP server."""
    from topdown.auth import AuthConfig

    auth_config = AuthConfig.from_env()

    if transport == "stdio":
        if auth_config.is_enabled:
            import logging
            logging.getLogger(__name__).warning(
                "Auth mode '%s' is enabled but stdio transport doesn't use HTTP auth. "
                "Auth is only enforced on HTTP transport.",
                auth_config.mode,
            )
        mcp.run(transport="stdio")
    elif transport == "http":
        # Create a new MCP instance with auth for HTTP transport
        server = _create_mcp(host=host, port=port)
        _register_tools_and_resources(server)
        server.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
