# topdown-profiler

CPU profiling for Intel, AMD Zen, ARM Neoverse, and Apple silicon (M1/M6), plus native Apple GPU analysis. Linux Top-Down Microarchitecture Analysis (TMA) and native Apple results share SQL storage, run labels, and an MCP server; Apple measurements retain their native counters and categories.

On Linux, wraps [pmu-tools/toplev](https://github.com/andikleen/pmu-tools) on Intel, [AMD uProf](https://www.amd.com/en/developer/uprof.html) (`AMDuProfPcm`) on AMD Zen, or `perf stat --topdown` on ARM Neoverse to collect, store, and query CPU performance data — like [Polar Signals](https://www.polarsignals.com/) but for hardware performance counters.

**Apple silicon:** [M1 and M6 examples](#apple-silicon-m1m6) cover native
macOS profiling. M1 provides Apple's four-category CPU bottleneck view; M6
provides process counters, sampled stacks, execution traces, and system memory context.

[![CI](https://github.com/redis-performance/topdown-profiler/actions/workflows/ci.yml/badge.svg)](https://github.com/redis-performance/topdown-profiler/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/topdown-profiler)](https://pypi.org/project/topdown-profiler/)

## What is Top-Down Microarchitecture Analysis?

The Linux slot-TMA view classifies CPU pipeline slots into four categories that sum to 100%:

```
Pipeline Slots (100%)
├── Frontend_Bound    15.2%  ███████         Instruction supply problems
├── Bad_Speculation   10.1%  █████           Branch mispredictions, machine clears
├── Backend_Bound     44.6%  ██████████████  Data supply / execution bottlenecks
│   ├── Memory_Bound  30.2%  ███████████     Cache misses, DRAM latency
│   │   ├── L1_Bound   5.1%  ██
│   │   ├── L3_Bound  12.4%  ██████
│   │   └── DRAM_Bound 8.3%  ████
│   └── Core_Bound    14.4%  ███████         Port contention, dividers
└── Retiring          30.1%  ███████████     Useful work (higher = better)
```

The Linux collectors store that data with labels (branch, test name, topology,
etc.) for CLI and MCP queries. Apple M1 uses native Delivery, Discarded,
Processing, and Useful categories; see the [M1 and M6 examples](#apple-silicon-m1m6).

## Install

```bash
pip install topdown-profiler

# Or from source
git clone https://github.com/redis-performance/topdown-profiler.git
cd topdown-profiler
poetry install
```

### Prerequisites

The following prerequisites apply to Linux TMA. For macOS, see
[Apple silicon (M1/M6)](#apple-silicon-m1m6).

- Linux with `perf` tools installed
- Intel CPU (Sandy Bridge or newer), AMD Zen (EPYC 7xx1 / 7xx2 / 7xx3 / 9xx4), **or** ARM Neoverse (Graviton3/4)
- Per-vendor tooling (see below)
- `perf_event_paranoid <= 1` (or run as root)

```bash
# Check permissions
cat /proc/sys/kernel/perf_event_paranoid
# If > 1, fix with:
sudo sysctl kernel.perf_event_paranoid=1
```

### Intel Prerequisites

- [pmu-tools](https://github.com/andikleen/pmu-tools) installed (`pip install pmu-tools`)
- Optional: export `TOPDOWN_TOPLEV_PATH=/path/to/toplev.py` if not on `PATH`
- L1-L4 TMA metrics (30+ metrics across Skylake/Ice Lake/Sapphire Rapids)

### AMD Zen Prerequisites

Two collectors are supported; the tool picks the best available automatically:

**Preferred — AMD uProf (L1+L2 pipeline utilization):**
- [AMD uProf](https://www.amd.com/en/developer/uprof.html) installed (`.deb`, `.rpm`, or `.tar.bz2`)
- `AMDuProfPcm` binary on `PATH` (or export `TOPDOWN_UPROF_PCM_PATH=/opt/AMDuProf_X.Y-ZZZ/bin/AMDuProfPcm`)
- Root typically required for system-wide collection (IBS / PMU driver access)
- Richest metric coverage on Zen 4+ (EPYC 9xx4 Genoa/Bergamo/Turin); older Zen families produce a subset
- Uses `AMDuProfPcm -m pipeline_util -a -d <sec> -o <csv>` under the hood
- L1 + L2 categories (Frontend/Backend/Bad_Spec/Retiring + sub-categories)

```bash
# Install (download requires accepting AMD's EULA on the website)
wget <URL-from-https://www.amd.com/en/developer/uprof.html>
tar -xjf AMDuProf_Linux_x64_*.tar.bz2 -C /opt
export PATH="/opt/AMDuProf_Linux_x64_*/bin:$PATH"
AMDuProfPcm --help  # sanity check
```

**Fallback — stock `perf stat -e <zen-events>` (L1 only, no extra install):**
- Activates automatically on AMD hosts when AMDuProfPcm is missing
- Uses Zen 4/5 PMU events (`de_src_op_disp.all`, `ex_ret_ops`, `ex_ret_brn_misp`,
  `de_no_dispatch_per_slot.no_ops_from_frontend`, `.backend_stalls`, `cpu-cycles`)
- Linux kernel 6.7+ for Zen 4 events; 6.10+ for Zen 5 (EPYC 9R45)
- `perf_event_paranoid <= -1` for `--system-wide` (or run as root)
- Bad_Speculation is best-effort (AMD exposes no "mis-speculated slots" counter)

### ARM Neoverse Prerequisites

- Linux kernel 5.15+ with ARM PMU perf support
- `perf` tools installed (`apt install linux-tools-$(uname -r)` or `yum install perf`)
- `perf_event_paranoid <= 1` (same as Intel)
- No pmu-tools required — uses `perf stat --topdown` directly
- L1 topdown metrics only (Frontend_Bound, Backend_Bound, Bad_Speculation, Retiring)

### Collector auto-detection

| CPU vendor / arch | Default collector | Override env var |
|---|---|---|
| `x86_64` + `GenuineIntel` | `toplev` | `TOPDOWN_COLLECTOR=toplev` |
| `x86_64` + `AuthenticAMD` (uProf installed) | `uprof_pcm` | `TOPDOWN_COLLECTOR=uprof_pcm` |
| `x86_64` + `AuthenticAMD` (no uProf) | `perf_stat_amd` | `TOPDOWN_COLLECTOR=perf_stat_amd` |
| Linux `aarch64` | `perf_stat` | `TOPDOWN_COLLECTOR=perf_stat` |
| macOS `arm64` | Native Apple CPU/GPU profiles; M1 guided CPU bottlenecks; M3+ GPU replay | — |

`/proc/cpuinfo` `vendor_id` drives the Intel vs AMD split on x86_64.
On AMD, the presence of `AMDuProfPcm` in `PATH` (or `TOPDOWN_UPROF_PCM_PATH`)
decides between `uprof_pcm` and the `perf_stat_amd` fallback.

### Troubleshooting

**`No samples collected.`** — check `kernel.perf_event_paranoid`:
```bash
cat /proc/sys/kernel/perf_event_paranoid
# For --system-wide AMD collection, need -1:
sudo sysctl -w kernel.perf_event_paranoid=-1
```

**AMDuProfPcm not found on AMD host** — tool auto-falls back to `perf_stat_amd`.
Force uprof if you know it's installed:
```bash
export TOPDOWN_UPROF_PCM_PATH=/opt/AMDuProf_Linux_x64_X.Y.Z/bin/AMDuProfPcm
```

**`Topdown requested but the topdown metric groups aren't present`** — means
`perf stat --topdown` was invoked on AMD. Shouldn't happen now; if it does,
force the right collector: `TOPDOWN_COLLECTOR=perf_stat_amd`.

**Zen 5 events missing from `perf list`** — kernel older than 6.10. Upgrade
kernel or use `TOPDOWN_COLLECTOR=perf_stat_amd` anyway (raw event numbers
fall through) — or install AMDuProfPcm which has its own event knowledge.

## Quick Start (Linux)

### Collect

Profile a process by name (not PID) with benchmark labels:

```bash
topdown collect --process redis-server --level 3 --duration 30s \
  --label git_branch=unstable \
  --label git_hash=abc123 \
  --label test_name=set-get-100 \
  --label topology=oss-standalone \
  --label client_tool=memtier \
  --label build_variant=release
```

### Query

```bash
# What are the bottlenecks for this branch?
topdown query --label git_branch=unstable --bottlenecks

# VTune-style pipeline funnel (where do 100% of slots go?)
topdown query --funnel --label git_branch=unstable --label test_name=set-get-100

# Which benchmarks are DRAM-bound above 15%?
topdown query --bottleneck DRAM_Bound --min-pct 15

# Full TMA tree for a specific run
topdown query --run-id <id> --tree
```

### Compare

```bash
# Compare two runs by ID
topdown compare <run-id-a> <run-id-b>

# Compare release vs debug by labels
topdown compare --label-a build_variant=release --label-b build_variant=debug
```

### Explain

Every TMA metric has built-in descriptions, typical causes, and tuning hints:

```bash
topdown explain DRAM_Bound
```

```
╭──────────────── Description ────────────────╮
│ Backend_Bound.Memory_Bound.DRAM_Bound       │
│                                             │
│ Stalls caused by loads missing all cache    │
│ levels and going to main memory (DRAM).     │
│ Latency is typically 60-120ns (local) or    │
│ 150-300ns (remote NUMA).                    │
╰─────────────────────────────────────────────╯
╭──────────────── Typical Causes ─────────────╮
│   - Working set exceeding LLC capacity      │
│   - Random access to large hash tables      │
│   - Pointer-chasing with poor locality      │
│   - NUMA remote memory accesses             │
╰─────────────────────────────────────────────╯
╭──────────────── Tuning Hints ───────────────╮
│   - Use numactl --membind to keep data      │
│     local                                   │
│   - Configure THP for large Redis instances │
│   - Pin io-threads to same NUMA node        │
│   - Drill into MEM_Bandwidth vs             │
│     MEM_Latency                             │
╰─────────────────────────────────────────────╯
```

## Apple silicon (M1/M6)

Use `topdown apple` on macOS. Choose the diagnostic available on your chip:

| Chip | Bottleneck view | Other measurements |
|---|---|---|
| M1 | Delivery / Discarded / Processing / Useful, verified with Xcode 27.1 | Process counters, fixed-counter samples with stacks, experimental event groups |
| M6 | Four-category breakdown unavailable on the tested OS | Process counters, fixed-counter samples with stacks, execution traces, system/cluster stall and bandwidth histograms |

`stat` and M6 `soc` need neither root nor Xcode. Instruments captures need Xcode
tools; fixed-counter and custom M1 modes also need full Xcode's `instrumentbuilder`.
Add `--store` to `apple stat`, `record`, or `soc` to save results to SQLite or
PostgreSQL with the same run IDs and labels used by Linux. Native Apple queries
and MCP tools read these results; `--funnel` remains a Linux slot-TMA view.
See the [Apple CLI guide](docs/apple-silicon.md#database-storage-and-mcp) for storage and MCP details.

```bash
# Capture and store; the output includes a run_id.
topdown apple stat --store --label test_name=apple-check \
  --output /tmp/apple-db-001 -- /path/to/program args

# Import an existing capture instead.
topdown apple store /tmp/apple-capture --process redis-server --label chip=M1
topdown apple query RUN_ID
# For stored stack profiles (fixed/cpu/time/M1 event modes):
topdown apple report --run-id RUN_ID
```

Both examples below use the included pointer-chasing workload. From a source
checkout, build it with Xcode's compiler tools:

```bash
xcrun clang -O2 -g examples/apple-m1-controls.c -o /tmp/apple-controls
```

Use a new output directory for every capture. Measurements vary with the chip,
toolchain, scheduling, and other system activity.

### M1 example: four-category CPU bottlenecks

Record the workload with Xcode 27.1's CPU Counters template:

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  topdown apple record --mode counters --duration 5 \
  --output /tmp/m1-memory-001 -- /tmp/apple-controls memory 40000000 8388608
```

The CLI saves `profile.json`. To display its native shares as percentages:

```bash
python3 - <<'PY'
import json
with open("/tmp/m1-memory-001/profile.json") as f:
    shares = json.load(f)["apple_bottlenecks"]["shares"]
for name, share in shares.items():
    print(f"{name.title():<12} {share:6.2%}")
PY
```

A [recorded M1 run](docs/apple-m1-xcode27-validation.json) of this workload gave
the following breakdown, formatted as a tree for comparison with the Linux example:

```text
Apple M1 CPU bottlenecks (~100%, rounded)
├── Delivery       0.07%                     Instruction supply limits
├── Discarded      0.01%                     Discarded speculative work
├── Processing    99.44%  ████████████████████ Execution / data dependency stalls
└── Useful         0.48%                     Work that makes progress
```

**Interpretation:** Processing dominates this dependent-load workload. Investigate
data locality, cache behavior, and pointer dependencies. This view alone does not
separate cache misses from DRAM stalls. In the same validation set, a random-branch
workload instead showed **57.48% Delivery** and **34.85% Discarded**.

These are [Apple's native categories](https://developer.apple.com/documentation/xcode/addressing-cpu-bottlenecks),
weighted by sampled core cycles. Intel slot-TMA equivalence remains unvalidated;
the JSON stores these shares under `apple_bottlenecks` and keeps `tma` null.

### M6 example: process counters and memory-stall context

Run a longer pointer chase, measuring process totals while sampling system/cluster
firmware observations for one second:

```bash
topdown apple stat --output /tmp/m6-memory-stat-001 \
  -- /tmp/apple-controls memory 200000000 8388608 > /tmp/m6-memory-stat-001.json &
stat_pid=$!
topdown apple soc --duration 1 --output /tmp/m6-memory-soc-001 \
  > /tmp/m6-memory-soc-001.json
wait "$stat_pid"
```

Inspect `counters` in `/tmp/m6-memory-stat-001/stat.json` and `channels` in
`/tmp/m6-memory-soc-001/soc.json`. A [recorded M6 run](docs/apple-m6-readme-example.json)
on macOS 27.0.1 gave this summary:

```text
Apple M6 — pointer-chasing workload
├── Process instructions       849,633,335
├── Process cycles          18,603,265,709
├── PACC0 DRAM Stall             4,230 native observations
│   ├── Bucket "32%"             1,122 observations (26.52% of channel total)
│   ├── Bucket "36%"               720 observations (17.02% of channel total)
│   └── Bucket "28%"               403 observations ( 9.53% of channel total)
│       Other buckets omitted
└── Four-category TMA         unavailable on the tested OS
```

**Interpretation:** the process totals describe one PID's work, including startup
and shutdown. The DRAM histogram supplies memory context for the whole cluster,
including other processes, over a separate interval. Bucket names such as `"32%"`
are native firmware labels; **they do not mean this process spent 32% of its time
stalled on DRAM**. The percentages beside the counts describe only the distribution
of reported observations.

The tested M6 OS blocks programmable events, so these measurements cannot yield
Delivery / Discarded / Processing / Useful or the Linux TMA funnel. For function
locations, use [fixed-counter stack samples or execution traces](docs/apple-silicon.md).

### M6 hotspot report

Check support, capture fixed-counter stacks, and rank the observed functions:

```bash
topdown apple doctor
topdown apple record --mode fixed --duration 5 \
  --output /tmp/m6-hotspots-001 -- /tmp/apple-controls memory 40000000 8388608
topdown apple report /tmp/m6-hotspots-001
```

`doctor` recognizes the measured M6 kernel blocker and suggests available capture
modes. The report shows leaf-sample shares and caller counts; these describe
recorded samples, with missing stacks reported separately. Counter values are
not summed, and the report does not infer CPU-time or TMA percentages. Use
`--limit 10 --json` for a structured report.

### M6 GPU top-down experiment

The [GPU experiment guide](docs/apple-gpu-experiment.md) adds a native replay
workflow: rank GPU passes, inspect Apple's limiters, change the kernel, and verify
the result. On this M6, reducing loop-control instructions preserves the checksum
and shifts the dominant limiter from Control Flow to F32 execution:

| Four-pass workload | Replay GPU time | Native limiter example |
|---|---:|---|
| Original compute loop | 0.2661 ms | Control Flow 71.31% |
| Same FMAs, unrolled loop | 0.0861 ms | F32 55.86% |

The guide includes M6 capture/report commands, matched 4/8-pass controls, memory
experiments and [independent FMA chains](docs/apple-gpu-experiment.md#m6-compute-control-independent-fma-chains),
with measurement evidence. Limiters overlap, and some M6 values
exceed 100%; they are retained and flagged. Use the packaged CLI with full Xcode
(M3/A17 Pro or later for replay):

```sh
topdown apple gpu record --mode alu --passes 4 --items 16384 \
  --output /tmp/m6-gpu-001 --store --label test_name=m6-gpu
topdown apple gpu report --run-id RUN_ID
topdown apple query RUN_ID --section encoders --limit 4
```

GPU results support SQLite/PostgreSQL and MCP collection, queries and reports.
Reports retain native bandwidth units, observed-counter coverage and sampled
cores. Timeline hardware counters can include concurrent GPU activity.
Use `apple gpu profile` for an existing `.gputrace`. The guide also preserves the
M1 Instruments counter experiment, including sampled ALU versus Buffer Write
limiter contrasts; the tested Xcode 27.1 GPU replay requires M3/A17 Pro or later.
From a source checkout on M1, use the checked Instruments path and its saved
sampled-counter report:

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
python3 examples/apple_gpu/record_instruments.py --probe-source packaged \
  --mode compute --output /tmp/m1-gpu-compute-001
python3 examples/apple_gpu/report_instruments.py \
  /tmp/m1-gpu-compute-001/profile.json
```

This experimental path reports device-wide samples with pass coverage and
observed other GPU activity; it does not produce M6 replay encoder costs.

## Microarchitecture Analysis Example (Linux)

Here is a real-world example analyzing redis-server under a memtier benchmark:

```bash
# 1. Start your benchmark
memtier_benchmark -s 127.0.0.1 -p 6379 --test-time=60 --threads=4 --clients=50 &

# 2. Collect Level 3 TMA data while the benchmark runs
topdown collect --process redis-server --level 3 --duration 30s \
  --label git_branch=unstable \
  --label git_hash=a1b2c3d \
  --label test_name=set-get-50-50 \
  --label topology=oss-standalone \
  --label client_tool=memtier \
  --label build_variant=release \
  --label compiler=gcc-13

# Output:
# Found 1 PID(s) for 'redis-server': [12345]
# Collecting level 3 data for 30s...
# Done. Run ID: 7f3a2b1c-...
#   Samples: 2340 | Duration: 30.2s
#   Labels: 18 (7 user-supplied)

# 3. View the pipeline funnel — where are CPU cycles going?
topdown query --funnel --label test_name=set-get-50-50

# Pipeline Slots Funnel (100% total)
#   Useful work (Retiring): 31.2%
#   Wasted:                 68.8%
#
#   Backend_Bound               48.0%                    ███████████████████ ✗
#     Memory_Bound              32.1%                    ████████████ ✗
#       L3_Bound                12.8% of Stalls           ✗
#       DRAM_Bound               8.7% of Stalls           ✗
#       L1_Bound                 5.3% of Stalls           ✗
#       Store_Bound              3.1% of Stalls           ✗
#     Core_Bound                15.9%                    ██████ ✗
#       Ports_Utilization       13.2% of Clocks           ✗
#   Retiring                    31.2%                    ████████████ ✓
#   Frontend_Bound              12.3%                    █████ ✗
#     Fetch_Latency              8.1%                    ███ ✗
#       Branch_Resteers          3.8% of Clocks           ✗
#       ICache_Misses            3.2% of Clocks           ✗
#     Fetch_Bandwidth            4.2%                    █ ✗
#   Bad_Speculation              8.5%                    ███ ✗
#     Branch_Mispredicts         6.2%                    ██ ✗
#
# Rows marked 'of Stalls' / 'of Clocks' are fractions of that denominator, not
# of pipeline slots (toplev reports them that way), so they get no bar.

# 4. The workload is Backend_Bound (48%) → Memory_Bound (32% of slots), and within
#    Memory_Bound the largest stall share is L3_Bound (12.8% of Stalls).
#    Let's understand what L3_Bound means:
topdown explain L3_Bound

# 5. Collect again after tuning (e.g., enabling io-threads)
topdown collect --process redis-server --level 3 --duration 30s \
  --label git_branch=unstable \
  --label test_name=set-get-50-50 \
  --label build_variant=release-io-threads-4

# 6. Compare the two configurations
topdown compare \
  --label-a build_variant=release \
  --label-b build_variant=release-io-threads-4 \
  --process redis-server

#   Comparison: A = 7f3a2b1c-0d4e-4f5a-9b6c-7d8e9f0a1b2c
#          vs B = 9e4d5f6a-1b2c-4d3e-8f9a-0b1c2d3e4f5a
#   Metric                                   A       B     Delta   Direction
#   Retiring                             31.2%   38.5%   +7.3 pp   ↑ improved
#   Backend_Bound.Memory_Bound           32.1%   26.5%   -5.6 pp   ↓ improved
#   Backend_Bound.Core_Bound             15.9%   11.3%   -4.6 pp   ↓ improved
#   Frontend_Bound                       12.3%   14.1%   +1.8 pp   ↑ regressed
#   Backend_Bound.Memory_Bound.L3_Bound  12.8%    7.2%   -5.6 pp   ↓ changed
#     of Stalls
#   Data quality: MUX 9.0% (lowest per-interval event-multiplexing ratio). ...
#
#   Only slot shares get a better/worse reading; L3_Bound is a share of stall
#   cycles, so it is listed but not judged.

# 7. Which of your benchmarks are DRAM-bound?
#    --bottleneck matches the TMA node name at any depth, ignoring case; if no
#    node has that name it falls back to a substring match and says so.
topdown query --bottleneck DRAM_Bound --min-pct 10

# Runs where DRAM_Bound >= 10.0% (in the node's own unit):
# 7f3a2b1c-0d4e-4f5a-9b6c-7d8e9f0a1b2c  18.7% of Stalls  Backend_Bound.Memory_Bound.DRAM_Bound
#     2026-04-08 15:57 redis-server; test_name=hset-hget, topology=oss-cluster
# 3c8d9e2f-5a6b-4c7d-8e9f-0a1b2c3d4e5f  12.1% of Stalls  Backend_Bound.Memory_Bound.DRAM_Bound
#     2026-04-08 16:02 redis-server; test_name=zadd-zrange, topology=oss-standalone
```

## Labels

Every run is tagged with auto-detected system labels plus user-supplied benchmark labels:

### Auto-detected (zero config)
`arch`, `kernel_version`, `node`, `cpu`, `pmu_name`, `platform`, `comm`, `pid`, `collector`, `tma_level`, `pmu_tools_version` (Intel) / `perf_version` (ARM)

### User-supplied (via `--label key=value`)
`git_branch`, `git_hash`, `build_variant`, `compiler`, `test_name`, `client_tool`, `topology`, `dataset_name`, `tested_commands`, `tested_groups`, `github_org`, `github_repo`, `role`, `coordinator_version`, `thread_name`

All labels are stored as JSON and queryable:

```bash
topdown list --label git_branch=unstable --label topology=oss-standalone
topdown query --label compiler=gcc-13 --bottlenecks
```

## Agent Mode (Continuous Collection)

Run as a daemon that collects periodically:

```bash
# Foreground
topdown agent --process redis-server --level 2 --every 5m --duration 30s

# Install as systemd service
sudo topdown install-service --process redis-server --level 2 --every 5m

# Preview the unit file without installing
topdown install-service --process redis-server --preview
```

## MCP Server (AI-Assisted Querying)

The MCP server lets Claude (or any MCP client) query your profiling data:

```bash
# Start MCP server (stdio for Claude Code/Desktop)
topdown mcp-serve

# HTTP transport for remote access
topdown mcp-serve --transport http --port 8000
```

### Claude Code / Claude Desktop config

Add to `.mcp.json` in your project or `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "topdown": {
      "command": "topdown",
      "args": ["mcp-serve"]
    }
  }
}
```

Then ask Claude:
- *"What's the top bottleneck for redis-server on branch unstable?"*
- *"Show me the pipeline funnel for test set-get-100"*
- *"Which benchmarks are DRAM-bound above 15%?"*
- *"Compare release vs debug builds for redis-server"*
- *"Explain what L3_Bound means and how to fix it"*

### MCP Tools

| Tool | Description |
|------|-------------|
| `collect_topdown` | Run a TMA collection for a process |
| `query_bottlenecks` | Find ranked CPU bottlenecks |
| `query_by_bottleneck` | Find runs matching a specific bottleneck |
| `get_funnel` | VTune-style pipeline slot funnel |
| `compare_runs` | Compare two runs by ID |
| `compare_by_labels` | Compare runs by label sets |
| `explain_metric` | Explain a TMA metric with tuning hints |
| `list_profiling_runs` | List recent runs |
| `apple_doctor` | Inspect Apple profiling support on the server host |
| `collect_apple_profile` | Capture and store native Apple counters, stacks, traces, or SoC observations |
| `import_apple_result` | Store an existing Apple JSON artifact from the server host |
| `query_apple_result` | Read native Apple summaries and paginated observations by run ID |
| `get_apple_hotspots` | Rank functions from stored Apple stack samples |
| `collect_apple_gpu_profile` | Capture a packaged Metal control and store GPU replay counters |
| `profile_apple_gpu_trace` | Replay and store an existing server-local Metal capture |
| `get_apple_gpu_report` | Rank stored GPU encoder costs and native limiters |

Apple collection runs on the MCP server's macOS host. Stored Apple data can be
queried from a server on any supported OS using the same database configuration.

## Storage Backends

### SQLite (default)

Zero configuration, stored at `~/.topdown/data.db`:

```bash
topdown collect --process redis-server --level 2 --duration 30s
```

### PostgreSQL

```bash
export TOPDOWN_BACKEND=postgresql
export TOPDOWN_DSN="postgresql://user:pass@host:5432/topdown"
topdown collect --process redis-server --level 2 --duration 30s
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOPDOWN_BACKEND` | Storage backend (`sqlite` or `postgresql`) | `sqlite` |
| `TOPDOWN_DSN` | PostgreSQL connection string | — |
| `TOPDOWN_DB_PATH` | SQLite database path | `~/.topdown/data.db` |
| `TOPDOWN_TOPLEV_PATH` | Path to toplev.py (Intel only) | `toplev.py` |
| `TOPDOWN_PMU_TOOLS_DIR` | pmu-tools directory (Intel only) | — |
| `TOPDOWN_COLLECTOR` | Collector backend: `toplev` (Intel), `perf_stat` (ARM), or auto-detect | auto |

## Knowledge Base

120+ TMA metrics with descriptions, causes, and tuning hints covering Intel Skylake through Panther Lake and ARM Neoverse L1:

```bash
topdown explain Frontend_Bound.Fetch_Latency.ICache_Misses
topdown explain Branch_Mispredicts
topdown explain Ports_Utilization
```

## CLI Reference

```
topdown collect         Collect TMA data for a process
topdown list            List recent profiling runs
topdown query           Query stored data (--bottlenecks, --tree, --funnel, --bottleneck)
topdown compare         Compare two runs (by ID or labels)
topdown explain         Explain a TMA metric
topdown agent           Continuous collection daemon
topdown install-service Install systemd service
topdown mcp-serve       Start MCP server
topdown version         Show version
```

## Development

```bash
git clone https://github.com/redis-performance/topdown-profiler.git
cd topdown-profiler
poetry install
make test
```

## License

MIT
