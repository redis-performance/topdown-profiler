# AGENTS.md — topdown-profiler MCP Agent

## Overview

topdown-profiler exposes an MCP (Model Context Protocol) server that enables AI assistants to collect, query, and analyze Intel Top-Down Microarchitecture Analysis (TMA) data.

## Setup

### Claude Code

Add to your project's `.mcp.json`:

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

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `~/.config/Claude/claude_desktop_config.json` (Linux):

```json
{
  "mcpServers": {
    "topdown": {
      "command": "topdown",
      "args": ["mcp-serve"],
      "env": {
        "TOPDOWN_DB_PATH": "/path/to/your/data.db"
      }
    }
  }
}
```

### HTTP Transport (remote/shared)

```json
{
  "mcpServers": {
    "topdown": {
      "command": "topdown",
      "args": ["mcp-serve", "--transport", "http", "--port", "8000"]
    }
  }
}
```

## Available Tools

### collect_topdown

Run a TMA collection for a process.

**Parameters:**
- `process_name` (str, required): Process name to profile (e.g. `redis-server`)
- `level` (int, default 2): TMA analysis level 1-6
- `duration_seconds` (int, default 30): Collection duration
- `system_wide` (bool, default false): Profile all CPUs
- `labels` (dict, optional): Labels like `{"git_branch": "unstable", "test_name": "set-get-100"}`

**Example prompt:** *"Profile redis-server for 30 seconds at level 3 with labels git_branch=unstable and test_name=set-get-100"*

### query_bottlenecks

Find ranked CPU bottlenecks from stored data.

**Parameters:**
- `process_name` (str, optional): Filter by process
- `labels` (dict, optional): Filter by labels
- `last_hours` (float, default 24): Time window
- `min_percentage` (float, default 5): Minimum threshold
- `top_n` (int, default 10): Max results

**Example prompt:** *"What are the top bottlenecks for redis-server on branch unstable?"*

### query_by_bottleneck

Find which benchmarks/runs hit a specific TMA bottleneck.

**Parameters:**
- `metric_name` (str, required): TMA metric (e.g. `DRAM_Bound`, `L3_Bound`)
- `min_pct` (float, default 5): Minimum percentage
- `labels` (dict, optional): Label filters
- `last_hours` (float, default 24): Time window

**Example prompt:** *"Which benchmarks are DRAM-bound above 15%?"*

### get_funnel

VTune-style pipeline slot funnel showing where 100% of CPU slots go.

**Parameters:**
- `run_id` (str, optional): Specific run
- `process_name` (str, optional): Filter by process
- `labels` (dict, optional): Filter by labels
- `level` (int, default 3): Max drill-down depth

**Example prompt:** *"Show me the pipeline funnel for redis-server running set-get-100"*

### compare_runs

Compare two profiling runs by ID.

**Parameters:**
- `run_id_a` (str, required): Baseline run ID
- `run_id_b` (str, required): Comparison run ID

**Example prompt:** *"Compare run abc123 with run def456"*

### compare_by_labels

Compare latest runs matching two different label sets.

**Parameters:**
- `label_a` (dict, required): Baseline labels (e.g. `{"build_variant": "release"}`)
- `label_b` (dict, required): Comparison labels (e.g. `{"build_variant": "debug"}`)
- `process_name` (str, optional): Process filter

**Example prompt:** *"Compare release vs debug builds of redis-server"*

### explain_metric

Explain a TMA metric with description, typical causes, and tuning hints.

**Parameters:**
- `metric_name` (str, required): Full path or leaf name (e.g. `DRAM_Bound` or `Backend_Bound.Memory_Bound.DRAM_Bound`)

**Example prompt:** *"Explain what L3_Bound means and how to fix it"*

### list_profiling_runs

List recent profiling runs.

**Parameters:**
- `process_name` (str, optional): Filter by process
- `labels` (dict, optional): Filter by labels
- `last_hours` (float, default 24): Time window

**Example prompt:** *"Show me all profiling runs from the last 24 hours"*

## Available Resources

| URI | Description |
|-----|-------------|
| `topdown://runs/{run_id}/tree` | Full TMA hierarchy for a run |
| `topdown://metrics` | All 120+ known TMA metrics |
| `topdown://methodology` | Intel TMA methodology overview |

## Label System

Runs are tagged with auto-detected labels (arch, kernel, cpu, hostname) and user-supplied labels. The AI can filter queries by any combination:

- *"What are the bottlenecks for branch unstable on test set-get-100?"* → filters by `git_branch` + `test_name`
- *"Compare oss-standalone vs oss-cluster topology"* → filters by `topology`

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOPDOWN_DB_PATH` | SQLite database path | `~/.topdown/data.db` |
| `TOPDOWN_BACKEND` | Storage backend (`sqlite` or `postgresql`) | `sqlite` |
| `TOPDOWN_DSN` | PostgreSQL connection string | - |
| `TOPDOWN_TOPLEV_PATH` | Path to toplev.py | `toplev.py` |
