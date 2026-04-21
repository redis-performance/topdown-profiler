"""
AMD Zen-family Pipeline Utilization knowledge base.

Parallel to ``metrics.py`` (Intel TMA) — covers the same canonical metric
names but with AMD-specific causes / tuning advice (CCX/CCD topology,
InfinityFabric bandwidth, DF C-state, ``amd_pstate`` governor, etc.).

Metric naming matches the canonical TMA hierarchy produced by the
``uprof_pcm`` collector (which translates AMDuProfPcm ``pipeline_util``
columns to ``Frontend_Bound``, ``Backend_Bound.Memory_Bound``, etc.).

Zen family coverage:
  * Zen 1 (EPYC 7xx1 Naples): L1 only — sub-categories may be unavailable
  * Zen 2 (EPYC 7xx2 Rome): L1 + partial L2
  * Zen 3 (EPYC 7xx3 Milan): L1 + full L2
  * Zen 4 (EPYC 9xx4 Genoa/Bergamo): L1 + L2 + L3 (cache/memory groups)
  * Zen 5 (EPYC 9xx5 Turin): same as Zen 4 + wider core

Each entry has the same shape as Intel METRICS_KB for drop-in use:
    description, typical_causes, tuning_hints, level, parent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


AMD_METRICS_KB: Dict[str, Dict[str, Any]] = {

    # ===================================================================
    # LEVEL 1
    # ===================================================================

    "Frontend_Bound": {
        "description": (
            "Fraction of dispatch slots stalled by the frontend (Op Cache, "
            "Decoder, BP) — the CPU couldn't deliver a micro-op to dispatch. "
            "On Zen, frontend delivery comes from two parallel sources (Op "
            "Cache and Decoder); both must be dry to stall."
        ),
        "typical_causes": [
            "Op Cache miss rate too high (hot code won't fit in 4K-op/core OC)",
            "Instruction cache miss chain from cold code regions",
            "Indirect branch mispredictions forcing Op Cache re-fills",
            "Large binary with poor code locality (many ITLB misses)",
        ],
        "tuning_hints": [
            "Profile with `perf record -e ex_ret_brn_ind_misp` to find indirect-branch sinks",
            "Apply PGO/BOLT to improve Op Cache hit rate (4K µops/core on Zen 4)",
            "Keep Redis hot command handlers small enough to fit in the OC",
            "Enable transparent hugepages for code (/sys/kernel/mm/transparent_hugepage/enabled)",
            "AMDuProfPcm -m l1 exposes IC miss ratio + Op Cache miss ratio separately",
        ],
        "level": 1,
        "parent": None,
    },

    "Bad_Speculation": {
        "description": (
            "Fraction of dispatch slots wasted on incorrectly speculated ops "
            "(branch mispredicts + pipeline restarts from memory ordering / "
            "self-modifying code / SMC clears). On Zen, the branch predictor "
            "is a TAGE-L-style predictor — unpredictable branches + indirect "
            "calls hurt most."
        ),
        "typical_causes": [
            "Unpredictable data-dependent branches (hash-table probes, polymorphic dispatch)",
            "Indirect calls via function pointers (C++ vtables, Redis command dispatch)",
            "Frequent context switches polluting branch history",
            "Self-modifying code triggering machine clears",
        ],
        "tuning_hints": [
            "Replace unpredictable branches with branchless code (`cmov`, AVX-512 masks)",
            "Sort workload data where possible to make branches more predictable",
            "Use `__builtin_expect` / `likely()` / `unlikely()` for skewed branches",
            "Minimise indirect calls in hot paths — Redis command dispatch is indirect; inline fast paths",
            "`amd-uprof` timer-based profile + `perf record -e ex_ret_brn_misp` pinpoints misprediction hotspots",
        ],
        "level": 1,
        "parent": None,
    },

    "Backend_Bound": {
        "description": (
            "Fraction of dispatch slots stalled by the backend (execution "
            "units + load/store queue + register file). On Zen this splits "
            "into Memory (cache/DRAM latency) and CPU (execution-port / "
            "dispatch-window contention)."
        ),
        "typical_causes": [
            "L3 misses hitting DRAM (cross-CCD traffic on multi-CCD EPYC)",
            "Load/store queue saturation under heavy pipelined ops",
            "Register pressure on AVX-heavy kernels",
            "InfinityFabric bandwidth contention on NUMA-crossing accesses",
        ],
        "tuning_hints": [
            "Pin Redis to a single CCD on multi-CCD EPYC (check `numactl -H` / `lscpu -e`)",
            "Reduce cross-CCX/cross-CCD traffic: keep allocator + thread on same CCD",
            "Lower memory pressure: tune jemalloc / use smaller object footprints",
            "On Zen 4+, check AMDuProfPcm -m memory for InfinityFabric bandwidth saturation",
        ],
        "level": 1,
        "parent": None,
    },

    "Retiring": {
        "description": (
            "Fraction of dispatch slots that retired useful ops. Higher is "
            "better. On Zen, retirement width is 8 (Zen 4) or 6 (Zen 3). "
            "High Retiring with low IPC typically indicates µop-heavy code "
            "(microcoded instructions like string-ops, wide AVX)."
        ),
        "typical_causes": [
            "Generally a positive signal — workload is throughput-bound on execution",
            "If IPC is low despite high Retiring: check Retiring.Microcode",
        ],
        "tuning_hints": [
            "Stress-test with higher client concurrency to find the real bottleneck",
            "If already Retiring-heavy at target RPS, further gains need algorithmic work",
            "Consider AVX-512 or VNNI for compute-bound paths on Zen 4 (Genoa supports AVX-512)",
        ],
        "level": 1,
        "parent": None,
    },

    # ===================================================================
    # LEVEL 2 — Frontend_Bound
    # ===================================================================

    "Frontend_Bound.Fetch_Latency": {
        "description": (
            "Frontend stalls dominated by fetch latency (I-cache / Op Cache "
            "miss → L2/L3 miss chain). On AMD this is AMDuProfPcm's "
            "``Frontend_Bound.Latency`` column."
        ),
        "typical_causes": [
            "Hot code doesn't fit in Op Cache (4K µops on Zen 4)",
            "Cold function calls forcing I-cache re-fills from L2/L3",
            "ITLB misses on large binaries",
        ],
        "tuning_hints": [
            "Enable transparent hugepages for code segments",
            "Apply PGO/BOLT to improve code layout + hot/cold separation",
            "Check AMDuProfPcm -m l1 for OC/IC miss ratios",
        ],
        "level": 2,
        "parent": "Frontend_Bound",
    },

    "Frontend_Bound.Fetch_Bandwidth": {
        "description": (
            "Frontend stalls dominated by insufficient fetch bandwidth — "
            "decoder/Op Cache can supply ops but not fast enough. Lower "
            "severity than Fetch_Latency typically. AMD column: "
            "``Frontend_Bound.BW``."
        ),
        "typical_causes": [
            "Long instruction sequences not captured by Op Cache (>4K µops in hot loop)",
            "Complex branches forcing decoder re-alignment",
        ],
        "tuning_hints": [
            "Reduce hot-loop size so the 4K-µop Op Cache is sufficient",
            "Minimise instruction-mix churn in the hot path",
        ],
        "level": 2,
        "parent": "Frontend_Bound",
    },

    # ===================================================================
    # LEVEL 2 — Bad_Speculation
    # ===================================================================

    "Bad_Speculation.Branch_Mispredicts": {
        "description": (
            "Dispatch slots wasted by branch mispredictions. AMD column: "
            "``Bad_Speculation.Mispredicts``. Redis workloads often show "
            "this on hash-table probes and object-encoding switches."
        ),
        "typical_causes": [
            "Data-dependent branches on low-entropy inputs",
            "Indirect calls with many targets (Redis command dispatch, modules)",
        ],
        "tuning_hints": [
            "Branchless code (`cmov`, bit tricks) replaces unpredictable branches",
            "Inline Redis fast paths (e.g., `lookupKey` hot path) to reduce indirect calls",
            "AVX-512 mask instructions avoid explicit branches in scanning code",
        ],
        "level": 2,
        "parent": "Bad_Speculation",
    },

    "Bad_Speculation.Machine_Clears": {
        "description": (
            "Pipeline restarts from memory ordering violations, SMC, or "
            "reset handlers. AMD column: ``Bad_Speculation.Pipeline_Restarts``. "
            "Typically very small (<1%) in well-behaved workloads — a spike "
            "often points at false sharing."
        ),
        "typical_causes": [
            "False sharing on shared cache lines (cross-thread atomic ops)",
            "Self-modifying code / JIT engines regenerating nearby code",
            "Uncached accesses / MMIO in hot paths",
        ],
        "tuning_hints": [
            "Pad shared atomic counters to 64B / 128B alignment (Zen 4 prefetch line = 64B)",
            "AMDuProfPcm -m l3 shows 'Ave L3 Miss Latency' — spikes correlate with coherency traffic",
            "For Redis, check counters touched by I/O threads vs main thread",
        ],
        "level": 2,
        "parent": "Bad_Speculation",
    },

    # ===================================================================
    # LEVEL 2 — Backend_Bound
    # ===================================================================

    "Backend_Bound.Memory_Bound": {
        "description": (
            "Backend stalls from data-supply bottlenecks (L1/L2/L3/DRAM). "
            "AMD column: ``Backend_Bound.Memory``. On multi-CCD EPYC this "
            "includes cross-CCD L3 traffic, which has distinctly higher "
            "latency than intra-CCD L3."
        ),
        "typical_causes": [
            "L3 misses served from DRAM (high NUMA / cross-socket cost)",
            "Cross-CCD cache line probes on multi-CCD EPYC (Genoa up to 12 CCDs)",
            "Load queue saturation from dependent memory chains",
        ],
        "tuning_hints": [
            "Pin Redis + allocator to a single CCD using `numactl --cpunodebind`",
            "Reduce dataset footprint: smaller objects → more L3 hits",
            "Check InfinityFabric throughput with AMDuProfPcm -m memory",
            "Consider `amd-pstate` in schedutil mode to keep cores at high P-state under load",
        ],
        "level": 2,
        "parent": "Backend_Bound",
    },

    "Backend_Bound.Core_Bound": {
        "description": (
            "Backend stalls from execution-resource contention (execution "
            "ports, register file, scheduler entries) rather than memory. "
            "AMD column: ``Backend_Bound.CPU``. Often indicates "
            "ILP-limited code."
        ),
        "typical_causes": [
            "Long dependency chains in scalar code",
            "Port contention on integer/FP ops",
            "Division / square-root latency",
        ],
        "tuning_hints": [
            "Unroll hot loops to expose more ILP",
            "Vectorize with AVX2/AVX-512 (Zen 4 supports double-pumped 512-bit)",
            "Replace expensive divisions with reciprocal multiply + correction",
            "Split long dependency chains into parallel accumulators (classic reduction trick)",
        ],
        "level": 2,
        "parent": "Backend_Bound",
    },

    # ===================================================================
    # LEVEL 2 — Retiring
    # ===================================================================

    "Retiring.Light_Operations": {
        "description": (
            "Useful work from fast-path (single-µop) instructions. AMD "
            "column: ``Retiring.Fastpath``. This is the healthy majority "
            "of Retiring for most Redis workloads."
        ),
        "typical_causes": [
            "Normal throughput-bound execution — a positive signal",
        ],
        "tuning_hints": [
            "If already-high Retiring.Light_Operations at target RPS, reach for algorithmic wins",
        ],
        "level": 2,
        "parent": "Retiring",
    },

    "Retiring.Heavy_Operations": {
        "description": (
            "Useful work from microcoded (multi-µop) instructions. AMD "
            "column: ``Retiring.Microcode``. Elevated when code uses "
            "string-ops (`rep movsb`), gather/scatter, or complex AVX-512."
        ),
        "typical_causes": [
            "Heavy use of `rep movsb` / `memcpy` microcoded paths",
            "Gather/scatter AVX-512 on poorly-aligned data",
            "Complex division / IDIV instructions",
        ],
        "tuning_hints": [
            "Replace small `memcpy` with inline loads/stores where profitable",
            "Align gather targets to 64B to take the fast path",
            "Prefer multiply-reciprocal over IDIV for constant divisors",
        ],
        "level": 2,
        "parent": "Retiring",
    },
}


# ---------------------------------------------------------------------------
# Helper functions (parallel to metrics.py API)
# ---------------------------------------------------------------------------


def get_metric_info(metric_name: str) -> Optional[Dict[str, Any]]:
    """Look up an AMD TMA metric by exact or partial (leaf) name.

    Same semantics as ``metrics.get_metric_info``.
    """
    if metric_name in AMD_METRICS_KB:
        return AMD_METRICS_KB[metric_name]
    matches: Dict[str, Dict[str, Any]] = {}
    suffix = f".{metric_name}"
    for key, value in AMD_METRICS_KB.items():
        if key == metric_name or key.endswith(suffix):
            matches[key] = value
    if len(matches) == 1:
        return next(iter(matches.values()))
    elif len(matches) > 1:
        return matches
    return None


def list_all_metrics() -> List[str]:
    """Return a sorted list of all AMD TMA metric names."""
    return sorted(AMD_METRICS_KB.keys())


def get_children(metric_name: str) -> List[str]:
    """Return the direct children of a given node in the AMD KB."""
    full_paths: List[str] = []
    if metric_name in AMD_METRICS_KB:
        full_paths.append(metric_name)
    else:
        suffix = f".{metric_name}"
        for key in AMD_METRICS_KB:
            if key.endswith(suffix):
                full_paths.append(key)
    children: List[str] = []
    for fp in full_paths:
        depth = fp.count(".") + 1
        for key in AMD_METRICS_KB:
            if key.startswith(fp + ".") and key.count(".") == depth:
                children.append(key)
    return sorted(set(children))
