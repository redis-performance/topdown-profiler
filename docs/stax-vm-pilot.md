# Stax integration: Linux aarch64 VM validation

The optional CLI/database/MCP integration is functionally validated against Stax
`aedd85e08894fc3250ec975a486544d091e11281`. Capture remains experimental. This
study supports diagnostic use on the tested VM; it does not establish a production
overhead guarantee or complete the matched Redis 8.6/memtier Apple/Intel pilot.

Exact settings, paired observations, hashes and resource snapshots are in
[stax-vm-validation.json](stax-vm-validation.json). The separate
[Intel laptop pilot](stax-linux-pilot.md) encountered a real permission blocker.

## Environment and workload

- Isolated Ubuntu 24.04.4/VZ guest on an Apple M6 host: aarch64, kernel
  6.8.0-117-generic, four virtual CPUs and 6 GiB memory.
- Stax capture ran as root inside the guest. No host profiling policy or helper
  installation was changed; the default Docker context was unchanged.
- Redis 8.2.3 at `ceb01e1584d6fd847cccf4e3aca2100d2097c321`, built with debug
  information, optimization, frame pointers and the libc allocator.
- Dedicated Redis and clients used a private Unix socket with persistence disabled.
  Concurrent SET and GET clients each used 16 connections, 128-byte payloads and a
  10,000-key range. Pipeline depth was the explicit configuration difference.
- Five pairs alternated baseline/profile order. Each three-second measurement
  window sat within a six-second recording, after two seconds of setup allowance.
  Both wall-clock run bounds and observed monotonic event bounds were checked in
  the final runs. PING probes ran at approximately 100 Hz.

The budget was declared before measurement: at most 10% median paired throughput
loss, 20% median paired PING p99 increase and 10% baseline throughput range.
PING round trips and Redis's own command latency percentiles are separate measures;
PING RTT is not the end-to-end latency of the concurrent SET/GET workload.

## Measured overhead

| Configuration | Median paired throughput loss | Median paired PING p99 change | Result |
|---|---:|---:|---|
| Pinned Stax, 99 Hz, pipeline 16 | 0.74% | +9.17% | Within the declared median budget |
| Pinned Stax, 99 Hz, pipeline 1, serialized repeat | 0.47% | +1539.40% | Tail-latency budget exceeded |
| Idle adapter/server control, pipeline 1, no perf capture | -0.12% | -1.27% | No comparable tail increase |
| Pinned Stax, 9 Hz, pipeline 1 | 0.81% | +2184.28% | Lower frequency did not remedy the tail effect |
| Experimental 5 ms Linux drain timeout, pipeline 1 | 0.38% | +1634.87% | Did not remedy the effect; not adopted |

For the final pinned runs, median baseline/profile PING p99 was approximately
0.189/0.204 ms at pipeline 16 and 0.065/1.004 ms at pipeline 1. The reported paired
percentage medians differ from ratios of those independent medians. Individual
pipeline-16 pairs ranged from -6.77% to +21.02% PING p99 change; passing a median
budget is not a guarantee for every run.

The idle control exercised an idle Stax server and the adapter's polling pattern
without perf collection. Its results point toward native recording as the source
of the pipeline-1 effect in this experiment; they do not isolate a particular
kernel or unwinder operation. The shorter-drain build had a distinct binary hash
and was kept separate from pinned upstream captures. The adapter keeps upstream's
original drain timeout.

One repeat overlapped a separate codec validation capture. That repeat is excluded
in full. The adapter now takes a per-user advisory lock, and the pipeline-1 series
was rerun without overlapping captures. External profilers and other users remain
outside that cooperative lock.

Resource observations include PID/start-time checks, per-process CPU time and RSS
snapshots during each measurement. In the first pipeline-16 profile window, Stax's
server used about 1% of one logical CPU and grew from roughly 58 to 60 MB RSS.
These are snapshots during recording, not peak memory during archive save/import.

No CI performance threshold is enabled. Use repeated, controlled measurements on
the actual workload before making a performance decision.

## Functional and archive checks

- A real busy/sleeping workload produced 101 CPU samples, a resolved `busy`
  function, thread waits and reopened archives.
- Redis captures resolved application/libc frames and survived save/open, thread
  and hotspot queries, flamegraphs and annotated ARM64 disassembly of `aeApiPoll`.
  Native `compare --json` was smoke-tested; this is not a regression-gate validation.
- Executor and simulated Metal examples exposed synthetic stack records that must
  not enter CPU hotspot denominators. The executor capture contained 150 CPU
  samples plus 318 target stack records; the simulated Metal capture contained
  11 CPU samples plus 319 target records. The Metal clock in that example is fake.
- A codec example produced 159 CPU samples and 471 target stack records across two
  lanes. Most of its CPU leaf addresses lacked archived symbol names; successful
  ingestion alone does not establish stack quality.
- The importer follows v3 event replay precedence, preserves raw unsigned PMU
  fields and keeps synthetic spans separate from CPU and off-CPU durations.
  Frame/stack deduplication reduced a one-second Redis document from about 54 MB
  to 6 MB. Its local SQLite summary/hotspot queries took roughly 6 ms.
- SQLite and real PostgreSQL checks cover integer/string retention, atomic rollback,
  foreign-key cleanup, SQL pagination and queries after source artifact removal.
  Real CLI/MCP cancellation checks retain partial archives, reap owned processes
  and leave borrowed targets alive.

The guest exposed software/tracepoint event sources but no hardware PMU device.
Hardware counter availability remains unverified. V3 archives also do not establish
native sample-loss accounting or unwind completeness; retained logs and native
views remain part of capture review.

M1/M6 privileged Stax CPU acceptance remains pending. The pinned Mac helper needs
a private root-owned socket and its teardown guard patch; the adapter does not
install it. Native Apple Instruments and GPU replay support remain separate.

## Reproduce

From a checkout with optional Stax binaries configured and suitable Linux capture
permissions:

```bash
python examples/stax/redis_pilot.py \
  --redis-server /absolute/path/to/redis-server \
  --redis-benchmark /absolute/path/to/redis-benchmark \
  --frequency 99 --pipeline 16 --repeats 5 --duration 3 \
  --output /tmp/new-stax-pilot-directory
```

Repeat with `--pipeline 1` into a different directory. Preserve source/binary
revisions, workload settings, resource pressure and raw paired observations.
Raw evidence for this session is retained in
`.local-runs/stax-integration-20261023/` and the dedicated
`topdown-stax-pilot` Colima guest.
