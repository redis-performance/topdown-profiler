# topdown-profiler

Intel Top-Down Microarchitecture Analysis (TMA) collector with MCP server, label-based querying, and pluggable SQL backends.

Wraps [pmu-tools/toplev](https://github.com/andikleen/pmu-tools) to collect, store, and query CPU performance data — like [Polar Signals](https://www.polarsignals.com/) but for hardware performance counters.

[![CI](https://github.com/redis-performance/topdown-profiler/actions/workflows/ci.yml/badge.svg)](https://github.com/redis-performance/topdown-profiler/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/topdown-profiler)](https://pypi.org/project/topdown-profiler/)

## What is Top-Down Microarchitecture Analysis?

TMA classifies every CPU pipeline slot into four categories that sum to 100%:

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

This tool collects that data, stores it with labels (branch, test name, topology, etc.), and lets you query it from the CLI or via AI assistants through MCP.

## Install

```bash
pip install topdown-profiler

# Or from source
git clone https://github.com/redis-performance/topdown-profiler.git
cd topdown-profiler
poetry install
```

### Prerequisites

- Linux with `perf` tools installed
- Intel CPU (Sandy Bridge or newer)
- [pmu-tools](https://github.com/andikleen/pmu-tools) installed (`pip install pmu-tools`)
- `perf_event_paranoid <= 1` (or run as root)

```bash
# Check permissions
cat /proc/sys/kernel/perf_event_paranoid
# If > 1, fix with:
sudo sysctl kernel.perf_event_paranoid=1
```

## Quick Start

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

## Microarchitecture Analysis Example

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
#   Frontend_Bound              12.3%  █████ ✗
#     Fetch_Latency              8.1%  ███ ✗
#       ICache_Misses            3.2%  █ ✗
#       Branch_Resteers          3.8%  █ ✗
#     Fetch_Bandwidth            4.2%  █ ✗
#   Bad_Speculation              8.5%  ███ ✗
#     Branch_Mispredicts         6.2%  ██ ✗
#   Backend_Bound               48.0%  ███████████████████ ✗
#     Memory_Bound              32.1%  ████████████ ✗
#       L1_Bound                 5.3%  ██ ✗
#       L3_Bound                12.8%  █████ ✗
#       DRAM_Bound               8.7%  ███ ✗
#       Store_Bound              3.1%  █ ✗
#     Core_Bound                15.9%  ██████ ✗
#       Ports_Utilization       13.2%  █████ ✗
#   Retiring                    31.2%  ████████████ ✓

# 4. The workload is Backend_Bound (48%) → Memory_Bound (32%) → L3_Bound (12.8%)
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

# Comparison: 7f3a2b1c vs 9e4d5f6a
#
# Regressions (1):
#   ↑ Frontend_Bound: 12.3% -> 14.1% (+1.8%)
# Improvements (3):
#   ↓ Backend_Bound.Memory_Bound.L3_Bound: 12.8% -> 7.2% (-5.6%)
#   ↓ Backend_Bound.Core_Bound: 15.9% -> 11.3% (-4.6%)
#   ↑ Retiring: 31.2% -> 38.5% (+7.3%)   ← more useful work!

# 7. Which of your benchmarks are DRAM-bound?
topdown query --bottleneck DRAM_Bound --min-pct 10

# Runs where DRAM_Bound >= 10%:
#   RUN ID       | VALUE  | PROCESS       | LABELS
#   7f3a2b1c     | 18.7%  | redis-server  | test_name=hset-hget, topology=oss-cluster
#   3c8d9e2f     | 12.1%  | redis-server  | test_name=zadd-zrange, topology=oss-standalone
```

## Labels

Every run is tagged with auto-detected system labels plus user-supplied benchmark labels:

### Auto-detected (zero config)
`arch`, `kernel_version`, `node`, `cpu`, `pmu_name`, `platform`, `comm`, `pid`, `toplev_level`, `pmu_tools_version`

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

## Knowledge Base

120+ TMA metrics with descriptions, causes, and tuning hints covering Intel Skylake through Panther Lake:

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
