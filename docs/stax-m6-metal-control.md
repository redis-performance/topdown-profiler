# Stax on M6: real Metal command-buffer spans

On Apple M6 with macOS 27.0.1 and Xcode 27.1, a rootless control completed
300 Metal command buffers and verified the output checksum. Stax accepted
299 spans after its capture gate opened. The optional topdown importer retained
all 299 spans in SQLite and reported **zero CPU samples and zero CPU hotspots**.

These are real completed command-buffer timestamps. The helper supplied no
kperf records. This control does not validate privileged CPU sampling, PMU data,
CPU stack attribution, GPU hardware counters or GPU utilization.

Exact revisions, hashes, counts and query output are in
[stax-m6-metal-control.json](stax-m6-metal-control.json). This extends the
[M1 bridge control](https://github.com/redis-performance/topdown-profiler/blob/bc96a8d38563c624976d960c746a972291dd6952/examples/stax_m1_metal_lane/README.md)
from the separate M1 pilot draft.

## What was checked

The Swift app submitted a tiny `add_one` compute kernel over 16,384 unsigned
integers, waited for each command buffer to complete, and reported its
`gpuStartTime`/`gpuEndTime` through Stax's target API. After 300 passes, the
checksum was 4,915,200, matching the expected result. The first command buffer
ran before the target capture gate became active; the other 299 were accepted.

For every command buffer, the app measured host `mach_absolute_time()` just
before submission and just after completion. The host timebase was 125/3 ns per
tick. All 300 GPU intervals fell within these host bounds. All 299 archived
start/end timestamps matched the producer's converted nanosecond values exactly.
This checks the clock conversion for this host and API; it does not establish
precision for other timestamp producers.

The archive retained the Metal lane name, span label, origin thread ID and
durations. All 299 origins were unlinked, with the native diagnostic reason
`spans_origin_no_thread`: there were no CPU samples to supply an origin stack.
An origin thread ID alone is not CPU stack attribution.

The [small regression fixture](../tests/test_data/stax_v3_m6_metal_excerpt.json)
retains the original first/last span events and target symbols, with the full
capture's summary and diagnostics. Its two selected durations are 5,250 and
8,333 ns. Diagnostics still describe the full 299-span capture. Unrelated
application symbols are omitted, and the source event stream's SHA-256 is
recorded in the fixture.

The importer and both SQL backends are tested against this excerpt. A real
Python 3.10.21/MCP 1.24.0 stdio client also imported and queried it through the
installed wheel: all seven Stax tools were registered, the CPU-hotspot
denominator was zero, and the selected Metal durations summed to 13,583 ns.
The full capture's total duration is in the JSON evidence. Sums of span durations
are not GPU utilization, and a command buffer can contain multiple kernels.

## Reproduce the source control

Use the pinned Stax revision and the source/build instructions in the linked
M1 bridge README. The example comes from an immutable commit in the M1 pilot
draft; it need not be checked out over your working branch. From a profiler
checkout, extract it into a temporary directory:

```bash
git fetch origin bc96a8d38563c624976d960c746a972291dd6952
STAX_EXAMPLE_SOURCE=$(mktemp -d /tmp/topdown-metal-source.XXXXXX)
git archive bc96a8d38563c624976d960c746a972291dd6952 \
  examples/stax_m1_metal_lane | tar -x -C "$STAX_EXAMPLE_SOURCE"
```

Copy `$STAX_EXAMPLE_SOURCE/examples/stax_m1_metal_lane` to
`stax-metal-bridge/` inside the disposable Stax workspace. Before building, apply
[m6-metal-clock.patch](../examples/stax/m6-metal-clock.patch) from that workspace:

```bash
git apply /absolute/path/to/topdown-profiler/examples/stax/m6-metal-clock.patch
```

The patch changes the lane labels to M6 and adds checked Mach timestamp bounds
and a JSON producer log. It does not change Stax itself. Build the static Rust
bridge and Swift executable, then use the README's private-socket, zero-kperf
fake-helper control. A 300-pass run takes several seconds. Save the archive
with the same private server, then import it:

```bash
STAX_SERVER_SOCKET=/absolute/path/to/private/server.sock \
  stax save /tmp/new-m6-metal-archive
topdown stax import /tmp/new-m6-metal-archive \
  --process-name m6-metal-control -L host_chip=apple-m6 --db /tmp/m6-stax.db
topdown stax targets RUN_ID --db /tmp/m6-stax.db
topdown stax hotspots RUN_ID --db /tmp/m6-stax.db
```

The measured integration control used topdown's capture adapter with only its
root-owned helper check replaced inside the disposable test process. The fake
helper ran as the current user and emitted zero kperf records. The public
`topdown stax record` preflight still rejects this helper; no root helper or
LaunchDaemon was installed. Archive import requires no helper.

The kernel, buffer size and deliberate 20 ms pause make this a functional
control. It is not an overhead study or a GPU bottleneck classification.
For native Apple GPU counters and replay reports, use the separate
[Apple profiling workflow](apple-silicon.md).
