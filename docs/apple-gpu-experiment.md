# Apple GPU top-down experiments

Start with the most expensive GPU passes, inspect Apple's native limiters, change
the workload, and verify both the result and its GPU timing. M6 supports this
workflow through **GPU replay profiling**, even though its Instruments counter
timeline currently exposes little useful compute data. The packaged `apple gpu`
CLI collects native counters, stores them in SQLite/PostgreSQL, and exposes
collection, paginated queries and bottleneck reports through MCP.

The commands use new output paths and do not change host security settings.
Raw captures stay local; reviewed M6 measurements are in
[apple-gpu-m6-validation.json](apple-gpu-m6-validation.json). Its
`measurement_commit` identifies the original experiment source for the recorded
source hashes; the packaged workflow adds storage and MCP integration.

## M6 finding and useful GPU report

Measured on Apple M6, macOS 27.0.1 (26A434), Xcode 27.1 (27A9275):

| Method | Observed result |
|---|---|
| Public `MTLDevice.counterSets` | `timestamp/GPUTimestamp` only; stage-boundary sampling supported |
| Instruments Metal System Trace | Empty or RT Unit Active only across default, Limiters and Utilizations captures |
| `gpudebug profile run` | Native replay timings, encoder costs, occupancy and limiters; 33 counter groups observed |

The CPU programmable-counter kernel restriction does not prevent this GPU replay
path. The replay captures include the probe's command queue, rather than a
system-wide GPU activity stream. The timeline hardware counters have a wider
scope and can include concurrent device activity, as the control below shows.

The original kernel executes 1024 FMAs in a loop. The `alu` control performs the
same 1024 FMAs with fewer loop-control instructions and produces the same
checksum. Four-pass replay observations illustrate the top-down workflow:

| Workload | Original command GPU time | Replay GPU time | Native limiter observation in a profiled encoder |
|---|---:|---:|---|
| `compute` | 0.5820 ms | 0.2661 ms | Control Flow 71.31%; F32 15.70% |
| `alu` | 0.1308 ms | 0.0861 ms | F32 55.86%; Control Flow 1.73% |
| `memory` | 5.4718 ms | 4.4176 ms | Compute Shader Launch 105.14%, flagged as above 100% |
| `stream` | 6.1895 ms | 4.0438 ms | Compute Shader Launch 91.86%; MMU 5.53% |

Compute/ALU use 16,384 floats. Memory/stream use 16,777,216 floats in two shared
buffers; `memory` handles one float per thread, while `stream` handles 64 using
vector operations. Both memory variants apply one FMA per float and pass. All
eight 4/8-pass captures match an independent CPU FMA reference. Original command
times use Metal's command-buffer GPU timestamps; replay times come from the
profiler and must be compared within their own measurement path.

Doubling passes changes replay GPU time from 0.2661 to 0.5395 ms (`compute`),
0.0861 to 0.1830 ms (`alu`), 4.4176 to 8.3792 ms (`memory`), and 4.0438 to
8.1504 ms (`stream`). In a separate unprofiled control with 1,048,576 floats and
four passes, seven alternating trials per mode measured median command GPU times
of 12.904 ms and 2.124 ms respectively, a 6.08× difference. Ranges were
7.802–27.036 ms and 1.624–5.229 ms; frequency and other activity were not fixed.
This supports the loop-control improvement on this host, rather than a portable
speedup guarantee.

**These are overlapping native limiters, not a four-way percentage partition.**
The report retains values above 100% and flags them. Their M6 denominators are
not calibrated here. The memory cases do not establish a DRAM-bound
classification: the native launch limiter dominates both variants. Sampling
coverage also varies: the memory captures report 10/12 and 5/12 sampled cores,
while the compute, ALU and stream captures report 12/12. The report prints this
coverage and preserves all exported counters as native strings.

### M6 stream work per thread

An additional [eight-trial control](apple-gpu-m6-stream-granularity.json) varies
the vectorized stream kernel from 4 to 256 floats per thread. Every trial applies
the same four passes to 16,777,216 floats in two shared buffers and verifies every
output element against the CPU reference. The order is
64, 4, 16, 256, 256, 16, 4, 64 floats per thread, with two trials per variant:

| Floats/thread | Original command GPU time | Replay GPU time | Timeline shader-launch limiter | Native external GPU bandwidth |
|---|---:|---:|---:|---:|
| 4 | 4.0321–4.1526 ms | 4.0623–4.1010 ms | 100.32–100.33% | 122.95–124.57 GiB/s |
| 16 | 4.1348–4.1406 ms | 4.0622–4.0720 ms | 97.68–97.71% | 123.40–124.54 GiB/s |
| 64 | 4.1156–4.2007 ms | 4.0671–4.0984 ms | 90.89–91.11% | 122.81–123.82 GiB/s |
| 256 | 4.1788–4.1885 ms | 4.0856–4.1892 ms | 65.99–66.55% | 120.26–122.98 GiB/s |

All replays sampled 12/12 cores. Increasing work per thread reduces the native
launch metric considerably while GPU times remain close. The debugger defines
this limiter as launches **or stalled launches**, normalized to peak launch
rate. Its memory counter measures traffic to memory external to the GPU,
potentially device memory, and exports `GiB / Second`. These definitions support
reading the launch, bandwidth and occupancy observations together. They do not
establish DRAM saturation or a portable performance benefit. Two trials per
variant and uncontrolled frequency/background activity also limit small timing
comparisons. Algorithmic array bytes remain separate from native traffic.
Apple's [profiling tools talk](https://developer.apple.com/videos/play/tech-talks/111374/)
also explains that launch stalls can reflect back pressure. Its
[performance guide](https://developer.apple.com/documentation/xcode/optimizing-gpu-performance)
distinguishes the overlapping timeline from detailed counters measured for
individual passes in isolation.

Reproduce the balanced experiment using a fresh output directory:

```sh
python3 examples/apple_gpu/stream_granularity.py --output /tmp/m6-stream-width-001
topdown apple gpu report /tmp/m6-stream-width-001/trial-00-vectors-16/replay
```

The script writes the four generated Swift sources, their source/binary hashes,
capture logs, per-trial native profiles and `results.json`. Its generated sources
match the committed measurement hashes. Raw traces stay local.
The public runner was then exercised end to end for another eight trials, all
with verified output and 12/12 sampled cores. Its original GPU times ranged from
4.0817 to 4.2319 ms and replay times from 4.0290 to 4.1713 ms. This replication
also found no consistent timing advantage as work per thread increased; its
counter values and capture hashes are retained in the same evidence file.
Three replication trials also have vertex or fragment timeline observations
despite the captured queue containing only compute encoders. These values are
preserved; counter availability varies between runs.

### M6 dependent reads: parallelism and table size

The [dependent-read probe](../examples/apple_gpu/dependent_reads_probe.swift)
executes 16,777,216 logical `uint` loads per trial. Each thread follows a chain
through a CPU-initialized table; every output is checked against an independent
CPU recurrence. Changing the number of independent chains changes the loop
length while retaining the logical load count. Two table allocations and three
chain counts were measured in a balanced forward/reverse order:

| Allocated table | Independent chains | Original command GPU time | Replay GPU time | Native active cores |
|---|---:|---:|---:|---:|
| 64 MiB | 64 | 77.4195–77.6923 ms | 81.7184–81.8344 ms | 8% |
| 64 MiB | 4,096 | 11.9596–11.9745 ms | 9.4159–9.4705 ms | 84% |
| 64 MiB | 262,144 | 11.8131–11.8981 ms | 9.3366–9.3452 ms | 100% |
| 64 KiB | 64 | 16.3811–16.4249 ms | 10.3593–10.3779 ms | 8–9% |
| 64 KiB | 4,096 | 0.7026 ms | 0.2780 ms | 76% |
| 64 KiB | 262,144 | 0.4717–0.4773 ms | 0.1865–0.1866 ms | 98–100% |

Each row contains two trials. A second 12-trial run of the public runner
replicated the broad timing pattern; both runs are retained in
[the native evidence](apple-gpu-m6-dependent-reads.json). Increasing parallel
chains makes the large-table replay much faster until the timing changes little
between 4,096 and 262,144 chains. The smaller table has substantially lower
timings, especially with many chains. These controls help distinguish limited
parallel work from the effects of the address range.

Native counter availability matters. The 64-chain cases have no per-encoder
limiter entries; the report prints them as unavailable. At 262,144 chains the
large-table timeline reports about 20% kernel occupancy and 102.82 GiB/s external
GPU bandwidth, while the small-table cases report 84–85% kernel occupancy and
1.34 GiB/s. The same logical load count therefore does not imply the same external
traffic. Native compute-launch limiters are high in both high-parallelism cases
despite very different timings. Sampling coverage and concurrent graphics
observations remain explicit.

This is not a calibrated cache-level, DRAM-bandwidth, or per-load latency test.
Table allocation bounds the address range; unique addresses, cache hits,
transaction sizes and access overlap are not measured. Changing chain count also
changes launch count and loop length. Frequency and background activity remain
uncontrolled, and timeline counters can include other GPU work.

```sh
python3 examples/apple_gpu/dependent_reads.py --output /tmp/m6-dependent-reads-001
topdown apple gpu report /tmp/m6-dependent-reads-001/trial-00-bits-24-threads-64/replay
# Store a replay, or use profile_apple_gpu_trace with its capture.gputrace in MCP.
topdown apple store /tmp/m6-dependent-reads-001/trial-02-bits-24-threads-262144/replay \
  --process dependent-reads --label test_name=m6-dependent-reads
```

### M6 compute control: independent FMA chains

The [FMA probe](../examples/apple_gpu/fma_chains_probe.swift) executes
268,435,456 logical fused multiply-add updates per trial, varying 64/4,096
threads and 1/4/16 independent chains within each thread. The positive
coefficients keep the tested states growing, and safe/precise Metal math avoids
unsafe reassociation. After each original command, every output's Float bit
pattern must match a fused CPU reference. The result records coefficient bits,
checksums, work counts and original GPU timing.

The [measured M6 evidence](apple-gpu-m6-fma-chains.json) contains 12 forward/reverse
trials plus 12 public-runner replications, with source, binary and artifact hashes.
These are the full ranges across all four observations of each condition:

| Threads | Chains/thread | Original GPU ms | Replay GPU ms | Native active-core share |
|---:|---:|---:|---:|---:|
| 64 | 1 | 54.74–54.91 | 62.9507–62.9536 | 8% |
| 64 | 4 | 20.27–20.42 | 15.7393–15.7408 | 8% |
| 64 | 16 | 12.52–12.54 | 6.2144–6.2263 | 8–9% |
| 4,096 | 1 | 2.49–3.73 | 1.2858–1.4776 | 74–82% |
| 4,096 | 4 | 1.2472–1.2478 | 0.4940–0.4959 | 63–67% |
| 4,096 | 16 | 0.6823–0.6866 | 0.2703–0.3336 | 66–67% |

More independent chains made the same logical work faster in this control.
With 4,096 threads, the per-encoder F32 Limiter rose from 9.34–10.48% for one
chain to 54.86–55.00% for 16 chains while replay duration fell. Read this native
counter alongside verified work and duration: a larger limiter value alone
cannot rank performance. The 64-thread one-chain captures have no per-encoder
F32 Limiter field, even though their timeline F32 observations are present.
Missing fields stay unavailable.

Changing chains also changes register demand, loop iterations, coefficient reuse
and input/output size. Changing threads changes scheduling and per-thread work.
The logical FMA count does not establish issued instructions, pipeline latency
or peak FLOP/s. Original command and replay timings describe different
executions; frequency and background GPU activity are uncontrolled. Timeline
counter scope remains wider than the captured workload.

Run the balanced experiment on a supported replay host:

```bash
python examples/apple_gpu/fma_chains.py --output apple-fma

topdown apple gpu report \
  apple-fma/trial-00-threads-64-chains-1/replay/profile.json

topdown apple store apple-fma/trial-00-threads-64-chains-1/replay/profile.json \
  --label test_name=m6-fma --label threads=64 --label chains_per_thread=1
```

The runner freezes the compiled Swift source and its own Python source in the
new output directory. Keep `results.json` with the captures for the verified
work counts and provenance. Stored replays use the existing
`query_apple_result(section="encoders")` and `get_apple_gpu_report` MCP tools;
SQLite/PostgreSQL round-trip all 24 measured profiles without changing native
strings, missing counters, costs or scope.

### Timeline counters include concurrent GPU activity

A separate [offscreen renderer](../examples/apple_gpu/background_render_probe.swift)
was run during replay of the same four-encoder compute-only capture. The
[baseline/background/after control](apple-gpu-m6-background-scope.json) records:

| Condition | Replay GPU time | Sampled cores | Fragment occupancy in timeline |
|---|---:|---:|---:|
| Before background renderer | 4.1007 ms | 12/12 | Unobserved |
| Concurrent offscreen renderer | 4.6705 ms | 2/12 | 19.46% |
| After renderer stops | 4.1007 ms | 12/12 | Unobserved |

The captured encoder list remains four compute passes throughout. The separate
renderer completed 23,021 commands successfully. This supports treating timeline
hardware counters as GPU observations that can include other device activity,
rather than attributing every timeline metric to the captured encoders. It also
shows why a native `Consistent state=yes` flag does not certify workload
isolation: all three cases have that flag. Core coverage remains visible.

To repeat the control, compile the renderer, take a baseline replay, then start
the renderer before the next replay. Use separate new replay output paths:

```sh
mkdir /tmp/m6-background-001
xcrun swiftc -O -framework Metal examples/apple_gpu/background_render_probe.swift \
  -o /tmp/m6-background-001/probe
/tmp/m6-background-001/probe /tmp/m6-background-001 &
BACKGROUND_RENDER_PID=$!
# Wait for /tmp/m6-background-001/ready, then profile your existing compute trace.
topdown apple gpu profile /path/compute.gputrace --output /tmp/m6-background-replay-001
touch /tmp/m6-background-001/stop
wait "$BACKGROUND_RENDER_PID"
```

The renderer checks for `stop` after each completed command and otherwise exits
after 40 seconds. It uses an offscreen texture and requires no window. Repeat
the replay after it exits; original application timing and these replay
observations remain separate measurements.

### Mixed compute, blit and render capture

The [mixed probe](../examples/apple_gpu/mixed_probe.swift) increments a buffer,
copies it with a blit encoder, and draws a red triangle to an offscreen texture.
It verifies every copied value and the output pixel before reporting success.
It needs no window or display surface:

```sh
mkdir /tmp/m6-gpu-mixed-001
xcrun swiftc -O -framework Metal examples/apple_gpu/mixed_probe.swift \
  -o /tmp/m6-gpu-mixed-001/probe
MTL_CAPTURE_ENABLED=1 /tmp/m6-gpu-mixed-001/probe /tmp/m6-gpu-mixed-001
topdown apple gpu profile /tmp/m6-gpu-mixed-001/capture.gputrace \
  --output /tmp/m6-gpu-mixed-replay-001 --store --label test_name=m6-mixed
topdown apple gpu report /tmp/m6-gpu-mixed-replay-001
```

The [M6 mixed validation](apple-gpu-m6-mixed-validation.json) retains all three
native encoder types and counters, plus the exact source hash. Collection,
storage, encoder paging and reports were also exercised through MCP. This tiny
correctness workload is not a bottleneck calibration. Native cost attribution
can rank encoders differently from their measured durations; the report retains
both values and does not derive cost from duration.

### CLI, database and MCP

Install the project and use full Xcode. Replay profiling requires M3/A17 Pro or
later; the M1 Instruments experiment below uses a different collection path.
A direct Xcode 27.1 attempt on this M1 first captured a checksum-verified
four-pass `.gputrace`, then `gpudebug` explicitly rejected replay profiling
with `GPU profiling requires Apple M3 / A17 Pro or later`. The collector
terminated the session it had created and retained the command logs; the
[M1 replay capability check](apple-gpu-m1-replay-capability.json) records hashes
and the native response.
For Xcode 27.1, `topdown apple doctor` now directs M1/M2 hosts to Instruments
for GPU counters instead of suggesting replay profiling. On an untested Xcode
version it leaves replay support unverified until a native attempt.
`topdown apple doctor` reports the GPU debugger version separately from CPU
programmable-counter diagnostics. GPU access remains unverified until a native
profile succeeds; a CPU kernel restriction does not establish GPU availability.

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
# New output directory; compiles the packaged probe, captures, replays and stores.
topdown apple gpu record --mode compute --passes 4 --items 16384 \
  --output /tmp/m6-gpu-compute-001 --store --label test_name=m6-gpu-compute
topdown apple gpu record --mode alu --passes 4 --items 16384 \
  --output /tmp/m6-gpu-alu-001 --store --label test_name=m6-gpu-alu

# Use the run_id printed by either capture.
topdown apple gpu report --run-id RUN_ID --limit 4
topdown apple gpu report --run-id RUN_ID --json
topdown apple query RUN_ID --section encoders --offset 0 --limit 2

# Existing Metal capture, or an exported profile from another machine.
topdown apple gpu profile /path/workload.gputrace --output /tmp/m6-replay-001 --store
topdown apple store /path/profile.json --process metal-workload --label chip=M6
topdown apple gpu report /path/profile.json
```

Use `--db /path/results.db` with `--store` for a specific SQLite database and
with run-ID query/report commands to read it. For PostgreSQL, install the
`postgresql` extra and set `TOPDOWN_BACKEND=postgresql` and `TOPDOWN_DSN` as in
[the storage guide](apple-silicon.md#database-storage-and-mcp). The existing
native Apple document table stores the complete GPU JSON atomically with its run;
no Linux metric samples are created. GPU runs have `collector=apple`,
`processor=gpu`, `device`, and `measurement_kind=apple-gpu-replay-profile` labels.
Native integer counters remain strings exactly as exported by the GPU debugger.

New replay captures also retain the collection host's chip model, OS build and
kernel UUID in `environment`. Stored `cpu` labels use that recorded identity;
the debugger's native GPU `device` string remains separate. Imports preserve
the captured provenance when read on another host.
Imports without timestamps are explicitly labeled with an import-time basis.

New profiles include `timeline_counter_groups` for the available bandwidth,
occupancy, shader-launch, active-core and F32 groups. Each observed leaf retains
its native value, unit, description and timeline scope. A listed counter without
an observation has null metadata: querying that leaf directly can otherwise
produce a default zero. Reports print observed metrics and their coverage;
JSON reports expose `timeline_metrics`, including explicit unavailable entries.
The full native groups survive SQLite/PostgreSQL import and MCP summary queries.
Reports expose `timeline_counter_scope.target_exclusive=false`: timeline hardware
counters can include concurrent device activity. They print the native
`Consistent state` flag alongside sampled-core coverage. Per-encoder counters
and timeline means describe different measurement scopes.
Older profiles without these groups retain their existing report format.

Labels are retained as exported by the debugger. In a native Xcode 27.1 control,
Metal encoder labels containing NUL were truncated at that character in the
debugger output, while the probe's own JSON retained the full labels. Original
probe metadata can be stored alongside the replay without losing its escaped
NUL payload values; encoder IDs remain distinct when displayed labels collide.

The MCP server exposes:

GPU captures share the serialized native capture worker with CPU captures;
read/query tools use separate workers and remain responsive during replay.
Cancelling a queued capture prevents it from starting. Cancelling active work waits for the current native debugger command to finish
(with its existing timeout), retains command logs, skips automatic database
storage, and terminates only the
debugger session created by that capture when its ID is known. Cleanup uses only
an unambiguous native session-creation message; unexpected startup output stays
in the retained logs. A session whose startup did not return an ID retains its
180-second idle timeout. Cancellation during a debugger command is deferred because killing its client can leave an
XPC profiling operation running after the session disappears. Other MCP tools
remain responsive while that command finishes.
CLI Ctrl-C during replay also waits for the current bounded debugger command,
closes its owned session, retains artifacts and exits with status 130.
SIGTERM follows the same cleanup path and exits with status 143, including when
SIGINT was ignored by the shell that launched the CLI. Repeated SIGTERM does
not interrupt cleanup. A forced SIGKILL cannot run Python cleanup.

On POSIX, SIGTERM to the stdio MCP server cancels its requests, waits for native
cleanup, and exits with status 143, including when the client keeps stdin open
or stops reading stdout. Allow the current bounded debugger command to finish
before escalating to SIGKILL; a forced process kill cannot run Python cleanup.
HTTP transport retains its server's signal handling.

| Tool | Purpose |
|---|---|
| `collect_apple_gpu_profile(mode, passes, items, execution, process_name, labels)` | Capture the packaged Metal control and store its replay profile |
| `profile_apple_gpu_trace(path, execution, process_name, labels)` | Replay an existing server-local `.gputrace` and store the profile |
| `import_apple_result(path, process_name, labels)` | Store an existing GPU `profile.json` |
| `query_apple_result(run_id, section="encoders", offset=0, limit=100)` | Page through native encoder counters |
| `get_apple_gpu_report(run_id, limit=20, offset=0)` | Rank encoder costs and native limiters |

For example, call `collect_apple_gpu_profile` with
`{"mode":"alu","passes":4,"items":16384,"labels":{"test_name":"m6-gpu"}}`,
then pass its `run_id` to `get_apple_gpu_report`. Collection runs on the server's
Mac; importing, querying and reporting saved results work on any supported OS.
Paths refer to the server host. Report pagination retains original costs without
renormalizing the selected encoders. Compilation/capture timeouts are 90/60
seconds; replay has a 300-second budget plus up to 15 seconds for session cleanup.
Failed collection/storage preserves local artifacts and returns an error.
The run's timestamp range covers the collection workflow; use the native
`timeline` for replay GPU timing and `probe.gpu_seconds` for original command GPU
timestamps. Neither is application CPU time.

### Reproduce the lower-level replay experiment

The source-checkout wrappers use the same packaged collector and report code.
The packaged replay probe and the M1 Instruments probe use different controls;
`alu` reduces loop overhead in the replay probe, while the Instruments `alu`
control retains the original loop. Their `stream` controls also differ: the replay
probe uses vectorized FMAs, while the Instruments probe uses scalar addition.
The packaged probe was also run through the M1 Instruments path below. Use the
matching source and measurement path when reproducing each result.
The one-command M1 runner completed native packaged-probe limiter checks for
all four modes, plus a utilization check for compute. Every requested pass and
required metric was sampled. The 100-pass ALU check still had 13 passes with
less than 10% sampled duration, and the eight-pass stream check had observed
other GPU activity during 33% of sampled probe time. The capture hashes and
coverage are in
[the M1 packaged-probe artifact](apple-gpu-m1-packaged-probe.json).
Use a fresh directory and full Xcode:

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
mkdir /tmp/topdown-gpu-replay-001
xcrun swiftc -O -framework Metal topdown/collector/apple_gpu_probe.swift \
  -o /tmp/topdown-gpu-replay-001/probe

MTL_CAPTURE_ENABLED=1 /tmp/topdown-gpu-replay-001/probe 4 --mode compute \
  --result /tmp/topdown-gpu-replay-001/probe.json \
  --capture /tmp/topdown-gpu-replay-001/compute.gputrace

python3 examples/apple_gpu/replay_profile.py \
  /tmp/topdown-gpu-replay-001/compute.gputrace \
  --output /tmp/topdown-gpu-replay-001/replay
python3 examples/apple_gpu/report_replay.py \
  /tmp/topdown-gpu-replay-001/replay/profile.json
python3 examples/apple_gpu/report_replay.py \
  /tmp/topdown-gpu-replay-001/replay/profile.json --json
```

Repeat with `--mode alu`, `--mode memory --items 16777216`, or
`--mode stream --items 16777216`, and with 8 passes in separate directories.
The default four-pass compute checksum remains `8336.906823405065`, matching the
M1 experiment. `--mode idle` initializes Metal but submits no GPU commands;
use it for Instruments controls, not replay capture.

The collector runs `gpudebug` with default GPU state and overlapping execution.
Use `--execution serial` for a separate serial replay; keep settings matched
when comparing. It creates and terminates its own session, preserves command
logs, and waits at most 30 seconds for the performance tables after Xcode reports
the profile loaded. Missing data and unsupported replay profiling fail clearly.
Saved `profile.json` reports can be read offline on other operating systems.

The probe accepts 1–500 passes, up to 16,777,216 items, and at most 268,435,456
item-passes. Stream item counts must be divisible by 64. Pass counts and GPU
timestamps are recorded in `--result` JSON; checksums are checked against a CPU
reference before success is reported. An existing capture/result is not replaced.

Apple documents [programmatic capture](https://developer.apple.com/documentation/xcode/capturing-a-metal-workload-programmatically),
[the command-line GPU debugger](https://developer.apple.com/documentation/xcode/debugging-with-interactive-command-line-tools),
and [GPU performance counters](https://developer.apple.com/documentation/xcode/analyzing-apple-gpu-performance-using-counter-statistics).
Replay instrumentation and execution settings can change timing; profiler costs
describe the captured workload under those settings.

## M1 finding

On an Apple M1 (7 GPU cores), macOS 27.0.1, Xcode 27.1 (27A9275), the public
`MTLDevice.counterSets` list exposed only `timestamp/GPUTimestamp`. The public
sampling checks accepted stage boundaries, not draw, dispatch, or blit boundaries.
Nevertheless, **Instruments Metal System Trace exported real GPU counter intervals**
when its Performance Limiters profile was selected. A four-pass compute probe
completed with checksum `8336.906823405065` and 1.362 ms summed GPU command time.
The trace's `metal-gpu-counter-intervals` table contained 7,565 rows across 242
sampling intervals. Named counters included ALU Limiter, F32 Utilization, Compute
Occupancy, Buffer Read Limiter, and GPU Read Bandwidth. The Performance Utilizations
profile yielded 5,616 rows across 432 intervals in a separate four-pass capture.
The default CLI Metal System Trace template selected no counter set and produced
zero rows in this table.

`gpudebug profile run` on this M1 reported that its replay profiler requires
Apple M3 or A17 Pro or later. That restriction did not prevent Instruments from
exporting the counter intervals above.

The counter intervals are sampled across GPU activity, not automatically scoped
to our compute passes. Their percentages, bandwidth values, and “Top Performance
Limiter” categories are **not** interchangeable with Intel CPU pipeline-slot TMA
fractions. No bottleneck percentages are claimed from this first capture. Raw
traces stay local because they include activity from other processes.

**Check timestamp overlap, not only row count.** Two fresh recordings that exited
immediately after the probe had counter rows but zero counter intervals overlapping
its labeled GPU passes. The counter stream ended about 31 ms and 80 ms, respectively,
before the first pass. Keeping the recorder alive for one second after the probe
completed produced overlap. On this M1, a four-pass capture then covered all four
passes (12 overlapping counter intervals); a 100-pass capture covered all 100
(284 intervals, 5.37 ms sampled within 34.04 ms GPU work); and a 200-pass capture
covered 199 of 200 (562 intervals, 10.76 ms sampled within 67.53 ms GPU work).
These are sampled portions of labeled work, not complete instruction accounting.

## M1 workload contrast

The `alu` workload performs 1,024 dependent FMA operations per item. The `stream`
workload reads and writes 64 MiB each per pass with one addition per item. Both
verify a deterministic checksum. With the one-second capture tail, the native
counter samples that overlap the labeled passes changed substantially:

| Workload | Passes | GPU work | Sampled work | ALU Limiter | Buffer Write Limiter | GPU Read Bandwidth |
|---|---:|---:|---:|---:|---:|---:|
| ALU | 100 | 34.04 ms | 5.37 ms | 73.61% | 0.11% | 0.18 GB/s |
| ALU | 200 | 67.53 ms | 10.76 ms | 72.92% | 0.11% | 0.09 GB/s |
| Stream | 100 | 225.47 ms | 111.81 ms | 1.33% | 99.44% | 29.60 GB/s |
| Stream | 200 | 466.83 ms | 234.04 ms | 1.61% | 98.26% | 28.93 GB/s |

The table uses overlap-duration-weighted means of **sampled** counter intervals,
not a complete census of each pass. The 100-pass stream capture had no other
active GPU interval overlap in the exported timeline. The 200-pass stream capture
had 17.83 ms of other active GPU interval overlap within 466.83 ms of probe work;
the ALU captures had less than 0.33 ms each. The raw traces remain local. The
sanitized [M1 validation data](apple-gpu-m1-validation.json) includes pass counts,
checksums, coverage, units, and selected sampled means for M6 comparison.

Apple explains that utilization and limiter counters describe related but
different aspects of GPU work, and that a high ALU limiter alone does not prove
a performance problem. The contrast above shows the counter profiles respond to
these two controlled workloads; it does not establish an Intel-style 100% GPU
pipeline-slot partition. In fact, summing the individual limiter sampled means
for the 100-pass stream run gives **260.23%** (Buffer Write 99.44%, Buffer Read
67.15%, MMU 48.63%, GPU Last Level Cache 43.68%, plus smaller values). The
subsystem limiters overlap and cannot be treated as children in a 100% tree.
The `Top Performance Limiter` label provides a narrower, categorical view: ALU
was the highest limiter for 98.68% of the 100-pass ALU run's sampled top-limiter
time, while Buffer Write was highest for 99.98% of the corresponding stream run.
Those are shares of **sampled top-limiter labels**, not shares of GPU time or
pipeline slots. Apple demonstrates this highest-limiter track in its GPU counter
walkthrough.
The summarizer reports unparsed top-limiter sampled time separately. Check that
it is zero before treating the listed category shares as representative of all
sampled top-limiter intervals; it was zero in all six replicated captures.
See Apple's
[GPU counter walkthrough](https://developer.apple.com/videos/play/wwdc2020/10603/),
[Metal compute guidance](https://developer.apple.com/videos/play/tech-talks/10580/),
and [occupancy analysis](https://developer.apple.com/documentation/xcode/finding-your-metal-apps-gpu-occupancy).

The separate **Performance Utilizations** counter set also worked on this M1
with template profile code 4. New 100-pass captures covered all labeled passes:
ALU Utilization was 34.63% for the FMA probe and 1.35% for the streaming probe;
GPU Read Bandwidth was 0.02 versus 29.59 GB/s. The streaming bandwidth agrees
with the Performance Limiters capture (29.60 GB/s). This is an independent
counter-set check, still subject to the same system-wide sampling limits. Its
sanitized results are also in the validation JSON.

### Alternating M1 replication and observed interference

Three fresh ALU/stream pairs, alternated in that order with 100 passes each,
completed checksum verification and had counter samples overlapping every pass.
The device-wide counter means repeated the contrast. Excluding any counter
interval that overlapped *observed* other active GPU work made the distinction
at least as strong:

| Counter, clean sampled intervals | ALU range across 3 runs | Stream range across 3 runs |
|---|---:|---:|
| ALU Limiter | 71.60–73.32% | 1.30–1.33% |
| Buffer Write Limiter | 0.10–0.11% | 99.29–99.46% |
| GPU Read Bandwidth | 0.03–0.08 GB/s | 28.11–28.53 GB/s |

Among those same clean counter intervals, ALU was the highest-limiter label
for 98.55–99.24% of sampled ALU top-limiter time; Buffer Write was highest for
99.96–99.98% of sampled stream top-limiter time. These are categorical shares
of sampled labels, not GPU work-time or pipeline-slot shares.

The unfiltered sampled means, counter coverage, per-run checksums and XML hashes
are in [the replication evidence](apple-gpu-m1-replication.json). The fraction of
sampled probe time with observed other activity was 4.0–6.4% for ALU and
9.3–11.9% for stream. The clean subset discards entire counter intervals that
overlap that activity; it is still a sampled, device-wide measurement. Unseen
activity, boundary contamination, frequency and thermal changes are not ruled
out by these six runs.

An idle control using the packaged Metal probe submitted **zero** GPU commands
and verified its checksum. The same M1 Instruments profile nevertheless
exported 162,631 counter rows across 5,178 sample intervals, alongside 4,298
active GPU intervals attributed to at least three other processes. It had no
labeled probe interval. This confirms why row count cannot establish a
workload-specific bottleneck; the idle control and its XML hashes are retained
in the replication evidence.

A concurrent control launched checksummed ALU (500 requested passes) and stream
(100 passes) probes in one capture. The summarizer rejected the trace without
`--pid`, then selected each probe from the same device-wide counter export.
The export contained 495 labeled ALU passes and all 100 stream passes, each
with counter overlap. The five missing ALU labels are excluded from its
coverage; the probe itself completed and verified all 500 passes. During
observed overlap, the unfiltered ALU Buffer Write Limiter rose to 39.73% and
GPU Read Bandwidth to 23.66 GB/s. On counter intervals without *observed*
other active GPU work, they were 1.15% and 0.77 GB/s. The stream's unfiltered
ALU Limiter was 18.57%, versus 1.96% on that clean subset. This directly
demonstrates that PID selection identifies probe windows but cannot make
device-wide counter values process-specific. The raw XML hashes, sampled
coverage, and both summaries are in [the concurrent evidence](apple-gpu-m1-concurrent.json).
Only 3.53 ms of the ALU's 36.48 ms sampled time remained in the clean subset;
89.47% overlapped observed other GPU activity. The stream overlap was 33.90%
of its sampled time. These fractions are now explicit summary fields. The
cleaned ALU values are a small sample and should not be treated as an isolated
full-run measurement.

A sequential control ran checksummed ALU (500 passes) and stream (100 passes)
processes in a single trace, with a pause between them. All passes had labeled
GPU work, but counter samples overlapped 499 ALU passes and all 100 stream
passes. On sampled intervals without *observed* other GPU activity, ALU Limiter
was 72.16% for ALU versus 1.28% for stream; Buffer Write Limiter was 0.11%
versus 99.37%; GPU Read Bandwidth was 0.04 versus 28.09 GB/s. The sampled
fractions of probe GPU work were 15.86% and 49.11%, respectively, and 3.99%
and 7.72% of that sampled time overlapped observed other activity. Median
per-pass sampled fractions were 17.54% for ALU and 51.09% for stream; three
ALU passes were sampled below 10%, including the unsampled pass. This
within-trace control preserves the workload contrast without deliberate
concurrency, but the missing ALU counter overlap means the checked runner
would reject the capture. The launch script, XML hashes, coverage, and
summaries are in [the sequential evidence](apple-gpu-m1-sequential.json).

Reversing the order in a second trace (stream first, then ALU) preserved the
clean sampled contrast: ALU Limiter was 72.18% for ALU and 1.26% for stream;
Buffer Write Limiter was 0.11% and 99.44%; GPU Read Bandwidth was 0.04 and
28.38 GB/s. All 500 ALU and 100 stream passes overlapped every required
metric. Still, the least-sampled ALU pass had only 5.93% coverage, which the
nonzero-overlap check accepts and the per-pass fraction exposes. Overall
sampled work fractions were 15.74% and 50.75%; observed other GPU activity
overlapped 4.93% and 7.60% of that sampled time. The script, XML hashes,
per-pass coverage and native summaries are in
[the reversed-order evidence](apple-gpu-m1-reversed-sequential.json). These
two same-trace orderings support a repeatable workload contrast, within the
limits of sampled, device-wide counters.

### Same Metal kernels on M1 and M6

The M6 replay probe compiled and checksum-verified on M1 under Instruments.
The M1 summarizer now recognizes its command-buffer and encoder labels as
well as the original M1 probe labels. Four Performance Limiters captures used
the same packaged kernels and item counts as the M6 controls:

| Packaged mode | Passes × items | M1 command GPU time | Passes sampled | Clean ALU Limiter | Clean Buffer Write Limiter |
|---|---:|---:|---:|---:|---:|
| Compute | 100 × 16,384 | 34.43 ms | 100/100 | 72.30% | 0.10% |
| ALU | 100 × 16,384 | 7.02 ms | 76/100 | 23.02% | 0.20% |
| Memory | 8 × 16,777,216 | 19.50 ms | 8/8 | 2.50% | 98.39% |
| Stream | 8 × 16,777,216 | 20.09 ms | 8/8 | 1.61% | 99.17% |

Compute and optimized ALU returned the same checksum. Their original-command
GPU times differed by 4.90× in these separate M1 captures. The optimized
ALU commands were short enough that 24 passes had no counter overlap and 37
had under 10% sampled coverage, so its limiter mean needs caution. Memory
and stream also returned the same checksum; their clean sampled GPU Read
Bandwidth means were 27.51 and 26.41 GB/s. Observed other activity overlapped
13.30% and 30.83% of their sampled work, respectively. The selected template,
probe, TOC and XML hashes, checksums, per-metric coverage and unfiltered means
are in [the packaged-probe evidence](apple-gpu-m1-packaged-probe.json).
The kernels now match across hosts, but M1's device-wide Instruments samples
and M6's captured-workload replay counters have different scopes and native
limiter meanings. Their percentages cannot be compared as one TMA scale.

A matched-width control increased both packaged compute and ALU commands to
65,536 items for 100 passes. In the Performance Limiters pair, all passes
overlapped the required metrics; original-command GPU time was 66.66 versus
22.71 ms, and clean ALU Limiter was 89.72% versus 71.17%. The independent
Performance Utilizations set showed a different direction: clean F32
Utilization was 14.26% for compute and 64.33% for optimized ALU, while total
ALU Utilization was 42.85% and 68.65%. A reversed-order utilization pair
repeated the F32 split at 14.21% versus 65.76%; all its passes were sampled.
The first utilization pair sampled 100 compute and 99 ALU passes. The three
separate capture pairs gave compute/ALU GPU-time ratios of 2.93, 3.39, and
3.33. Native limiter and utilization percentages have different meanings,
and the shorter 16,384-item ALU control had 24 unsampled passes and a much
lower ALU Limiter. The earlier limiter difference therefore cannot be assigned
to the kernel change alone. The checksums, source and XML hashes, pass coverage,
both counter sets, and reordered replication are in
[the width-control evidence](apple-gpu-m1-packaged-width-control.json).

Four further M1 traces placed both matched-width packaged kernels in each
capture, then reversed their order under each counter profile. The two probe
processes ran sequentially, and the same device-wide counter export was
summarized once per recorded PID:

| Counter profile and order | Clean ALU Limiter, compute / optimized ALU | Clean F32 Utilization, compute / optimized ALU | Passes with counter samples, compute / optimized ALU |
|---|---:|---:|---:|
| Limiters, compute first | 89.64% / 70.82% | 14.22% / 64.74% | 100 / 99 |
| Limiters, ALU first | 89.22% / 69.43% | 14.17% / 63.45% | 100 / 100 |
| Utilizations, compute first | — | 14.22% / 64.32% | 100 / 100 |
| Utilizations, ALU first | — | 14.16% / 64.57% | 100 / 100 |

Both modes returned the same checksum in every trace. The first limiter trace
missed counter overlap on one short optimized-ALU pass; all other passes had
samples for the required metrics. Observed other activity overlapped 3.6–9.9%
of sampled probe work across these summaries. Compute/ALU command GPU-time
ratios ranged from 3.07 to 4.26, so this control supports the direction of the
kernel effect rather than a fixed speedup. ALU remained the most frequent
*top-limiter label* for both kernels, showing why that label alone cannot
describe F32 utilization or the size of the ALU limiter. An additional
per-pass overlap-weighted check found no overlap between the sampled F32
Utilization ranges in these four traces: compute was at most 15.29%, while
every sampled optimized-ALU pass was at least 26.68%. These are unfiltered,
device-wide samples, and one ALU pass in the first limiter trace was unsampled.
The same-trace
checksums, PID matches, counter coverage, sampled and clean means, and hashes
are in [the paired-trace evidence](apple-gpu-m1-packaged-paired-traces.json).

The packaged memory and vectorized stream controls also ran sequentially in
one M1 trace in both orders, each with eight passes over 16,777,216 floats:

| Order | Clean GPU Read Bandwidth, memory / stream | Clean Buffer Write Limiter, memory / stream | Command GPU time, memory / stream |
|---|---:|---:|---:|
| Memory first | 27.19 / 27.90 GB/s | 99.02% / 98.73% | 20.38 / 19.32 ms |
| Stream first | 28.88 / 27.66 GB/s | 99.60% / 98.23% | 18.92 / 19.41 ms |

Every requested pass and required metric had counter overlap, and both modes
returned the same checksum in each trace. The similar native sampled bandwidth
and limiter readings support the earlier separate-capture comparison. Which
mode had higher sampled read bandwidth changed with execution order, so these
traces do not show a consistent M1 bandwidth advantage for either kernel. Observed
other GPU activity still overlapped 6.9–17.9% of sampled work; clean means
retain only intervals without that observed overlap. GPU Read Bandwidth is a
native GPU counter, and these readings do not prove DRAM saturation or map to
the M6 replay launch limiter. The checksums, PID matches, sample coverage and
hashes are in [the memory/stream paired evidence](apple-gpu-m1-packaged-memory-stream-paired.json).

## Reproduce the Instruments comparison

For a checked M1 capture in one command, set the full Xcode developer directory
and use a new output directory:

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
python3 examples/apple_gpu/record_instruments.py \
  --output /tmp/m1-gpu-alu-001 --mode alu --passes 100 --profile 3
python3 examples/apple_gpu/record_instruments.py \
  --output /tmp/m1-gpu-stream-001 --mode stream --passes 100 --profile 3

# Use the packaged M6 probe kernels on M1's sampled counter timeline.
python3 examples/apple_gpu/record_instruments.py \
  --probe-source packaged --mode compute --passes 100 --items 65536 \
  --output /tmp/m1-gpu-packaged-compute-001
python3 examples/apple_gpu/record_instruments.py \
  --probe-source packaged --mode memory --passes 8 --items 16777216 \
  --output /tmp/m1-gpu-packaged-memory-001
python3 examples/apple_gpu/report_instruments.py \
  /tmp/m1-gpu-packaged-compute-001/profile.json
# Use --json for a compact machine-readable report.
```

The runner verifies the M1 probe checksum and target exit, checks that Xcode
selected the requested counter profile, exports both tables, and requires
counter overlap with every requested probe pass. It also requires each expected
counter metric to overlap every pass, checks units, and verifies decoded
top-limiter labels for profile 3. It saves the trace, XML, command logs and
`profile.json`. `--profile 4` selects Performance Utilizations.
For the packaged probe, it also validates the saved `probe.json` checksum,
mode, item count, and ordered command labels. It requires the observed pass
indices to run from zero through the requested last pass. For either probe it
binds the selected counter PID to the compiled probe path in Xcode's trace
table of contents, and saves the compiled binary hash. The packaged default
is 100 passes and 65,536 items for compute/ALU, or 8 passes and 16,777,216
items for memory/stream; the original M1 probe keeps its existing defaults.
Native checks passed for both the original four-pass M1 ALU probe and the
packaged 100-pass compute probe with this PID binding. Their hashes and TOC
process matches are in [the PID-binding evidence](apple-gpu-m1-pid-binding.json).
Very short packaged ALU commands can miss individual counter samples, causing
the checked runner to reject an otherwise completed probe.
Failed checks leave the artifacts for inspection in the new output directory.
This experimental M1 summary is separate from the packaged GPU replay profile
format used by the database and MCP commands above.
The tested M1 Performance Limiters and Performance Utilizations exports have
32 and 13 counter names respectively, with no `Control Flow` counter in either
inventory. They cannot reproduce M6's per-encoder Control Flow limiter; the
M1 compute/optimized-ALU comparison uses sampled ALU and F32 counters instead.
The saved-profile report ranks the sampled limiter and utilization means within
each counter type, shows bandwidth separately, and prints the sampled fraction,
sparse-pass count, and overlap with observed other GPU activity. Its compact
JSON retains the selected PID and capture hashes; the text report states whether
the capture bound that PID to the compiled probe. Older profiles without this
check remain readable and are labeled as unverified. Its clean means
exclude counter intervals that overlap observed other activity and show how
much sampled time remains for each metric; they do not make device-wide
counters process-specific. New captures also report median and range of sampled
per-pass means for key metrics. Those pass distributions overlap-weight the
native counter intervals and remain unfiltered device-wide observations; they
are not a complete census of each pass. Each sampled pass contributes one mean
to the reported quantiles, regardless of its duration or number of samples.
When GPU activity intervals are present, the report also gives a separate
per-pass summary after excluding entire counter intervals that overlap
observed other activity. Filtered pass coverage can be
sparse, so its text report uses the median and p10–p90 rather than extremes.
A fresh 100-pass packaged compute capture on this M1 completed the checked
runner with per-pass ALU Limiter values from 73.47% to 92.93% and F32
Utilization from 11.73% to 15.23%. GPU Read Bandwidth has a 0.26 GB/s sampled
pass median, a 0.12–4.48 GB/s range and a 0.38 GB/s pooled mean; the high
pass cannot be assigned to the probe from device-wide counters. Its source,
binary, trace-table and XML hashes are in [the packaged-probe
evidence](apple-gpu-m1-packaged-probe.json).
Recomputing that hashed export with the conservative activity filter leaves 98
passes for GPU Read Bandwidth: the median is 0.26 GB/s and p10–p90 is
0.18–0.33 GB/s, versus a 0.27 GB/s pooled clean mean. This narrower upper tail
is consistent with contamination from observed other activity; it does not
establish process-exclusive bandwidth.
A fresh checked capture with this reporting code sampled all 100 packaged
compute passes, verified the compiled-probe PID, and retained clean samples
for all 100 passes of the key metrics. Observed other activity overlapped 6.60%
of sampled probe work. The clean ALU Limiter pass median was 89.30% with
p10–p90 of 86.47–92.47%; clean GPU Read Bandwidth had a 0.33 GB/s median and
0.24–0.47 GB/s p10–p90. Capture and XML hashes, means and both pass
distributions are in [the fresh M1 validation](apple-gpu-m1-clean-pass-validation.json).
The same runner completed a fresh Performance Utilizations capture with all
100 passes sampled and a verified probe PID. Its clean F32 Utilization pass
median was 14.20%, with p10–p90 of 13.71–14.73%; 5.72% of sampled work
overlapped observed other GPU activity. The same evidence file includes its
separate capture and XML hashes.
Reanalyzing the earlier eight-pass packaged memory and stream captures with the
same code retained all eight sampled and clean passes for both workloads.
Their clean GPU Read Bandwidth pass medians were 29.29 and 26.96 GB/s, with
Buffer Write Limiter medians of 98.83% and 99.45%, respectively. The original
means and coverage remained unchanged; the source-profile and XML hashes are
recorded in the fresh M1 validation file.
On this M1, a four-pass profile-3 ALU smoke check sampled all four passes;
a fresh four-pass check covered 16.28% of GPU work time with counter samples.
A 100-pass profile-4 stream capture sampled all 100 and reported 1.77% ALU
Utilization and 28.18 GB/s GPU Read Bandwidth. The checked-runner hashes and
coverage are in the replication evidence.

The equivalent lower-level commands are shown below for template and export
inspection.

Use Xcode's developer directory; Command Line Tools alone do not include
Instruments. Set `DEVELOPER_DIR` to the actual Xcode app location if it differs.
The output directory must be new. The template helper copies Apple's installed
template and edits the two `counterprofile` settings in the copy. It fails if the
template layout or defaults differ; it never edits the Xcode installation.
On this Xcode version, `xctrace record --show-recording-options` did not expose
Metal counter selection; trying a `Metal Application` JSON entry with
`--recording-options` returned “does not support recording options.” The copied
template is the reproducible CLI route tested here.

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
mkdir /tmp/topdown-gpu-check-001
xcrun swiftc -O -framework Metal examples/apple_gpu/probe.swift \
  -o /tmp/topdown-gpu-check-001/probe

python3 examples/apple_gpu/make_counter_template.py \
  /Applications/Xcode.app/Contents/Applications/Instruments.app/Contents/Packages/GPU.instrdst/Contents/Templates/Metal\ System\ Trace.tracetemplate \
  /tmp/topdown-gpu-check-001/limiters.tracetemplate --profile 3

xcrun xctrace record \
  --template /tmp/topdown-gpu-check-001/limiters.tracetemplate \
  --output /tmp/topdown-gpu-check-001/limiters.trace \
  --time-limit 8s --no-prompt --target-stdout - \
  --launch -- /bin/sh -c '/tmp/topdown-gpu-check-001/probe 4 && sleep 1'

xcrun xctrace export --input /tmp/topdown-gpu-check-001/limiters.trace \
  --toc --output /tmp/topdown-gpu-check-001/toc.xml
xcrun xctrace export --input /tmp/topdown-gpu-check-001/limiters.trace \
  --xpath '/trace-toc/run[@number="1"]/data/table[@schema="metal-gpu-counter-intervals"]' \
  --output /tmp/topdown-gpu-check-001/counters.xml
xcrun xctrace export --input /tmp/topdown-gpu-check-001/limiters.trace \
  --xpath '/trace-toc/run[@number="1"]/data/table[@schema="metal-gpu-intervals"]' \
  --output /tmp/topdown-gpu-check-001/gpu-intervals.xml
python3 examples/apple_gpu/summarize_counters.py \
  /tmp/topdown-gpu-check-001/counters.xml \
  --gpu-intervals /tmp/topdown-gpu-check-001/gpu-intervals.xml
```

The shell exits with the probe's status and waits one second only on success. The
wait gives Instruments time to retain delayed counter samples. Confirm the TOC
says `Counter Set: Performance Limiters`, the probe prints `checksum_verified=true`, and
`probe_coverage.passes_with_counter_samples` is nonzero. `counter_rows` alone
does not establish probe coverage. Use 100 and 200 passes for longer comparison
captures; the four-pass run is a smoke check. Inspect sampled time, pass coverage,
and `other_active_gpu_time_overlapping_ns` before interpreting
`sampled_counter_means`. The latter are weighted only over sampled intervals.
`other_active_gpu_time_in_counter_samples_ns` narrows the interference check to
the sampled portions of probe work. `sampled_gpu_work_fraction` and
`observed_other_activity_fraction_of_sampled_gpu_work` put coverage and
observed interference on a 0–1 scale. Minimum, median and maximum sampled
fractions per pass, plus the count sampled below 10%, expose sparse coverage
even when every pass has some counter overlap. Per-metric pass counts reveal
missing limiters in an otherwise sampled pass. The summary records
`counter_scope` as `device-wide` and `probe_coverage.selected_pid` for
provenance; selecting a PID does not isolate the native GPU counters from
concurrent processes.
Counter samples that straddle a probe boundary can still include outside GPU
work, so zero observed overlap does not certify isolation.
The summarizer reads exported counter rows incrementally. A 202 MB M1 XML
export used 376 MB peak resident memory with per-metric coverage enabled. An
earlier 183 MB export used 284 MB after the streaming change, versus 3.07 GB
before it, and produced byte-identical JSON. If a trace includes multiple
labeled probe processes, it fails until `--pid PID` selects one. The native
counter stream is still system-wide; concurrent GPU activity remains visible in
the overlap fields. Counter intervals for each named metric must arrive in time
order without overlap; otherwise the weighted mean is rejected.
To run the memory control, substitute `probe 100 stream` in the shell command and
use a new trace/output directory. `probe 100 alu` selects the original workload
explicitly.
To try Performance Utilizations, create a second template with `--profile 4` and
capture to another new trace path. Profile numbers 3 and 4 are observations from
Xcode 27.1, not a public compatibility contract; verify the TOC on each Xcode
version. Other numeric profile candidates were unsupported on the tested M1.

Six additional M6 captures used the default template, profiles 3/4, 128/256-pass
compute controls and an idle Metal process. The TOCs retained the requested
profile IDs, but the counter tables varied between empty and RT Unit Active
only, including under idle. Compute captures contained 128/256 target GPU
intervals; idle contained zero. All included other-process GPU activity.
The evidence records those counts, inventories and source hashes. It does not
establish why Instruments exposes this limited/inconsistent inventory on M6.

Before reporting a GPU bottleneck share, compare idle and matched N/2N probe
captures, check concurrent GPU activity, and confirm each counter's unit and
denominator. The summary script reports row counts, timestamp overlap, and native
sampled means with units. It does not convert them to CPU TMA fractions. For
example, the native `GPU Read Bandwidth` row is in GB/s and has
`is-percentage=0`, despite the generic schema also containing a `percent-value`
column.

Apple's [Metal performance analysis guide](https://developer.apple.com/documentation/xcode/analyzing-the-performance-of-your-metal-app/)
describes selecting Performance Limiters in Instruments. Apple's
[programmatic capture guide](https://developer.apple.com/documentation/xcode/capturing-a-metal-workload-programmatically)
documents a separate GPU trace capture path.
