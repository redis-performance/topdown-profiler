# Optional Stax profiles

Stax adds sampled CPU stacks, scheduler waits and cooperating target spans to the
shared run/label workflow. These results use separate SQL documents and never
create Linux slot-TMA rows. Existing Apple Instruments and Metal replay commands
remain available.

Capture is experimental. Functional checks have run on a Linux aarch64 VM;
M1/M6 capture acceptance remains pending. Measure overhead on the workload being
investigated, especially when interpreting tail latency.

## Install and inspect

The adapter has no additional Python dependencies. Install Stax separately using
its [upstream instructions](https://stax.bearcove.eu/guide/getting-started/).
The tested source revision is `aedd85e08894fc3250ec975a486544d091e11281`.
It requires Rust 1.92 or newer and writes archive format v3, even though its CLI
help still describes v2. This adapter accepts v3 bundles and directory archives;
it rejects other versions explicitly.

For an isolated source build:

```bash
git clone https://github.com/bearcove/stax.git
cd stax
git checkout aedd85e08894fc3250ec975a486544d091e11281
cargo build --locked --release -p stax -p stax-server
export TOPDOWN_STAX_PATH="$PWD/target/release/stax"
export TOPDOWN_STAX_SERVER_PATH="$PWD/target/release/stax-server"
topdown stax doctor
```

The executables can also be on `PATH`. macOS additionally requires Stax's signed
privileged helper; `TOPDOWN_STAX_DAEMON_SOCKET` overrides `/var/run/staxd.sock`.
Linux capture requires permission for per-CPU, system-wide `perf_event_open`
events even when attaching to one PID. A successful per-process perf test does not
establish these permissions. The doctor reports
prerequisites and policy hints; it does not establish counter availability.

For the pinned macOS revision, use a root-owned helper socket inside a directory
owned by the profiling user with mode `0700`. The adapter rejects a shared socket
layout: that helper has no peer-UID check. Apply its session teardown guard patch
before use; the unpatched setup-error path can leave kperf/kdebug configured.
These are required operator setup steps. The [M1 pilot runbook and patch](https://github.com/redis-performance/topdown-profiler/blob/9e22cea348df35404eb7dcfade17eee15d2ec416/docs/stax-m1-pilot.md)
document the pinned workaround. Doctor checks filesystem ownership and permissions;
it cannot attest which helper binary is serving the socket.

M1 and M6 need independent capture validation on their installed kernels. A
helper socket does not prove that programmable counters work. Archive import and
SQL queries work without Stax executables or capture privileges, on either OS.

## Redis capture and queries

Build Redis with optimization, debug information and frame pointers, start the
benchmark, then attach to its existing PID:

```bash
topdown stax record --pid 12345 --duration 5 --frequency 99 \
  --output /tmp/redis-stax-baseline --store --process-name redis-server \
  --label git_hash=abc123 --label test_name=set-get \
  --label benchmark_execution_id=redis-baseline-01

# Substitute the run_id printed by record.
topdown stax query RUN_ID
topdown stax hotspots RUN_ID --sort self --limit 20
topdown stax hotspots RUN_ID --sort total --limit 20
topdown stax waits RUN_ID --limit 20
topdown stax query RUN_ID --section samples --offset 0 --limit 10
topdown list --label collector=stax
```

Output is JSON. `--db PATH` selects SQLite; `TOPDOWN_BACKEND=postgresql` and
`TOPDOWN_DSN` select PostgreSQL as for other collectors. Use `--store` with
database/label options during capture. Import a saved artifact separately with:

```bash
topdown stax import /tmp/redis-stax-baseline/archive \
  --process-name redis-server --label test_name=set-get
```

Every import creates a new run. Automatic Stax labels describe the collector,
archive platform, measurement kind and time basis; recorded capture settings also
provide `sampling_frequency_hz`. Supply benchmark, revision and build labels
explicitly. An archive without companion capture settings reports them as
unavailable rather than inferring a frequency from its sample count.

## Launch and cooperating targets

To launch an owned target, provide an executable argv after `--` instead of a PID:

```bash
topdown stax record --duration 5 --output /tmp/stax-command --store \
  -- /absolute/path/to/workload --workload-option value
topdown stax targets RUN_ID
```

The target inherits the private Stax socket, allowing instrumented programs to
report cooperating spans. The adapter launches it and attaches by PID; startup
before attachment can be missed. It stops the owned process group after capture.
An attached process is never signaled. Interactive terminal input is unavailable;
launched target output is retained in `target.log`. Use a foreground command;
a daemonizing command can leave the profiled process group.

Target reports retain producer-supplied timestamps, span counts, origin-thread
links and ingest diagnostics. A Metal lane label can come from simulated GPU
timestamps; it does not by itself prove a hardware measurement. Native Apple GPU
counters remain accessible through `topdown apple gpu`.

## Measurement meanings

- CPU hotspots count physical CPU stack samples. Synthetic target-lane stack
  records are retained and excluded from the CPU denominator. Self shares use the
  leaf frame; inclusive shares count each function once per sample, including
  recursive stacks. Inclusive shares across functions can overlap.
- Off-CPU reports sum scheduler interval durations in nanoseconds, grouped by
  thread, stack and available waking thread. Unknown wakers remain null.
- Linux on-CPU intervals are Stax's sample-gap estimates; they can overlap waits.
  Neither their sum nor CPU-plus-wait totals form a utilization denominator.
- Cooperating target spans have separate duration totals. Overlapping spans do
  not form GPU utilization, CPU time, or TMA pipeline-slot percentages.
- Raw PMU fields preserve unsigned integers. Zero does not establish that a
  counter was supported, and the adapter does not sum values with unverified scope.

The importer replays `events.jsonl` or bundle events when present, following
upstream's authoritative event-stream behavior. It stores deduplicated frame and
stack tables. Paged sample/interval queries expand only selected rows back into
resolved frames. Archived symbol names are retained without Rust/C++ demangling;
unresolved addresses remain visible.

## Artifacts and lifecycle

Captures use a per-user advisory lock: a simultaneous Stax capture fails before
starting tools. They own a temporary Unix socket and server, with its WebSocket listener on
an ephemeral loopback port. They do not reuse an existing Stax server. MCP
captures share the existing serialized native-capture worker with Apple profiling.

The output directory contains `capture.json`, server/recorder logs and `archive/`.
`processes.json` records the live adapter, target, server and recorder IDs for
resource observations.
Directory archives avoid loading a duplicated aggregate copy during event replay
and keep optional code blobs outside the database. Bundle imports and each
structured directory member have a 512 MiB import limit; use shorter captures
when scheduler event volume is high.

Cancellation attempts to stop the owned recording and save `partial-archive/`
before cleanup. Failed or cancelled captures are not automatically stored. The
capture metadata records the outcome and any partial-save error.

CLI output directories belong to the caller. MCP retains captures under
`stax-captures/` beside the configured SQLite database path, including when using
PostgreSQL. The adapter does not delete capture artifacts automatically. Database
queries retain their normalized observations if source files are later removed;
reopening in Stax or performing disassembly still requires those files. Metadata
hashes cover the ingested structured files, not external code blobs.

## MCP tools

`stax_doctor`, `collect_stax_profile`, `import_stax_result`, `query_stax_result`,
`get_stax_hotspots`, `get_stax_thread_waits`, and `get_stax_target_spans` expose the
same workflow. Paths and PIDs are local to the MCP server host. Collection accepts
exactly one `pid` or `command` argv list, plus optional labels, duration and frequency.
Queries page `samples`, `intervals`, `wakeups`, `threads`, `frame_table`,
`stack_table`, `target_lanes` and `application_records`, with limits of 1–1000 rows.

See the [measured VM pilot](stax-vm-pilot.md) and [pilot plan](stax-plan.md) for
results, limitations and platform/overhead acceptance criteria.
