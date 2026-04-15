"""Subprocess wrapper for perf stat --topdown (ARM Neoverse and Intel).

Provides the same interface as ToplevRunner (run_and_parse -> list[ToplevSample])
so the analysis/storage layers work identically for both collectors.

Output format (perf stat --topdown -I <ms> -x,)::

    Header:  time,percent of slots  bad_speculation,percent of slots  retiring,...
    Data:    1.000839140,1.4,31.6,16.9,50.0,

Metric names appear in the header row; data rows are positional values.
"""

import logging
import signal
import subprocess
from dataclasses import dataclass, field

from topdown.collector.csv_parser import ToplevSample

logger = logging.getLogger(__name__)

# Mapping from perf stat header tokens to canonical TMA metric names.
# The header contains entries like "percent of slots  bad_speculation".
# We extract the trailing identifier and map it here.
_HEADER_NAME_MAP: dict[str, str] = {
    # L1 metrics
    "retiring": "Retiring",
    "frontend_bound": "Frontend_Bound",
    "backend_bound": "Backend_Bound",
    "bad_speculation": "Bad_Speculation",
    # L2 metrics (may appear on newer kernels)
    "heavy_operations": "Retiring.Heavy_Operations",
    "light_operations": "Retiring.Light_Operations",
    "branch_mispredicts": "Bad_Speculation.Branch_Mispredicts",
    "machine_clears": "Bad_Speculation.Machine_Clears",
    "fetch_latency": "Frontend_Bound.Fetch_Latency",
    "fetch_bandwidth": "Frontend_Bound.Fetch_Bandwidth",
    "memory_bound": "Backend_Bound.Memory_Bound",
    "core_bound": "Backend_Bound.Core_Bound",
}

# Also support the topdown-* event names (seen on some Intel perf versions)
_EVENT_NAME_MAP: dict[str, str] = {
    "topdown-retiring": "Retiring",
    "topdown-fe-bound": "Frontend_Bound",
    "topdown-be-bound": "Backend_Bound",
    "topdown-bad-spec": "Bad_Speculation",
    "topdown-heavy-ops": "Retiring.Heavy_Operations",
    "topdown-light-ops": "Retiring.Light_Operations",
    "topdown-br-mispredict": "Bad_Speculation.Branch_Mispredicts",
    "topdown-machine-clears": "Bad_Speculation.Machine_Clears",
    "topdown-fetch-lat": "Frontend_Bound.Fetch_Latency",
    "topdown-fetch-bw": "Frontend_Bound.Fetch_Bandwidth",
    "topdown-mem-bound": "Backend_Bound.Memory_Bound",
    "topdown-core-bound": "Backend_Bound.Core_Bound",
}


@dataclass
class PerfStatOptions:
    """Options for perf stat --topdown collection."""

    interval_ms: int = 1000
    pids: list[int] | None = None
    system_wide: bool = False
    extra_args: list[str] = field(default_factory=list)


class PerfStatRunner:
    """Wraps perf stat --topdown as a subprocess."""

    def __init__(self, options: PerfStatOptions):
        self.options = options

    def build_command(self) -> list[str]:
        """Build the perf stat --topdown command line."""
        cmd = [
            "perf",
            "stat",
            "--topdown",
            f"-I{self.options.interval_ms}",
            "-x,",  # CSV output with comma delimiter
        ]

        if self.options.pids:
            pid_str = ",".join(str(p) for p in self.options.pids)
            cmd.extend(["-p", pid_str])
        elif self.options.system_wide:
            cmd.append("-a")

        cmd.extend(self.options.extra_args)
        return cmd

    def run(self, duration_seconds: int) -> tuple[str, str]:
        """Run perf stat for a duration, return (stdout, stderr).

        When monitoring specific PIDs (``-p``), runs perf stat without a
        workload command and sends SIGINT after the duration to collect
        results. When system-wide (``-a``) or no target, uses
        ``-- sleep <duration>`` so perf stat exits naturally.
        """
        cmd = self.build_command()
        use_sleep_workload = not self.options.pids
        if use_sleep_workload:
            cmd.extend(["--", "sleep", str(duration_seconds)])
        logger.info("Running: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if not use_sleep_workload:
                # PID mode: let perf collect for the duration, then SIGINT
                import time
                time.sleep(duration_seconds)
                proc.send_signal(signal.SIGINT)

            try:
                stdout, stderr = proc.communicate(
                    timeout=duration_seconds + 30,
                )
            except subprocess.TimeoutExpired:
                proc.send_signal(signal.SIGINT)
                try:
                    stdout, stderr = proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=5)

            return stdout, stderr

        except FileNotFoundError:
            raise RuntimeError(
                "perf not found. Install linux-tools: "
                "apt install linux-tools-$(uname -r) or yum install perf"
            )

    def run_and_parse(self, duration_seconds: int) -> list[ToplevSample]:
        """Run perf stat --topdown and return parsed samples.

        perf stat writes CSV data to stderr, same as toplev.
        """
        stdout, stderr = self.run(duration_seconds)

        if stderr:
            for line in stderr.strip().splitlines():
                ll = line.lower()
                if "error" in ll or "not supported" in ll:
                    logger.warning("perf stat: %s", line)

        # perf stat writes CSV data to stderr
        samples = parse_perf_stat_output(stderr)
        if not samples and stdout:
            # Fallback: try stdout
            samples = parse_perf_stat_output(stdout)
        logger.info("Parsed %d samples from perf stat output", len(samples))
        return samples


def _extract_metric_name(header_col: str) -> str | None:
    """Extract the metric identifier from a perf stat header column.

    Header columns look like ``"percent of slots  bad_speculation"``
    or ``"topdown-retiring"`` or just ``"bad_speculation"``.
    Returns the canonical TMA name or None if not recognized.
    """
    col = header_col.strip()
    if not col or col == "time":
        return None

    # Format 1: "topdown-*" event names (some Intel perf versions)
    if col.startswith("topdown-"):
        return _EVENT_NAME_MAP.get(col)

    # Format 2: "percent of slots  <name>" (ARM / newer perf)
    # Extract the last whitespace-separated token
    parts = col.split()
    if parts:
        token = parts[-1].lower()
        return _HEADER_NAME_MAP.get(token)

    return None


def parse_perf_stat_output(text: str) -> list[ToplevSample]:
    """Parse perf stat --topdown -x, output into ToplevSample objects.

    Supports two output formats:

    **Format A (ARM / newer perf):** Header row with metric names, then
    positional data rows::

        time,percent of slots  bad_speculation,percent of slots  retiring,...
        1.000839140,1.4,31.6,16.9,50.0,

    **Format B (some Intel perf):** Per-line event names::

        1.001234,25.30,%,topdown-retiring,1000000000,100.00

    The parser auto-detects the format from the first non-comment line.
    """
    if not text or not text.strip():
        return []

    lines = text.strip().splitlines()
    if not lines:
        return []

    # Skip comment lines at the start
    data_lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if not data_lines:
        return []

    # Detect format: if the first line contains metric identifiers in the
    # header (non-numeric first column), parse as Format A (header + data).
    # Otherwise parse as Format B (per-line events).
    first_cols = [c.strip() for c in data_lines[0].split(",")]

    # Format A: first column is "time" or header contains "percent of slots"
    # or header contains known metric names
    is_header_format = False
    if first_cols[0].lower() == "time":
        is_header_format = True
    elif not _is_timestamp(first_cols[0]):
        # First column is not a number — likely a header
        is_header_format = True

    if is_header_format:
        return _parse_header_format(data_lines)
    else:
        return _parse_perline_format(data_lines)


def _is_timestamp(s: str) -> bool:
    """Check if a string looks like a float timestamp."""
    try:
        float(s.strip())
        return True
    except ValueError:
        return False


def _parse_header_format(lines: list[str]) -> list[ToplevSample]:
    """Parse header-based format (Format A).

    First line is the header mapping columns to metric names.
    Subsequent lines are positional data: timestamp,val1,val2,...
    """
    header_cols = [c.strip() for c in lines[0].split(",")]

    # Build column index -> canonical metric name mapping
    col_metrics: dict[int, str] = {}
    for i, col in enumerate(header_cols):
        name = _extract_metric_name(col)
        if name:
            col_metrics[i] = name

    if not col_metrics:
        logger.warning("No topdown metrics found in header: %s", lines[0])
        return []

    logger.debug("Header metric mapping: %s", col_metrics)

    samples = []
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        cols = [c.strip() for c in line.split(",")]

        # Parse timestamp (first column)
        try:
            timestamp = float(cols[0])
        except (ValueError, IndexError):
            continue

        # Parse each metric value by column position
        for col_idx, metric_name in col_metrics.items():
            if col_idx >= len(cols):
                continue
            raw = cols[col_idx].rstrip("%")
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue

            samples.append(
                ToplevSample(
                    timestamp=timestamp,
                    cpu=None,
                    metric_name=metric_name,
                    value=value,
                    unit="%",
                    status="",
                )
            )

    return samples


def _parse_perline_format(lines: list[str]) -> list[ToplevSample]:
    """Parse per-line format (Format B).

    Each line contains: timestamp,value,unit,event-name,...
    The event name column is found by scanning for a topdown-* token.
    """
    samples = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 4:
            continue

        # Find the topdown event name by scanning columns
        event_name = None
        event_idx = None
        for i, col in enumerate(cols):
            if col.startswith("topdown-"):
                event_name = col
                event_idx = i
                break

        if event_name is None:
            continue

        mapped_name = _EVENT_NAME_MAP.get(event_name)
        if mapped_name is None:
            logger.debug("Skipping unknown perf topdown event: %s", event_name)
            continue

        # Timestamp is always the first column
        try:
            timestamp = float(cols[0])
        except ValueError:
            timestamp = None

        # Value: scan columns between timestamp and event name
        value = None
        for i in range(1, event_idx):
            raw = cols[i].rstrip("%")
            if not raw:
                continue
            try:
                v = float(raw)
                if 0.0 <= v <= 100.0:
                    value = v
                    break
            except ValueError:
                continue

        if value is None:
            continue

        samples.append(
            ToplevSample(
                timestamp=timestamp,
                cpu=None,
                metric_name=mapped_name,
                value=value,
                unit="%",
                status="",
            )
        )

    return samples


def check_perf_stat_available() -> bool:
    """Check if perf stat --topdown is runnable."""
    try:
        result = subprocess.run(
            ["perf", "stat", "--topdown", "--", "sleep", "0"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_perf_topdown_supported() -> tuple[bool, str]:
    """Check if perf stat --topdown produces topdown metrics.

    Returns (ok, message).  Runs a 1-second collection and checks for
    topdown metric indicators in the output (either ``topdown-`` event
    names or header keywords like ``retiring``, ``frontend_bound``).
    """
    try:
        result = subprocess.run(
            ["perf", "stat", "--topdown", "-x,", "--", "sleep", "1"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stderr + result.stdout
        # Check for both output formats:
        # Format A header: "percent of slots  retiring" or just "retiring"
        # Format B per-line: "topdown-retiring"
        if "topdown-" in output or "retiring" in output:
            return True, "perf stat --topdown supported"
        if result.returncode != 0:
            return False, f"perf stat --topdown failed: {result.stderr.strip()[:200]}"
        return False, (
            "perf stat --topdown ran but produced no topdown events. "
            "Kernel PMU support for topdown metrics may be missing."
        )
    except FileNotFoundError:
        return False, "perf not found. Install linux-tools or perf package."
    except subprocess.TimeoutExpired:
        return False, "perf stat --topdown timed out."
