"""MCP (Model Context Protocol) server for topdown-profiler.

Exposes TMA collection, querying, and analysis as MCP tools and resources
for AI-assisted CPU performance analysis.

Auth modes (via TOPDOWN_AUTH_MODE env var):
- "none" (default): no auth, suitable for stdio transport / local use
- "api-key": bearer token auth, use `topdown setup` to generate a key
- "oauth": JWT validation against an external authorization server
"""


from mcp.server.fastmcp import FastMCP

from topdown.config import get_config
from topdown.storage import get_backend

_MCP_INSTRUCTIONS = (
    "You are connected to the topdown-profiler MCP server. "
    "This tool collects, stores, and queries Top-Down Microarchitecture "
    "Analysis (TMA) data using pmu-tools/toplev on Intel or perf stat "
    "--topdown on ARM Neoverse. Use the available tools to profile "
    "processes, query bottlenecks, compare runs, and explain CPU "
    "performance metrics."
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


def _get_backend():
    config = get_config()
    return get_backend(config)


# ──────────────────────────── Tools ────────────────────────────


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
    from topdown.analysis.bottleneck import find_bottlenecks, summarize_bottlenecks

    config = get_config()
    collector = resolve_collector(config)

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
        bottleneck_list = find_bottlenecks(metrics, top_n=5)
        summary = summarize_bottlenecks(bottleneck_list)

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
    from topdown.analysis.bottleneck import find_bottlenecks, summarize_bottlenecks

    backend = _get_backend()
    try:
        runs = backend.list_runs(process_name=process_name, labels=labels, last_hours=last_hours, limit=1)
        if not runs:
            return "No runs found matching filters."

        run = runs[0]
        metrics = backend.get_aggregated_metrics(run.run_id)
        found = find_bottlenecks(metrics, top_n=top_n, min_percentage=min_percentage)

        if not found:
            return f"No bottlenecks above {min_percentage}% found."

        lines = [
            f"Run: {run.run_id[:12]} | {run.process_name} | L{run.level} | {run.started_at.strftime('%Y-%m-%d %H:%M')}",
            f"Labels: {', '.join(f'{k}={v}' for k, v in run.labels.items() if k in ('git_branch', 'test_name', 'topology', 'build_variant'))}",
            "",
            summarize_bottlenecks(found),
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
        metric_name: TMA metric name (e.g. 'DRAM_Bound', 'L3_Bound', 'Branch_Mispredicts')
        min_pct: Minimum percentage threshold
        labels: Optional label filters
        last_hours: Time window in hours
    """
    import json

    backend = _get_backend()
    try:
        results = backend.query_by_bottleneck(
            metric_name=metric_name, min_pct=min_pct,
            labels=labels, last_hours=last_hours,
        )
        if not results:
            return f"No runs found where {metric_name} >= {min_pct}%"

        lines = [f"Runs where {metric_name} >= {min_pct}%:\n"]
        for r in results:
            run_labels = r.get("labels", "{}")
            if isinstance(run_labels, str):
                run_labels = json.loads(run_labels)
            interesting = {k: v for k, v in run_labels.items()
                          if k in ("git_branch", "git_hash", "test_name", "topology", "build_variant", "client_tool")}
            label_str = ", ".join(f"{k}={v}" for k, v in interesting.items())
            lines.append(
                f"  {r['run_id'][:12]} | {r.get('avg_value', 0):.1f}% | "
                f"{r.get('process_name', '')} | {label_str}"
            )
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

        metrics = backend.get_aggregated_metrics(run.run_id)
        if not metrics:
            return "No metrics found for this run."

        result = build_funnel(metrics, max_level=level)
        header = f"Run: {run.run_id[:12]} | {run.process_name} | L{run.level}\n\n"
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

        metrics_a = backend.get_aggregated_metrics(run_a.run_id)
        metrics_b = backend.get_aggregated_metrics(run_b.run_id)

        funnel_a = build_funnel(metrics_a, max_level=level)
        funnel_b = build_funnel(metrics_b, max_level=level)

        header = (
            f"A: {run_a.run_id[:12]} | {run_a.labels.get('git_branch', '?')} | "
            f"{run_a.labels.get('test_name', '?')}\n"
            f"B: {run_b.run_id[:12]} | {run_b.labels.get('git_branch', '?')} | "
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

        metrics_a = backend.get_aggregated_metrics(run_a.run_id)
        metrics_b = backend.get_aggregated_metrics(run_b.run_id)

        deltas = do_compare(metrics_a, metrics_b)
        if not deltas:
            return "No significant differences found."

        return (
            f"Comparing {run_a.run_id[:12]} (baseline) vs {run_b.run_id[:12]}\n\n"
            + summarize_comparison(deltas)
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
        metrics_a = backend.get_aggregated_metrics(run_a.run_id)
        metrics_b = backend.get_aggregated_metrics(run_b.run_id)

        deltas = do_compare(metrics_a, metrics_b)
        if not deltas:
            return "No significant differences found."

        label_str_a = ", ".join(f"{k}={v}" for k, v in label_a.items())
        label_str_b = ", ".join(f"{k}={v}" for k, v in label_b.items())
        return (
            f"Comparing [{label_str_a}] vs [{label_str_b}]\n\n"
            + summarize_comparison(deltas)
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
            lines.append(
                f"  {r.run_id[:12]} | {r.started_at.strftime('%Y-%m-%d %H:%M')} | "
                f"{r.process_name} | L{r.level} | {r.duration_seconds:.1f}s"
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
        tree = build_tree(metrics)
        return format_tree_text(tree)
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
    """Intel TMA methodology overview."""
    from topdown.knowledge.methodology import get_methodology
    return get_methodology()


# ──────────────────────────── Server entry ────────────────────────────


def _register_tools_and_resources(target: FastMCP):
    """Re-register all tools and resources on a new FastMCP instance."""
    # Tools
    target.tool()(collect_topdown)
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
