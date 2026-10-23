"""CPU profiling methodology and native platform measurement limits."""

TMA_METHODOLOGY = """\
# Intel Top-Down Microarchitecture Analysis (TMA) Methodology

## Overview

TMA is a structured approach to CPU performance analysis developed by Intel. It classifies \
CPU pipeline slot utilization into a hierarchy of categories, enabling engineers to quickly \
identify where performance is lost and focus optimization efforts.

## The Pipeline Slot Model

A modern CPU can issue multiple micro-operations (uops) per cycle. Each potential uop slot in \
each cycle is classified into one of four top-level categories. These four categories always \
sum to approximately 100% of pipeline slots.

## Level 1: The Four Categories

### Frontend Bound
Pipeline stalls because the Frontend (instruction fetch/decode) cannot supply enough uops to \
the Backend. The CPU is starved for instructions.

**Common causes:** I-cache misses, ITLB misses, branch misprediction resteer penalty, \
inefficient code layout.

### Bad Speculation
Pipeline slots wasted because the CPU speculated incorrectly. Work was done but had to be \
thrown away.

**Common causes:** Branch mispredictions, machine clears (memory ordering violations, \
self-modifying code).

### Backend Bound
Pipeline stalls because the Backend (execution units, memory subsystem) cannot process uops \
fast enough. The Frontend is supplying uops but they cannot be consumed.

**Sub-categories:**
- **Memory Bound:** Stalls from the memory hierarchy (L1/L2/L3 cache misses, DRAM latency)
- **Core Bound:** Stalls from execution unit limitations (port contention, divider, serialization)

### Retiring
Pipeline slots where useful work was done — uops were issued and eventually retired. Higher \
is better. A perfectly efficient workload would show 100% Retiring.

**Sub-categories:**
- **Light Operations:** Single-uop instructions (efficient)
- **Heavy Operations:** Multi-uop instructions, microcode assists (less efficient)

## How to Read Results

1. **Start at Level 1:** Which category dominates? A healthy workload typically has Retiring > 50%.
2. **Drill down:** If Backend_Bound is high, check Memory_Bound vs Core_Bound.
3. **Go deeper:** If Memory_Bound is high, check L1/L2/L3/DRAM to find which cache level is the bottleneck.
4. **Compare:** Run the same workload with different configurations and compare the TMA breakdown.

## Level Summary

| Level | Nodes | What it tells you |
|-------|-------|-------------------|
| 1     | 4     | Where are slots going? Frontend/Backend/Speculation/Retiring |
| 2     | 8     | Sub-categories within each L1 category |
| 3     | ~30   | Specific bottleneck types (e.g., L3_Bound, Branch_Mispredicts) |
| 4     | ~45   | Root causes (e.g., DTLB_Load, Store_Fwd_Blk) |
| 5-6   | ~30   | Very specific sub-causes (e.g., Load_STLB_Miss_4K) |

## Important Notes

- **Multiplexing:** PMU counters are limited. Deeper levels require multiplexing, which \
introduces measurement error. Use `--no-multiplex` or `--drilldown` to mitigate.
- **pmu-tools/toplev is Intel-only** (Sandy Bridge and newer). ``perf stat --topdown`` is \
available on both Intel and ARM Neoverse. AMD has a similar but different methodology.
- **System load matters:** Run benchmarks in a controlled environment. Background load will \
affect results.
- **Multiple runs:** Always take multiple measurements and look at averages to account for \
variance.

## ARM Neoverse Support

ARM Neoverse V1 (Graviton3) and V2 (Graviton4) support the same four L1 TMA categories \
via ``perf stat --topdown``:

| Aspect | Intel (pmu-tools/toplev) | ARM Neoverse (perf stat) |
|--------|--------------------------|--------------------------|
| Tool | pmu-tools/toplev.py | perf stat --topdown |
| L1 categories | Frontend_Bound, Bad_Speculation, Backend_Bound, Retiring | Same |
| Deeper levels | L2-L6 (30+ metrics) | L1 only on most Neoverse generations |
| Kernel requirement | Linux 4.x+ | Linux 5.15+ with ARM PMU support |

- **L1 only:** ARM Neoverse does not expose deeper TMA levels via ``perf stat --topdown``. \
The four L1 categories provide the high-level breakdown of where pipeline slots go.
- **Same analysis tools:** The ``explain``, ``query``, ``compare``, and ``funnel`` commands \
work identically on ARM L1 data.
- **Auto-detected on Linux:** The tool automatically selects ``perf stat --topdown`` on aarch64 and \
``toplev`` on x86_64. Override with ``TOPDOWN_COLLECTOR`` environment variable.

## Apple Silicon and macOS

Apple M1/M6 measurements use native macOS collectors. They are not Intel slot TMA \
or Linux ``perf stat --topdown`` data. Preserve their native scope and denominator:

| Measurement | Scope and interpretation |
|-------------|--------------------------|
| ``stat`` | All threads and user/kernel execution of one PID, excluding descendants; process-lifetime totals or observed PID-interval deltas. |
| Stack samples | Function frequency among recorded samples; sample shares are not CPU-time percentages. |
| Guided CPU Counters | Apple's named ratios weighted by sampled core cycles; retain valid-interval and cycle coverage. |
| Processor trace | Decoded execution intervals, native self counters and trace gaps; incomplete decoding is not full execution accounting. |
| SoC firmware | System/cluster observations with native units, including unrelated processes; not PID-attributed stalls or exact DRAM throughput. |

Use ``apple_doctor`` to inspect the server host, ``collect_apple_profile`` to capture, \
and ``import_apple_result`` to store an existing native artifact. Read summaries and \
ordered observation pages with ``query_apple_result``; use ``get_apple_hotspots`` for \
sampled-stack reports. Collection runs on the MCP server host; stored queries and \
reports also work offline on other supported operating systems.

Availability depends on the chip, kernel and toolchain. Installed templates do not \
verify counter access. Unknown coverage remains unavailable rather than becoming \
zero measured work. Apple records keep Intel TMA roots null and create no Linux \
TMA samples; use their native tools instead of Linux funnels and metric comparisons.
Native process CPU times retain raw Mach counters and their timebase. Nanosecond \
values are independently floored, so converted split sums can differ by 1 ns; \
raw instruction, cycle and Mach-time pairs remain exact. PID interval differences \
must match their before/after snapshots.

### Apple GPU Replay

Use ``collect_apple_gpu_profile`` for the packaged Metal control or \
``profile_apple_gpu_trace`` for an existing capture, then ``get_apple_gpu_report`` \
to rank measured encoders. Query raw ``encoders`` pages with ``query_apple_result``.
These measurements describe replay of a captured Metal workload, not original \
application wall time. Native encoder cost is profiler attribution, not duration \
divided by total GPU time. Limiter percentages overlap, can exceed 100%, and must \
not be normalized into pipeline-slot shares. Preserve sampled-core coverage, \
native units, replay settings and recorded host/tool provenance when comparing.
Timeline hardware counters can include other GPU activity and are not exclusive \
to captured encoders. A consistent-state flag does not certify workload isolation. \
Keep native definitions and distinguish unobserved counters from measured zero; \
missing per-encoder limiters remain unavailable.
The separate M1 Instruments sampled-counter experiment has a device-wide scope \
and different denominators; it is not interchangeable with replay costs.
"""


def get_methodology() -> str:
    return TMA_METHODOLOGY
