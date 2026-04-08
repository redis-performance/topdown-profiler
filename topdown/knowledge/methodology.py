"""Intel TMA methodology overview text."""

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
- **Intel only:** TMA is specific to Intel CPUs (Sandy Bridge and newer). AMD has a similar \
but different methodology.
- **System load matters:** Run benchmarks in a controlled environment. Background load will \
affect results.
- **Multiple runs:** Always take multiple measurements and look at averages to account for \
variance.
"""


def get_methodology() -> str:
    return TMA_METHODOLOGY
