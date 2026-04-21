"""Subprocess wrapper for AMDuProfPcm (AMD Zen pipeline utilization).

Provides the same interface as ToplevRunner/PerfStatRunner (``run_and_parse``
-> ``list[ToplevSample]``) so the analysis/storage layers work identically
across Intel (toplev), ARM Neoverse (perf stat), and AMD Zen (uprof_pcm).

AMD uProf exposes Zen 4+ Pipeline Utilization metrics that map cleanly onto
the canonical TMA hierarchy we already use. The closest analog to toplev is
``AMDuProfPcm -m pipeline_util -a -d <sec> -o <csv>``.

Output format (AMDuProfPcm -m pipeline_util -a -d <N> -o <csv>)::

    # Preamble lines (TSC_Frequency, Socket count, NPS, Group_Type)
    TSC_Frequency,100,Mhz
    Group_Type,pipeline_util

    Timestamp,Total_Dispatch_Slots,Frontend_Bound,Frontend_Bound.Latency,...
    09:12:08.123,1234567,15.23,10.3,...
    09:12:09.123,1234678,14.89,9.8,...

Pipeline_util metric naming (Zen 4+, per AMD uProf User Guide v4.2+):
- Frontend_Bound.Latency / Frontend_Bound.BW
- Bad_Speculation.Mispredicts / Bad_Speculation.Pipeline_Restarts
- Backend_Bound.Memory / Backend_Bound.CPU
- Retiring.Fastpath / Retiring.Microcode

These are mapped to canonical TMA names (Fetch_Latency, Fetch_Bandwidth,
Branch_Mispredicts, Machine_Clears, Memory_Bound, Core_Bound, Light/Heavy
Operations) so downstream code is vendor-agnostic.

NOTE: Column layout and exact header casing of AMDuProfPcm CSV output vary
slightly across uProf versions (4.x vs 5.x). The parser matches metric
names case-insensitively and ignores unknown columns. Integration testing
on a real AMD Zen machine is required to confirm version-specific quirks.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from topdown.collector.csv_parser import ToplevSample

logger = logging.getLogger(__name__)

# Mapping from AMDuProfPcm pipeline_util CSV header tokens to canonical TMA names.
# Keys are lower-cased (we match case-insensitively).
_AMD_NAME_MAP: dict[str, str] = {
    # L1 (top-level)
    "retiring": "Retiring",
    "frontend_bound": "Frontend_Bound",
    "backend_bound": "Backend_Bound",
    "bad_speculation": "Bad_Speculation",
    # L2 (sub-category) — AMD naming -> canonical
    "frontend_bound.latency": "Frontend_Bound.Fetch_Latency",
    "frontend_bound.bw": "Frontend_Bound.Fetch_Bandwidth",
    "bad_speculation.mispredicts": "Bad_Speculation.Branch_Mispredicts",
    "bad_speculation.pipeline_restarts": "Bad_Speculation.Machine_Clears",
    "backend_bound.memory": "Backend_Bound.Memory_Bound",
    "backend_bound.cpu": "Backend_Bound.Core_Bound",
    "retiring.fastpath": "Retiring.Light_Operations",
    "retiring.microcode": "Retiring.Heavy_Operations",
}

# Columns we skip (metadata / non-TMA quantities in pipeline_util group).
_SKIP_COLUMNS = {"timestamp", "total_dispatch_slots", "smt_disp_contention", "time", ""}

# When ``passthrough_unmapped=True`` on the parser, these groups' columns
# (case-insensitive substring match on the header) are emitted with their
# raw AMD names under the ``AMD.`` prefix, so cache/memory counters from
# ``-m l1,l2,l3,memory`` are queryable without polluting the canonical TMA
# hierarchy. Callers that only want canonical TMA metrics get unchanged
# behaviour (default).
_PASSTHROUGH_HINTS = (
    "access_rate", "miss_rate", "hit_rate",
    "access", "miss", "hit",
    "ic_", "op_cache", "l1_", "l2_", "l3_",
    "bw_", "bandwidth", "memory_bw",
    "ipc",
)


@dataclass
class UprofPcmOptions:
    """Options for AMDuProfPcm pipeline_util collection."""

    interval_ms: int = 1000
    pids: list[int] | None = None
    system_wide: bool = False
    # Group(s) to pass via -m. Default "pipeline_util" gives the TMA-analog
    # breakdown. Extend with "l1,l2,l3,memory" for cache/memory metrics.
    metric_group: str = "pipeline_util"
    uprof_pcm_path: str | None = None  # None => search $PATH and /opt/AMDuProf_*/bin/
    # When True, non-TMA columns from -m l1/l2/l3/memory are emitted under
    # an ``AMD.`` prefix so they can be queried / charted alongside TMA.
    # Default off for backward compatibility with pipeline_util-only use.
    passthrough_unmapped: bool = False
    extra_args: list[str] = field(default_factory=list)


class UprofPcmRunner:
    """Wraps AMDuProfPcm as a subprocess.

    AMDuProfPcm does not have a direct per-PID mode for time-series metric
    collection (that path is AMDuProfCLI with symbol sampling). For Phase 1
    we support system-wide collection only — on dedicated benchmark runners
    this is equivalent to per-process. If PIDs are provided, we log a
    warning and fall back to system-wide.
    """

    def __init__(self, options: UprofPcmOptions):
        self.options = options
        self._binary: str | None = None

    # ── binary discovery ─────────────────────────────────────────────

    def _locate_binary(self) -> str:
        """Return the path to AMDuProfPcm, or raise RuntimeError."""
        if self._binary:
            return self._binary
        if self.options.uprof_pcm_path:
            path = self.options.uprof_pcm_path
            if not os.path.isfile(path):
                raise RuntimeError(f"AMDuProfPcm not found at configured path: {path}")
            self._binary = path
            return path

        which = shutil.which("AMDuProfPcm")
        if which:
            self._binary = which
            return which

        # Fallback: glob /opt/AMDuProf_*/bin/AMDuProfPcm
        for opt_dir in sorted(Path("/opt").glob("AMDuProf_*"), reverse=True):
            candidate = opt_dir / "bin" / "AMDuProfPcm"
            if candidate.is_file():
                self._binary = str(candidate)
                return str(candidate)

        raise RuntimeError(
            "AMDuProfPcm not found. Install AMD uProf from "
            "https://www.amd.com/en/developer/uprof.html (extracts to "
            "/opt/AMDuProf_X.Y-ZZZ/), or set UprofPcmOptions.uprof_pcm_path."
        )

    # ── command build + run ──────────────────────────────────────────

    def build_command(self, output_path: str, duration_seconds: int) -> list[str]:
        """Build the AMDuProfPcm command line.

        Flags::

          -m <group>   Metric group(s) to collect (comma-separated).
          -a           All cores (system-wide). AMDuProfPcm pipeline_util
                       requires this for time-series metric rows.
          -d <sec>     Collection duration.
          -o <csv>     Output CSV file path.
        """
        binary = self._locate_binary()
        cmd = [
            binary,
            "-m", self.options.metric_group,
            "-a",                       # system-wide
            "-d", str(duration_seconds),
            "-o", output_path,
        ]
        cmd.extend(self.options.extra_args)
        return cmd

    def run(self, duration_seconds: int) -> str:
        """Run AMDuProfPcm for a duration and return the CSV output path.

        If PIDs were specified (not supported by AMDuProfPcm system-wide
        metric collection), log a warning and still run system-wide.
        """
        if self.options.pids:
            logger.warning(
                "AMDuProfPcm does not support per-PID time-series metric "
                "collection for group %r; running system-wide instead. "
                "PIDs requested: %s",
                self.options.metric_group,
                self.options.pids,
            )

        tmp = tempfile.NamedTemporaryFile(
            prefix="uprof_pcm_", suffix=".csv", delete=False
        )
        tmp.close()
        output_path = tmp.name
        cmd = self.build_command(output_path, duration_seconds)
        logger.info("Running: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=duration_seconds + 60)
            except subprocess.TimeoutExpired:
                proc.send_signal(signal.SIGINT)
                try:
                    stdout, stderr = proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate(timeout=5)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"AMDuProfPcm failed to launch ({e}). Check install at "
                "/opt/AMDuProf_*/bin/AMDuProfPcm and that it's executable."
            )

        if proc.returncode != 0:
            err = (stderr or stdout or "").strip()[:500]
            hint = ""
            if "permission" in err.lower() or "root" in err.lower() or proc.returncode == 126:
                hint = (
                    " Note: AMDuProfPcm system-wide mode typically requires "
                    "root (IBS / PMU driver access). Try sudo or install the "
                    "uProf kernel module."
                )
            raise RuntimeError(
                f"AMDuProfPcm exited with status {proc.returncode}: {err}{hint}"
            )

        return output_path

    def run_and_parse(self, duration_seconds: int) -> list[ToplevSample]:
        """Run AMDuProfPcm and return parsed samples."""
        try:
            output_path = self.run(duration_seconds)
        except RuntimeError as e:
            logger.error("%s", e)
            raise

        try:
            with open(output_path, "r") as f:
                text = f.read()
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass

        samples = parse_uprof_pcm_output(
            text, passthrough_unmapped=self.options.passthrough_unmapped,
        )
        logger.info("Parsed %d samples from AMDuProfPcm output", len(samples))
        return samples


# ── parser ──────────────────────────────────────────────────────────

# Timestamp column values look like "09:12:08.123" (HH:MM:SS.fff) or a float.
# We normalize them to a float seconds offset relative to the first row.
_HHMMSS_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?$")


def _parse_timestamp(cell: str) -> float | None:
    """Parse a timestamp cell to seconds (float).

    Accepts either HH:MM:SS.fff or a plain float / int string. Returns None
    if the cell doesn't look like a timestamp.
    """
    s = cell.strip()
    if not s:
        return None
    m = _HHMMSS_RE.match(s)
    if m:
        h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3))
        frac = float("0." + m.group(4)) if m.group(4) else 0.0
        return h * 3600 + mi * 60 + se + frac
    try:
        return float(s)
    except ValueError:
        return None


def _canonicalize_amd_metric(header_col: str) -> str | None:
    """Translate an AMDuProfPcm CSV header column to a canonical TMA name.

    Returns None if the column is metadata, unknown, or not a TMA metric.
    Matching is case-insensitive and ignores leading/trailing whitespace,
    quotes, and surrounding "Percent of " prefixes some versions use.
    """
    raw = header_col.strip().strip('"').strip()
    if not raw:
        return None
    low = raw.lower()
    # Strip common prefixes seen in uProf CSV across versions
    for prefix in ("percent of ", "pipeline_util.", "pipeline util."):
        if low.startswith(prefix):
            low = low[len(prefix):]
            break
    low = low.strip()
    if low in _SKIP_COLUMNS:
        return None
    # Direct hit on the map
    if low in _AMD_NAME_MAP:
        return _AMD_NAME_MAP[low]
    # Try spaces -> underscores (some versions emit "Backend Bound.Memory")
    normalized = low.replace(" ", "_")
    if normalized in _AMD_NAME_MAP:
        return _AMD_NAME_MAP[normalized]
    return None


def _passthrough_amd_metric(header_col: str) -> str | None:
    """Emit an ``AMD.<raw_name>`` canonical name for cache/memory columns.

    Used when ``passthrough_unmapped=True`` in the parser. Returns ``None``
    for columns that look like metadata / timestamps, otherwise returns a
    namespaced passthrough label (e.g. ``AMD.L3_Miss_Rate``).
    """
    raw = header_col.strip().strip('"').strip()
    if not raw:
        return None
    low = raw.lower()
    if low in _SKIP_COLUMNS:
        return None
    # Already-mapped TMA columns should never passthrough
    if _canonicalize_amd_metric(raw) is not None:
        return None
    # Only passthrough if the column looks like a metric (not random free-text)
    for hint in _PASSTHROUGH_HINTS:
        if hint in low:
            # Sanitise: keep alnum + underscore + dot; drop other chars
            clean = re.sub(r"[^A-Za-z0-9_.]", "_", raw.strip())
            clean = re.sub(r"_+", "_", clean).strip("_")
            if clean:
                return f"AMD.{clean}"
            return None
    return None


def parse_uprof_pcm_output(text: str, passthrough_unmapped: bool = False) -> list[ToplevSample]:
    """Parse AMDuProfPcm pipeline_util CSV into ToplevSample objects.

    The CSV has a short preamble (TSC_Frequency, Socket count, NPS,
    Group_Type), then a blank line, then a header row starting with
    "Timestamp", then one data row per interval. Preamble and blank
    lines are skipped; we locate the header by its first column being
    "timestamp" (case-insensitive).

    When ``passthrough_unmapped`` is True (default False), cache/memory
    columns from ``-m l1,l2,l3,memory`` that look like metrics are emitted
    with their raw AMD names under an ``AMD.`` prefix (e.g.
    ``AMD.L3_Miss_Rate``) so they're queryable without polluting the
    canonical TMA hierarchy.
    """
    if not text or not text.strip():
        return []

    # Find header row
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        first = line.split(",", 1)[0].strip().strip('"').lower()
        if first == "timestamp":
            header_idx = i
            break
    if header_idx is None:
        logger.warning("AMDuProfPcm output: could not locate Timestamp header row")
        return []

    header_cols = [c.strip().strip('"') for c in lines[header_idx].split(",")]

    # Map column index -> canonical metric name (skip metadata columns).
    # First pass: canonical TMA. Second pass (optional): AMD passthrough.
    col_metrics: dict[int, str] = {}
    for i, col in enumerate(header_cols):
        name = _canonicalize_amd_metric(col)
        if name:
            col_metrics[i] = name
    if passthrough_unmapped:
        for i, col in enumerate(header_cols):
            if i in col_metrics:
                continue
            name = _passthrough_amd_metric(col)
            if name:
                col_metrics[i] = name

    if not col_metrics:
        logger.warning(
            "AMDuProfPcm output: no recognized TMA columns in header: %s",
            header_cols,
        )
        return []

    logger.debug("AMD uProf column mapping: %s", col_metrics)

    # Parse data rows
    samples: list[ToplevSample] = []
    t0: float | None = None
    for line in lines[header_idx + 1:]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip().strip('"') for c in line.split(",")]
        if not cols or not cols[0]:
            continue
        ts = _parse_timestamp(cols[0])
        if ts is None:
            continue
        if t0 is None:
            t0 = ts
        rel_ts = ts - t0

        for col_idx, metric_name in col_metrics.items():
            if col_idx >= len(cols):
                continue
            raw = cols[col_idx].rstrip("%").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            samples.append(
                ToplevSample(
                    timestamp=rel_ts,
                    cpu=None,
                    metric_name=metric_name,
                    value=value,
                    unit="%",
                    status="",
                )
            )

    return samples


# ── availability probe ──────────────────────────────────────────────

def check_uprof_pcm_available(uprof_pcm_path: str | None = None) -> tuple[bool, str]:
    """Check if AMDuProfPcm is installed and runnable.

    Returns (ok, message). Looks in $PATH first, then
    /opt/AMDuProf_*/bin/AMDuProfPcm. Does not run the tool — just verifies
    the binary exists and is executable.
    """
    if uprof_pcm_path:
        if os.path.isfile(uprof_pcm_path) and os.access(uprof_pcm_path, os.X_OK):
            return True, f"AMDuProfPcm at {uprof_pcm_path}"
        return False, f"AMDuProfPcm not executable at {uprof_pcm_path}"

    which = shutil.which("AMDuProfPcm")
    if which:
        return True, f"AMDuProfPcm at {which}"

    for opt_dir in sorted(Path("/opt").glob("AMDuProf_*"), reverse=True):
        candidate = opt_dir / "bin" / "AMDuProfPcm"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return True, f"AMDuProfPcm at {candidate}"

    return False, (
        "AMDuProfPcm not found. Install AMD uProf from "
        "https://www.amd.com/en/developer/uprof.html then add "
        "/opt/AMDuProf_*/bin to PATH."
    )


def detect_amd_vendor() -> bool:
    """Return True iff the host CPU vendor is AuthenticAMD.

    Reads /proc/cpuinfo and matches ``vendor_id`` against ``AuthenticAMD``.
    Returns False on non-Linux or read errors.
    """
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.lower().startswith("vendor_id"):
                    _, _, value = line.partition(":")
                    return value.strip() == "AuthenticAMD"
    except OSError:
        return False
    return False
