# Stax Linux pilot: build validated, capture permission pending

Tested 2026-10-23 on `fco-tp`, an Intel Core Ultra 7 155U Linux development
laptop. The Apple pilot is being handled separately on the M6.

Stax builds and its Linux capture unit tests pass. An actual Redis attach fails
at the first system-wide perf event because this host has
`kernel.perf_event_paranoid=1`. No Stax samples were collected, so stack quality,
overhead, archive replay, and comparison remain unvalidated. Do not enable a
Stax collector or performance regression gate on the strength of this pilot yet.

Machine-readable results and binary hashes are in
[stax-linux-pilot-evidence.json](stax-linux-pilot-evidence.json).

## Builds and checks completed

| Component | Revision / configuration | Result |
|---|---|---|
| topdown-profiler | Base `0763ba3`, Linux Python 3.12 | 1,925 tests passed, 126 skipped |
| Stax | `aedd85e08894fc3250ec975a486544d091e11281`, Rust 1.98.1 | Release workspace built; CLI reports 0.1.1 |
| Stax Linux capture | Same source, release library tests | 18 passed, 0 failed |
| Redis | 8.6 GA, `006e6a6a602ac5265a676b3aebe9f8ba65459190` | Built and exercised with memtier |
| pmu-tools | `2568760ffc9394ddaa7a71f0c39930e780cf2dca` | Detects Meteor Lake, TMA 5.1-full; Redis capture exits successfully |

Build commands, run in separate source checkouts:

```bash
# Stax: keep workspace feature resolution consistent across binaries.
cargo build --release --workspace --locked -j 2
cargo test --release --locked -p stax-linux-capture --lib -j 2

# Redis: optimized code with debug information and frame pointers, libc allocator.
make -j2 MALLOC=libc OPT='-O2 -fno-omit-frame-pointer' redis-server redis-cli
```

The source checkouts and binaries are local to the pilot; no system service was
installed. Stax used a dedicated Unix socket and an ephemeral loopback WebSocket
port. All pilot Redis, client, and Stax server processes were stopped afterward.

## Capture permission finding

The host initially has:

```text
kernel.perf_event_paranoid = 1
kernel.perf_event_mlock_kb = 516
```

Per-process `perf stat` and a grouped Intel L1 TMA event probe both succeed.
System-wide `perf stat -a -e task-clock -- sleep 0.1` fails with exit code 255.
Stax also opens per-CPU, system-wide events and filters the target PID in its
reader. Targeting one PID therefore does not make its capture permission
requirements equivalent to per-process perf.

The real Redis attach at 900 Hz with default DWARF unwinding produced:

```text
error: recorder failed: perf_event_open on cpu 0 failed: Permission denied (os error 13)
(need perf_event_paranoid low enough, or the daemon)
```

The server reported zero samples and zero intervals. Noninteractive sudo is
unavailable on this host. An operator can temporarily enable the in-process pilot:

```bash
sudo sysctl kernel.perf_event_paranoid=0 kernel.perf_event_mlock_kb=65536
```

The larger pinned-page allowance is preparation for Stax's per-CPU rings; this
attempt failed before mapping any rings, so the default allowance was not itself
tested. This setup does not establish wakeup tracepoint access. Report unavailable
wakeup attribution separately from ordinary off-CPU intervals.

Restore the original settings after the pilot when no other profiling task needs
the temporary values:

```bash
sudo sysctl kernel.perf_event_paranoid=1 kernel.perf_event_mlock_kb=516
```

## Workload and remaining measurements

The prepared workload uses a fresh, loopback-only Redis instance with persistence
disabled, one I/O thread, and 10,000 prefilled keys with 64-byte values. Redis is
pinned to logical CPU 2, memtier to CPUs 4 and 5, and Stax server to CPUs 6 and 7.
These are affinity choices on a shared hybrid-core laptop, not reserved CPUs.

The client uses its fixed default seed and:

```bash
memtier_benchmark --server=127.0.0.1 --port="$PILOT_PORT" \
  --threads=2 --clients=10 --pipeline=4 --ratio=1:1 --data-size=64 \
  --key-minimum=1 --key-maximum=10000 --key-pattern=R:R --key-prefix=stax: \
  --test-time=30 --hide-histogram --print-percentiles=50,99 \
  --json-out-file=run.json
```

Before each measured run, flush only the pilot instance, prefill all keys, and
warm up for five seconds. The installed memtier is a custom build; its version
and binary hash are retained in the evidence file.

After capture permissions are available:

1. Repeat the Stax smoke test and verify nonzero samples, symbols, and thread data.
2. Run three unprofiled/profiled pairs in alternating order. Use 30-second client
   measurements wholly inside the capture window, with a 45-second hard capture
   limit and explicit stop after the client finishes. Record throughput, p50/p99,
   Redis/profiler CPU and memory use, and host pressure.
3. Evaluate against the provisional 5% throughput-overhead budget, with variability
   reported. This laptop cannot establish a CI threshold. Repeat on a controlled
   host before drawing a performance conclusion.
4. Inspect self/total hotspots, thread waits, flamegraphs, and annotated disassembly.
   Check losses, truncated stacks, and counter availability. If necessary, evaluate
   a lower sampling rate or frame-pointer-only capture as separate configurations.
5. Save, reopen, and compare real archives. Preserve units and collector provenance.

The local driver is at `~/.cache/topdown-stax-linux-pilot/run_pilot.py`; it records
command arguments and cleans up only its own processes. For this prepared host:

```bash
python3 ~/.cache/topdown-stax-linux-pilot/run_pilot.py --mode smoke --seconds 5 \
  --output ~/.cache/topdown-stax-linux-pilot/smoke-enabled
python3 ~/.cache/topdown-stax-linux-pilot/run_pilot.py --mode bench --seconds 30 --pairs 3 \
  --output ~/.cache/topdown-stax-linux-pilot/paired-900hz
```

These output directories must be new. The driver is a local experiment artifact,
not a supported project command or a completed collector integration.

## Additional finding: hybrid-CPU TMA CSV is misparsed

A separate 30-second Redis workload with a 32-second L2 toplev capture completed
successfully. The raw capture contains 64 metric rows. It ran while the Stax test
dependencies were compiling, so it demonstrates capture functionality only; its
throughput is not an unprofiled baseline or an overhead measurement.

Capture command:

```bash
python3 toplev.py --cputype core -l2 -I1000 -x, --no-desc \
  --pid "$REDIS_PID" -- sleep 32
```

This toplev version emits an extra `CPUs` column, whose value is `core`:

```text
Timestamp,CPUs,Area,Value,Unit,Description,Sample,Stddev,Multiplex,Bottleneck,Idle
1.000341466,core,Frontend_Bound,37.9,% Slots,,frontend_retired.latency_ge_4,0.0,100.0,,
```

At the tested topdown revision, `parse_output` treats that row as the ten-column
format and returns `metric_name='core', value=0.0, unit='37.9', status='100.0'`.
The expected metric is `Frontend_Bound`, value `37.9`, unit `% Slots`.

This is a production parser follow-up, not a Stax defect. The raw capture was not
imported into storage. Before using such captures, fix CPU/group-column detection
and add a regression fixture from this capture. Preserve the distinction between
hybrid PMU groups; do not silently combine `core` and `atom` results into one metric.
The default threshold-filtered toplev output is also not a complete pipeline funnel.

## Retained artifacts

Local directory: `~/.cache/topdown-stax-linux-pilot/`.

- `environment.json`, `preflight.json`, and `perf-*.stderr`: setup and permission probes.
- `stax-build.log`, `stax-linux-tests.log`, `topdown-tests.log`: build/test evidence.
- `smoke-default/`: command journal, Redis/client logs, and failed Stax attach logs.
- `native-tma/`: raw toplev output, memtier JSON, and exact command arguments.
- `run_pilot.py`, `run_native.py`: local reproduction drivers.

Sources: [pinned Stax Linux backend](https://github.com/bearcove/stax/tree/aedd85e08894fc3250ec975a486544d091e11281/stax-linux-capture),
[Stax installation](https://stax.bearcove.eu/guide/getting-started/), and
[the integration plan](stax-plan.md).
