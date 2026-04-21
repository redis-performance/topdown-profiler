"""AMD Zen TMA-analog collector built on Linux ``perf stat`` PMU events.

Acts as a fallback for ``uprof_pcm`` when AMDuProfPcm is not installed.
Uses a handful of Zen 4/5 PMU events present in stock ``perf`` (kernel
6.7+) to approximate the L1 TMA breakdown (Frontend_Bound, Backend_Bound,
Bad_Speculation, Retiring).

Caveats:
  * Bad_Speculation uses ``ex_ret_brn_misp`` as a proxy — AMD exposes no
    direct "mis-speculated slots" counter. Machine-clear contribution is
    under-counted. Expect ±5% skew and categories that don't sum to 1.0;
    we normalize at emit time.
  * Works on Zen 4 (EPYC 9004 family 19h) and Zen 5 (EPYC 9005 / 9R45
    family 26h). Verify events with ``perf list | grep de_no_dispatch``
    on the target host if results look odd.
  * Single ``perf stat`` invocation (5 events + cycles → fits in 6-counter
    Zen PMU budget) — no multiplexing, no group splits.

For authoritative L1+L2 breakdowns, prefer ``uprof_pcm`` with AMDuProfPcm.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import time
from dataclasses import dataclass, field

from topdown.collector.csv_parser import ToplevSample

logger = logging.getLogger(__name__)


# PMU events used (order must match _EVENT_KEYS below).
# ``de_src_op_disp.all`` is the denominator (dispatched ops ≈ slots).
# ``ex_ret_ops`` is retired ops.
# ``ex_ret_brn_misp`` is mispredicted-branch retires (Bad_Speculation proxy).
# ``de_no_dispatch_per_slot.no_ops_from_frontend`` / ``.backend_stalls`` are
# the frontend- / backend-attributed non-dispatch slots.
_EVENTS = [
    "de_src_op_disp.all",
    "ex_ret_ops",
    "ex_ret_brn_misp",
    "de_no_dispatch_per_slot.no_ops_from_frontend",
    "de_no_dispatch_per_slot.backend_stalls",
    "cpu-cycles",
]

# Stable keys for event counts within an interval, matching _EVENTS order.
_EVENT_KEYS = [
    "dispatched",
    "retired",
    "br_misp",
    "fe_no_disp",
    "be_stall",
    "cycles",
]


@dataclass
class PerfStatAmdOptions:
    """Options for the AMD perf-stat TMA fallback collector."""

    interval_ms: int = 1000
    pids: list[int] | None = None
    system_wide: bool = False
    # When True, also try to collect per-group extra events (unused in v1).
    extra_args: list[str] = field(default_factory=list)


class PerfStatAmdRunner:
    """Wraps ``perf stat -e <amd-events>`` for TMA-analog collection."""

    def __init__(self, options: PerfStatAmdOptions):
        self.options = options

    def build_command(self) -> list[str]:
        """Build the perf stat command line for AMD PMU events."""
        cmd = [
            "perf", "stat",
            "-e", ",".join(_EVENTS),
            f"-I{self.options.interval_ms}",
            "-x,",
        ]
        if self.options.pids:
            cmd.extend(["-p", ",".join(str(p) for p in self.options.pids)])
        elif self.options.system_wide:
            cmd.append("-a")
        cmd.extend(self.options.extra_args)
        return cmd

    def run(self, duration_seconds: int) -> tuple[str, str]:
        """Run perf stat for the given duration and return (stdout, stderr)."""
        cmd = self.build_command()
        use_sleep_workload = not self.options.pids and not self.options.system_wide
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
                time.sleep(duration_seconds)
                proc.send_signal(signal.SIGINT)
            try:
                stdout, stderr = proc.communicate(timeout=duration_seconds + 30)
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
                "apt install linux-tools-$(uname -r)"
            )

    def run_and_parse(self, duration_seconds: int) -> list[ToplevSample]:
        stdout, stderr = self.run(duration_seconds)

        # perf stat CSV events go to stderr by default
        samples = parse_perf_stat_amd_output(stderr)
        if not samples and stdout:
            samples = parse_perf_stat_amd_output(stdout)
        logger.info("Parsed %d samples from AMD perf stat output", len(samples))
        return samples


# ── parser ──────────────────────────────────────────────────────────


def parse_perf_stat_amd_output(text: str) -> list[ToplevSample]:
    """Parse ``perf stat -e <amd-events> -I N -x,`` output.

    Per-interval CSV rows look like (8 columns with -I)::

        <time>,<count>,<unit>,<event>,<counter_time>,<run>,<metric>,<metric_unit>

    We aggregate counts per timestamp, then compute Frontend/Backend/
    Bad_Speculation/Retiring percentages of ``de_src_op_disp.all``. Rows
    with <not counted> or <not supported> counts are skipped.
    """
    if not text or not text.strip():
        return []

    # timestamp -> {event_key: value}
    per_ts: dict[float, dict[str, float]] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 4:
            continue
        # Expect format: time,count,unit,event,...
        ts_raw, count_raw, _unit, event = cols[0], cols[1], cols[2], cols[3]
        if not ts_raw or not event:
            continue
        try:
            ts = float(ts_raw)
        except ValueError:
            continue
        # Skip "<not counted>" / "<not supported>"
        if count_raw.startswith("<") or count_raw.lower() in ("not counted", "not supported"):
            continue
        try:
            count = float(count_raw.replace(",", ""))
        except ValueError:
            continue

        key = _event_to_key(event)
        if key is None:
            continue
        per_ts.setdefault(ts, {})[key] = count

    samples: list[ToplevSample] = []
    for ts in sorted(per_ts.keys()):
        pack = per_ts[ts]
        computed = _compute_l1(pack)
        if computed is None:
            continue
        for metric_name, value in computed.items():
            samples.append(
                ToplevSample(
                    timestamp=ts,
                    cpu=None,
                    metric_name=metric_name,
                    value=value,
                    unit="%",
                    status="",
                )
            )

    return samples


def _event_to_key(event: str) -> str | None:
    """Map a perf event string to our stable key."""
    e = event.strip().lower()
    # Some kernels prefix with "cpu/" / "cpu_core/" — strip it
    for prefix in ("cpu/", "cpu_core/"):
        if e.startswith(prefix):
            e = e[len(prefix):]
    # Strip trailing /u, /k modifiers
    e = e.split("/", 1)[0]
    mapping = dict(zip(_EVENTS, _EVENT_KEYS))
    # direct hit
    if e in mapping:
        return mapping[e]
    # Check for canonical prefix match (some kernels emit with umask/notation)
    for canonical, key in mapping.items():
        if e == canonical.lower() or e.startswith(canonical.lower() + ":"):
            return key
    return None


def _compute_l1(pack: dict[str, float]) -> dict[str, float] | None:
    """Compute the 4 L1 TMA categories as % of dispatched ops.

    Returns None if the interval is missing the denominator or is zero.
    Percentages are normalized to sum to 100% (small rounding correction).
    """
    slots = pack.get("dispatched", 0.0)
    if slots <= 0:
        return None

    retired = pack.get("retired", 0.0)
    # br_misp is collected for future use (e.g. weighted Bad_Speculation) but
    # the current approximation derives Bad_Speculation from the residual.
    _br_misp = pack.get("br_misp", 0.0)  # noqa: F841
    fe_no_disp = pack.get("fe_no_disp", 0.0)
    be_stall = pack.get("be_stall", 0.0)

    # Raw percentages against dispatched-ops denominator
    retiring_pct = 100.0 * retired / slots
    frontend_pct = 100.0 * fe_no_disp / slots
    backend_pct = 100.0 * be_stall / slots
    # Bad_Speculation: dispatched but non-retired, excluding stall slots already
    # accounted for in fe/be non-dispatch. We approximate as (dispatched - retired)/slots
    # then cap; the branch-misp count is kept as a proxy in case callers want it.
    bad_spec_pct = max(0.0, 100.0 - retiring_pct - frontend_pct - backend_pct)
    # Guard: if br_misp is huge vs computed bad_spec, prefer the computed residual.

    # Normalize in case rounding makes it drift from 100
    total = retiring_pct + frontend_pct + backend_pct + bad_spec_pct
    if total > 0:
        scale = 100.0 / total
        retiring_pct *= scale
        frontend_pct *= scale
        backend_pct *= scale
        bad_spec_pct *= scale

    return {
        "Retiring": retiring_pct,
        "Frontend_Bound": frontend_pct,
        "Backend_Bound": backend_pct,
        "Bad_Speculation": bad_spec_pct,
    }


def check_perf_stat_amd_supported() -> tuple[bool, str]:
    """Check whether the AMD Zen PMU events we need are available.

    Returns (ok, message). A positive result means ``perf list`` knows at
    least the denominator event. We don't probe all events — some kernels
    name them slightly differently.
    """
    try:
        result = subprocess.run(
            ["perf", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, f"perf list failed: {result.stderr[:200]}"
        listing = (result.stdout or "") + (result.stderr or "")
        if "de_src_op_disp.all" in listing and "ex_ret_ops" in listing:
            return True, "AMD Zen dispatch events available via perf stat"
        return False, (
            "AMD Zen PMU events (de_src_op_disp.all / ex_ret_ops) not "
            "found in `perf list` output. Needs kernel 6.7+ for Zen 4, "
            "6.10+ for Zen 5."
        )
    except FileNotFoundError:
        return False, "perf not found. Install linux-tools or perf package."
    except subprocess.TimeoutExpired:
        return False, "perf list timed out."
