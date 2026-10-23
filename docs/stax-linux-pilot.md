# Stax Intel Linux pilot: live workflow validated, overhead unresolved

Tested 2026-10-23 on `fco-tp`, an Intel Core Ultra 7 155U Linux development
laptop. The user enabled the required perf permissions after the initial blocked
attempt. Live Redis capture, native inspection, archive replay, and the optional
topdown CLI/SQLite/MCP workflow now pass on this host. The Apple M6 work proceeds
separately.

The paired performance trials are too variable to establish the provisional 5%
throughput-overhead budget. Keep capture experimental and opt-in; this pilot does
not qualify a performance regression gate. Exact measurements, quality counts,
binary digests, and artifact hashes are in
[stax-linux-pilot-evidence.json](stax-linux-pilot-evidence.json).

## Builds and environment

| Component | Revision / configuration | Result |
|---|---|---|
| topdown-profiler | Base `f6b4e7e`, Linux Python 3.12 | 2,080 tests passed, 146 skipped; live CLI/SQLite/MCP checks passed |
| Stax | `aedd85e08894fc3250ec975a486544d091e11281`, Rust 1.98.1 | Release workspace built; CLI 0.1.1; archive v3 |
| Stax Linux capture | Same source, release library tests | 18 passed |
| Redis | 8.6 GA, `006e6a6a602ac5265a676b3aebe9f8ba65459190` | Optimized build, debug info, frame pointers, libc allocator |
| pmu-tools | `2568760ffc9394ddaa7a71f0c39930e780cf2dca` | Separate native TMA capture succeeded |

Host: Ubuntu 24.04, Linux `6.17.0-35-generic`, x86_64, 14 logical CPUs.
This is a shared hybrid-core laptop with background desktop activity, roughly
6 GiB available memory and nearly full 8 GiB swap at the start of the series.
Affinity does not reserve CPUs or control frequency. Memtier is the installed
custom build `255.255.255 sha=113c3755:1`; its binary digest is recorded.

Build commands, run in separate pinned source checkouts:

```bash
cargo build --release --workspace --locked -j 2
cargo test --release --locked -p stax-linux-capture --lib -j 2
make -j2 MALLOC=libc OPT='-O2 -fno-omit-frame-pointer' redis-server redis-cli
```

No system service was installed. Each experiment owned its Redis and client
processes; Stax used private Unix sockets and ephemeral loopback WebSocket ports.
Those processes were stopped afterward. The pilot did not change CPU policy.

## Permissions

The original attempt at `perf_event_paranoid=1`, `perf_event_mlock_kb=516`
failed at `perf_event_open` with permission denied and collected no samples.
Per-process perf success did not imply Stax permission: the pinned backend opens
system-wide per-CPU events, then filters the target threads in userspace.

The user applied these settings; successful captures used them:

```bash
sudo sysctl kernel.perf_event_paranoid=0 kernel.perf_event_mlock_kb=65536
```

This validates the combination, not a minimum locked-memory allowance. Wakeup
tracepoint attribution remained disabled. The settings remain as supplied by the
user. When other profiling work no longer needs them, restore the original values:

```bash
sudo sysctl kernel.perf_event_paranoid=1 kernel.perf_event_mlock_kb=516
```

## Live capture quality and diagnostic value

A smoke recording produced 4,345 CPU samples and 13,851 off-CPU intervals.
Representative archives from the smoke, 900 Hz and 99 Hz series all had matching
reported/observed sample and wait counts, no missing user stacks, median user
stack depth 11, and maximum depth 19. Unresolved user leaves were respectively
87/4,345 (2.00%), 497/26,081 (1.91%), and 74/2,882 (2.57%). The native renderer and
Python importer use different symbol-name handling; these quality counts use the
importer's archived symbol ranges.

In the smoke archive, `__write` accounted for 2,232/4,345 self samples (51.37%),
`read` for 470 (10.82%), and `epoll_wait` for 128 (2.95%). The native flame view
resolved the Redis path through `aeProcessEvents`, `beforeSleep`,
`handleClientsWithPendingWrites`, `writeToClient`, and `connSocketWrite`.
`stax annotate dictFind` resolved `dictFindLinkInternal`, source `dict.c:761`,
and annotated x86 instructions. This is useful function and call-path evidence;
user-side samples at syscall boundaries alone do not establish a kernel or
network bottleneck.

One active Redis thread was visible, as expected for this workload. Scheduler
wait intervals were captured, but `wakeups=false` and zero wakeup records mean
waking-thread attribution is unavailable. Kernel stacks were present in 2,765
smoke samples, but none had a resolved kernel leaf. Do not describe this as
resolved kernel profiling.

All six profiled performance trials reported `lost=0` and `dwarf_truncated=0`
in their native server logs. These fields are not represented in the v3 archive;
retain the logs rather than inferring losslessness from imported sample counts.

PMU group creation failed with `ENOENT` on logical CPUs 4–13 (E-core groups).
Redis was pinned to P-core CPU 2, and every CPU sample in the three inspected
archives contained nonzero cycles, instructions, L1D-miss and branch-mispredict
fields. Their per-CPU, system-wide readings/deltas do not establish Redis-only
counter totals or valid CPI. No TMA was derived from them. Likewise, Linux
sample-gap on-CPU intervals may overlap waits; their sum is not utilization.

## Paired workload and observations

Each fresh loopback Redis instance disabled persistence and used one I/O thread.
Redis ran on CPU 2, memtier on CPUs 4 and 5, and the native Stax server on CPUs 6
and 7. Before each measurement, the driver flushed only its own instance,
prefilled 10,000 keys with 64-byte values, and warmed up for five seconds.
The fixed-default-seed client used:

```bash
memtier_benchmark --server=127.0.0.1 --port="$PILOT_PORT" \
  --threads=2 --clients=10 --pipeline=4 --ratio=1:1 --data-size=64 \
  --key-minimum=1 --key-maximum=10000 --key-pattern=R:R --key-prefix=stax: \
  --test-time=30 --hide-histogram --print-percentiles=50,99 \
  --json-out-file=run.json
```

There were three pairs at upstream's 900 Hz default, followed by three at the
adapter's 99 Hz default, all with DWARF unwinding enabled. Within each series,
order was baseline/profiled, profiled/baseline, baseline/profiled. The native
recorder started before the client and remained alive after it finished; the
45-second hard limit was not reached. Captures were explicitly stopped after
measurement, with actual durations about 31–32 seconds. Sample timestamps are
not recording bounds: at 99 Hz the last observed sample was 9.8 ms before the
final resource observation, while the recorder was still active.

| Hz | Trial in execution order | Ops/s | p50 ms | p99 ms |
|---|---|---:|---:|---:|
| 900 | 01-baseline | 223,140 | 0.311 | 1.887 |
| 900 | 02-profiled | 304,209 | 0.199 | 1.047 |
| 900 | 03-profiled | 338,115 | 0.191 | 1.167 |
| 900 | 04-baseline | 281,955 | 0.207 | 1.295 |
| 900 | 05-baseline | 278,354 | 0.215 | 1.199 |
| 900 | 06-profiled | 352,145 | 0.191 | 1.183 |
| 99 | 01-baseline | 332,397 | 0.191 | 0.855 |
| 99 | 02-profiled | 280,161 | 0.215 | 1.191 |
| 99 | 03-profiled | 298,711 | 0.207 | 1.783 |
| 99 | 04-baseline | 334,280 | 0.183 | 2.063 |
| 99 | 05-baseline | 241,556 | 0.215 | 2.879 |
| 99 | 06-profiled | 283,121 | 0.207 | 1.327 |

All measured trials had zero connection errors and zero key misses. Define
paired throughput loss as `100 * (1 - profiled_ops / baseline_ops)`:

| Hz | Pair 1 loss | Pair 2 loss | Pair 3 loss | Baseline max/min spread |
|---|---:|---:|---:|---:|
| 900 | -36.33% | -19.92% | -26.51% | 26.36% |
| 99 | +15.71% | +10.64% | -17.21% | 38.39% |

Negative loss is apparent higher profiled throughput, not evidence that profiling
helps. The 99 Hz median paired loss was 10.64%, above the provisional 5% budget,
but variation and mixed directions prevent isolating profiler overhead. Tail
latencies were also unstable: the first 99 Hz pair increased p99 by 39.30%, while
the other two decreased it. Neither frequency earns an overhead acceptance pass,
and sequential series do not establish which frequency is cheaper.

During the 30-second measurements, native Stax server CPU time was 1.53–1.79 s
at 900 Hz and 0.87–1.29 s at 99 Hz. First-profile sampled server RSS was 35.7 MiB
and 22.9 MiB respectively, growing to 290.8 MiB and 171.2 MiB by the last profile.
Each series kept one server with prior history, including during baselines;
these are process observations, not isolated per-capture allocations. One-second
RSS sampling excludes save/import peaks. Redis CPU time and client CPU snapshots
are retained in the JSON evidence. No build, test suite, or archive import ran
during these trials. An idle default server was briefly started and stopped
during the last 99 Hz trial while probing unsupported server `--help`; it did not
record. Shared-host activity and CPU pressure remained uncontrolled throughout.

The next overhead acceptance test belongs on a quiet, controlled host, with
frequency/thermal state and core-sibling activity recorded, independent repeated
series, and fresh-server memory measurements. These observations measure native
Stax recording; they do not bound integrated Python import/storage peak resources.

## Replay, SQL and MCP checks

Reopening the smoke `.stax` archive in a new private native server reproduced
`threads`, self/total `top`, `flame`, and `annotate` output byte-for-byte.
Saving it as a directory and importing both forms produced identical normalized
results. `stax compare --json archive archive` returned zero for every delta.
Comparison of two distinct 900 Hz archives also returned valid JSON. Different
completed request counts and estimated on-CPU intervals make that a functional
check, not a regression verdict.

Separate five-second captures at 99 Hz exercised the current integration:

| Entry point | CPU samples | Off-CPU intervals | Result |
|---|---:|---:|---|
| `topdown stax record --store` | 423 | 11,075 | Capture, archive, labels and SQLite import passed |
| Registered `collect_stax_profile` MCP tool | 427 | 11,094 | Capture and storage passed |

Summary, self/total hotspot, wait, and paginated sample queries through registered
FastMCP `call_tool` matched CLI JSON exactly for the same run. This checks tool
registration/dispatch, not an external stdio/HTTP transport. Both captures left
the attached Redis process alive. The dedicated database contained two runs,
two `stax_results`, and zero TMA `samples` rows. Frequency labels and artifact
hashes were retained. See [Stax usage](stax-usage.md) for supported commands.

## Earlier hybrid-CPU TMA finding

The initial pilot separately captured 64 L2 toplev rows but exposed an extra
`CPUs` column that the old parser misread as the metric. That capture ran during
compilation and is functional evidence only, not an overhead baseline.

The fix is already present at tested base `f6b4e7e` (`8694d15`, `13be0eb`).
Replaying the retained raw capture now yields 64 parsed rows; the first is
`Frontend_Bound=37.9 % Slots`, with `cpu_group='core'`. This supersedes the
initial parser-blocker status. The raw capture was not imported, and default
threshold-filtered toplev output still does not constitute a complete funnel.

## Retained artifacts and reproduction

Local directory: `~/.cache/topdown-stax-linux-pilot/`. Large archives and raw logs
stay outside Git; the evidence JSON records their hashes and measured summaries.

- `environment.json`, `preflight.json`, `smoke-default/`: original setup and failed attach.
- `smoke-enabled/`, `paired-900hz/`, `paired-99hz/`: memtier JSON, resource snapshots,
  command journals, native query output, loss logs and `.stax` archives.
- `integration-enabled/`: CLI/MCP captures, query output, dedicated SQLite DB and assertions.
- `replay-enabled/`: reopened views, directory archive, comparison JSON and assertions.
- `native-tma/`: separate raw capture and current-parser replay result.
- `run_pilot.py`, `analyze_live.py`, `validate_integration.py`, `validate_replay.py`:
  local experiment drivers, with hashes in the evidence manifest.

The local driver expects the pinned builds under that cache directory. Use new
output directories for repeats:

```bash
python3 ~/.cache/topdown-stax-linux-pilot/run_pilot.py --mode smoke --seconds 5 \
  --output ~/.cache/topdown-stax-linux-pilot/smoke-repeat
python3 ~/.cache/topdown-stax-linux-pilot/run_pilot.py --mode bench --seconds 30 --pairs 3 \
  --frequency 99 --output ~/.cache/topdown-stax-linux-pilot/paired-99hz-repeat
```

For the portable, supported capture workflow use [stax-usage.md](stax-usage.md).
The local experiment driver is not a supported project command.

Sources: [pinned Stax Linux backend](https://github.com/bearcove/stax/tree/aedd85e08894fc3250ec975a486544d091e11281/stax-linux-capture),
[the integration plan](stax-plan.md), and [separate Linux VM results](stax-vm-pilot.md).
