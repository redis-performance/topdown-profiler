# Apple silicon profiles

Native CLI profiling is available through `topdown apple`. Process-accounted
instruction/cycle totals and fixed-counter samples with stacks work on the tested
M6 and M1. Native system/cluster firmware histograms and trace scheduling context
are also available on M6. The M1 accepts three experimental programmable-event groups.
Xcode 27.1's CPU Counters template exposes four native bottleneck categories on
this M1. Apple results share SQL storage and MCP access with Linux runs, while
retaining native units and staying separate from the Linux slot-TMA funnel.
They add no runtime dependencies and do not invoke `sudo` or change host settings.
The tested M6 rejects programmable events. Intel slot-TMA equivalence has not
been established for the Apple categories.

## Check support and view function hotspots

```sh
topdown apple doctor
```

`doctor` reads tool and kernel metadata without recording a workload. Its
`programmable_counters` field reports `known_kernel_blocker` for the measured M6
release kernel UUID `77F5F1F1-C717-3A42-A5FA-487ACF280495`. That kernel rejected
programmable event selectors in the earlier root sweep and in the Xcode 27.0/27.1
Instruments probes. The diagnostic identifies affected `counters`/`cpu` modes
and suggests process accounting or fixed-counter stacks. Other kernels remain
`unverified`; CPU names, installed templates, and Developer Mode alone do not
establish hardware access. Use `apple probe` to check an unfamiliar kernel.
Doctor's exit status continues to indicate whether the host and tools are
available; inspect `programmable_counters.status` for this specific limitation.

For a readable hotspot view on M6 or M1, capture fixed-counter stacks, then
report the exported profile:

```sh
topdown apple record --mode fixed --duration 5 \
  --output /tmp/apple-hotspots-001 -- /path/to/program args
topdown apple report /tmp/apple-hotspots-001
topdown apple report /tmp/apple-hotspots-001/profile.json --limit 10 --json
```

The report groups functions by symbol and binary, combining instruction addresses
within the same named function. `Leaf samples` counts observations at the top of
the stack; `In stack` counts each function once per sampled stack, including
callers and deduplicating recursion. `Leaf share` divides by all measured rows,
including rows without stack frames; unmeasured and missing-stack counts remain
visible. `--limit` truncates the ranking without changing its denominator. The
terminal view shortens long symbols to fit; `--json` retains their full names.
Time Profiler, CPU Profiler, and experimental M1 event stack exports can also be
read offline, including on non-Apple hosts.

These are sample-frequency hotspots. The report does not sum counter values or
native weights, infer CPU-time percentages, or produce TMA percentages. `stat`,
`soc`, guided bottleneck tables, and execution intervals require their own native
interpretation and cannot be read as sampled-stack profiles.

The Xcode 27.1 recheck and native hotspot smoke test are recorded in
[M6 diagnostics validation](apple-m6-diagnostics-validation.json).

## Database storage and MCP

`apple stat`, `apple record`, and `apple soc` accept `--store`. Storage uses the
existing database configuration: SQLite by default (`--db PATH` or
`TOPDOWN_DB_PATH`), or PostgreSQL with `TOPDOWN_BACKEND=postgresql` and
`TOPDOWN_DSN`. Install PostgreSQL support with
`pip install 'topdown-profiler[postgresql]'`. Raw trace bundles and logs remain
in the output directory; the database stores the complete exported native JSON
and, for `profile.json`, its sibling `capture.json` metadata when present.

```sh
topdown apple record --mode fixed --duration 5 --store \
  --label test_name=apple-fixed --label git_branch=main \
  --output /tmp/apple-stored-001 -- /path/to/program args

# Or import an existing profile.json, stat.json, soc.json, or capture directory.
topdown apple store /tmp/apple-hotspots-001 --process redis-server \
  --label test_name=apple-import

topdown apple query RUN_ID
topdown apple query RUN_ID --section samples --offset 0 --limit 100
topdown apple report --run-id RUN_ID --limit 10
topdown list --label test_name=apple-fixed
```

Each import creates a new run. Apple runs appear in the common run list and
support the same label filters, with `collector=apple` and `measurement_kind`
labels. SQLite stores the native document as JSON text; PostgreSQL uses JSONB.

New `stat` and `soc` captures include the native chip model, OS build and kernel
UUID, so stored M1 and M6 runs can be selected by their recorded `cpu` labels.
These fields come from the capture host's `sysctlbyname` API without launching
another process or requiring Instruments. Unavailable metadata is recorded as
an error alongside the fields that were read successfully; it does not prevent
counter collection.

Imports retain the recorded host identity, including when imported on Linux or
a different Mac. Recorded hardware/tool fields take precedence over conflicting
user labels. User labels can fill fields absent from older artifacts; import
never fills missing provenance from the current host.
Unsigned counters, including values above 2^53, remain integers through storage
and Python/MCP serialization. Clients must also handle large JSON integers
without converting them to floating point.

The importer retains source scope, native M1 bottleneck shares, unmeasured rows,
trace gaps, and null Intel TMA fields. It does not create floating-point Linux
metric rows. Capture timestamps are used when available; otherwise the run is
explicitly labeled `time_basis=import_time`, with unknown capture duration.
The source SHA-256 and import timestamp are retained. Imports verify that
performance/other-core instruction, cycle and raw Mach-time pairs add up exactly,
and that PID interval counts agree with their before/after snapshots. Converted
nanoseconds retain their native timebase and documented rounding. SoC channel/state identities,
units, totals and any reported shares must agree; zero observations retain null
shares. XML exports require known native column/cell types and unsigned integer
values; PID and nanosecond-window arguments must be integers. Trace gap counts must match the markers for each thread and interval.
Scheduling coverage, Running spans and core assignments must agree with each
other; missing core attribution never becomes an assumed core. Duplicate JSON
fields, non-finite numbers and invalid supplied timestamps
are rejected before insertion, including in capture metadata. UTC timestamps
ending in `Z` are supported across Python 3.10–3.13. A failed automatic capture
is not stored; database errors leave capture artifacts available for retry with
`apple store`.

Start the existing server with `topdown mcp-serve` (stdio), or use its existing
HTTP transport and authentication configuration. Both transports expose:

| Tool | Purpose |
|---|---|
| `apple_doctor()` | Inspect tools and kernel capabilities on the server host |
| `collect_apple_profile(mode, command, pid, duration_seconds, process_name, labels)` | Capture and store native results, returning a run ID and artifact path |
| `import_apple_result(path, process_name, labels)` | Import a server-local JSON file or capture directory |
| `query_apple_result(run_id, section, offset, limit)` | Read native summaries or observations, with pages of 1–1000 rows |
| `get_apple_hotspots(run_id, limit)` | Rank sample-frequency function hotspots from a stored stack profile |

For example, call `collect_apple_profile` with
`{"mode":"stat","pid":1234,"duration_seconds":5,"labels":{"test_name":"redis-m6"}}`,
then pass its `run_id` to `query_apple_result`. For stacks, use
`{"mode":"fixed","command":["/path/to/program","arg"],"duration_seconds":5}`
and pass the run ID to `get_apple_hotspots`. `stat` accepts either an existing
PID or a command; Instruments modes launch a command and require whole seconds.
`soc` has system/cluster scope and accepts neither PID nor command. MCP capture
duration is capped at 300 seconds; launched `stat` commands use it as a timeout.
Native captures run in one worker so profiling sessions remain serialized, while
read-only Apple tools use a separate worker pool to keep the server responsive.
Cancelling a queued capture prevents it from starting. Cancelling an active
capture stops its owned work, retains partial artifacts and skips automatic
storage; PID observation never signals the observed process.

The MCP server must run on macOS to collect, and resolves executable/file paths
on that host. Imports, database queries, and reports can run on other supported
OSes. The summary omits bulk arrays; use `section=samples`, `intervals`, `gaps`,
`channels`, `leaf_sample_counts`, or GPU `encoders` to page through the relevant native data.
SQLite and PostgreSQL project summaries and native array pages inside the
database, avoiding full-document loads into the server for each small query.
The complete document remains available for reporting and provenance, and
pages retain original order, native integer precision and their full-array total.
`list_profiling_runs` discovers both Linux and Apple runs. Linux metric
comparisons and funnels continue to operate on Linux TMA rows.

## Process instruction and cycle totals

`stat` uses macOS `proc_pid_rusage` accounting. It needs neither root nor Xcode and
supports Python 3.10–3.13. Use a new output directory:

```sh
# Capture the complete lifetime of one launched program.
poetry run topdown apple stat --output /tmp/apple-stat-001 \
  -- /path/to/program args

# Observe an existing process for five seconds without stopping it.
poetry run topdown apple stat --pid 1234 --duration 5 \
  --output /tmp/apple-stat-pid-001
```

The command prints JSON and retains `stat.json`. Launched targets also retain
`target.stdout`, `target.stderr`, executable/collector hashes and exit status.
`counters` contains unsigned integer instruction/cycle counts and user/system
times converted from Mach time to nanoseconds. Counts cover all threads in one
PID, exclude descendant processes, and retain the source API's semantics.

For launched commands, startup and shutdown are included. The final read occurs
after exit while the child is still waitable, before reaping it; short programs
therefore retain their counts. `--timeout` defaults to 30 seconds. On timeout or
interrupt, the owned process group receives SIGTERM, with a two-second grace
period before SIGKILL. Counts include any shutdown work during that grace period.
Timeout exits 5, interruption 130; ordinary target exit codes are propagated.
`target_completed` requires an observed successful exit without timeout/interrupt.

With `--pid`, counts are differences between snapshots. Process start identity is
checked to reject PID reuse, and the command never signals or waits for the target.
An idle interval can validly contain zero deltas when cumulative native counts
establish counter access. If the process disappears before the final read, the
command reports failure and retains the initial snapshot without inventing totals.

The implementation uses the macOS accounting interface described in Apple's
[Recount documentation](https://github.com/apple-oss-distributions/xnu/blob/main/doc/observability/recount.md).
The totals are not a TMA decomposition, and no IPC, frequency or residency estimate
is derived. macOS also exposes basic instruction/cycle totals through
`/usr/bin/time -l /path/to/program`; `stat` adds structured output and PID intervals.

When `RUSAGE_INFO_V6` is available, `stat` also reports
`performance_instructions` and `performance_cycles` from Apple's performance-core-only
fields, plus performance-core user and system time. The matching `other` counts
and times are aggregate totals minus those performance-core values. On a
two-core-kind M1 these are the efficiency-core remainder; on other chips, retain
the broader `other` label. The selected flavor is recorded in `source`, and older
systems fall back to V4 without these fields. These counts do not reveal per-core
pipeline slots or programmable events.

New captures preserve `user_time_mach`, `system_time_mach` and their available
performance/other-group counterparts, alongside `source.mach_timebase`.
Nanosecond values are floored independently from each native counter. The
remainder is subtracted in Mach units before conversion so an idle group's
converted time stays monotonic. Converted split sums can differ from the
aggregate by 1 ns; interval differences can carry either sign of that rounding.
Instructions, cycles and raw Mach-time pairs still add up exactly. Import checks
the conversion and before/after evidence instead of silently adjusting counters.

### M6 counted-instruction control

The [counted ARM loop](../examples/apple-counted-loop.c) executes one `add`, one
`subs`, and one conditional branch per iteration, and verifies its output.
[Recorded M6 validation](apple-m6-fixed-instruction-validation.json) includes
48 full-process captures and 48 startup-barrier intervals across four iteration
counts, default/background QoS requests, and a balanced repeated order.
The interval starts after executable initialization and includes the loop plus
its small I/O protocol. Counts still include user and kernel work.

| Iterations | Planned loop instructions | Full-process median, default QoS | Interval median, default QoS |
|---|---:|---:|---:|
| 0 | 0 | 10,882,834.5 | 20,620 |
| 1,000,000 | 3,000,000 | 13,950,805 | 3,020,863.5 |
| 10,000,000 | 30,000,000 | 40,890,899 | 30,031,992.5 |
| 100,000,000 | 300,000,000 | 311,065,217 | 300,072,853 |

Each median uses six trials. Subtracting the zero-loop interval median gives
ratios of 1.00008–1.00160 relative to planned loop instructions across the
nonzero sizes and both QoS requests. This supports the fixed-accounting path for
these controls and illustrates startup overhead. It is not a calibration of
programmable events, per-core width, or TMA shares. QoS is a scheduling request,
not a core pin; native performance/other counts and all raw times remain in the
evidence. Frequency, scheduling and background activity were uncontrolled.

Run the validation from a source checkout on Apple silicon with Xcode:

```sh
python3 examples/apple-validate-fixed-instructions.py --output /tmp/m6-fixed-check-001
# Each trial retains its full-process stat.json and paired interval.json.
topdown apple store /tmp/m6-fixed-check-001/trial-01-default-1000000/interval.json \
  --process counted-loop --label test_name=m6-fixed-interval
```

The runner retains source/binary hashes, compiler logs, disassembly and native
results. The same control exposed a false counter-decrease error caused by
rounding a derived remainder; the raw-time conversion above fixes it without
discarding the valid interval.

The [M1 paired check](apple-m1-fixed-instruction-validation.json) repeats the
same control after that fix. Three default-QoS 100-million-iteration runs minus
zero-loop baselines yielded 299,951,004–300,105,777 instructions against the
300-million planned loop instructions; three background-QoS pairs yielded
300,790,586–301,529,498. Every native V6 capture completed with exact
performance/other instruction, cycle and raw Mach-time partitions, and null
Intel TMA. Default runs were performance-core dominant; background runs were
other-core dominant. Separate-run startup variation can make a small
per-core-kind *difference* negative without any cumulative counter decrease.
The result checks process accounting within startup variation, not slot-TMA.
A further 16-case startup-barrier run used the same M6 validation script on M1.
After subtracting the zero-loop interval median, the instruction ratio to the
planned loop was 1.00021–1.00109 for default QoS and 0.99997–1.00392 for
background QoS across one, ten, and 100 million iterations. These intervals
remove most startup work but still include the short measurement protocol and
kernel activity. The raw-result hash, native counter splits and per-case counts
are retained in the M1 evidence file.

## Xcode's four-category M1 bottlenecks

On the tested M1, Xcode 27.1's standard CPU Counters template records four
per-thread, per-core ratios: Delivery, Discarded, Processing, and Useful.
Apple describes these in [Addressing CPU bottlenecks](https://developer.apple.com/documentation/xcode/addressing-cpu-bottlenecks).
`topdown apple record --mode counters` reads Xcode's `MetricTable` intervals,
checks that each has all four ratios plus a cycle count, and reports shares
weighted by the cycles in valid intervals. The JSON includes per-core breakdowns
and the count of incomplete or inconsistent intervals. It also reports the
valid fraction of scoped sampled cycles when every invalid interval has a cycle
count; otherwise that fraction is `null`. A legacy `cpu-pmc-value`
table remains supported where Xcode exports one. This is an Apple-native
four-category analysis; the `tma` object remains null because the Intel slot
classification and Apple's formulas have not been shown equivalent.
The overall shares are weighted by sampled core cycles. For a process that runs
on both P and E cores, this is not an average weighted by each core kind's peak
instruction bandwidth; use the per-core breakdown to interpret migrations.
Xcode also exports `CounterMetricByThread` arrays. Matching 1,767 intervals
against `MetricTable` identified their four positions as rounded
Useful/Processing/Delivery/Discarded shares scaled by 10,000; the best
permutation's mean absolute four-category error was 0.00020 in fractional
units, versus 0.0080 for the next best. These arrays are derived shares,
not raw programmable PMU event counts. The profiler reads the named
`MetricTable` ratios instead.
Across the eleven P/E control captures, 49,276 intervals passed the complete
four-ratio and near-unity checks. Thirty-seven inconsistent intervals were
excluded; they represented 0.00058% of reported cycles. Every control's
cycle-weighted shares summed to within 0.0002% of one. This demonstrates a
near-disjoint Apple partition in the exported data, while leaving the exact
hardware formulas and Intel slot equivalence unresolved.

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  poetry run topdown apple probe --mode counters --output /tmp/m1-guided-probe-001
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  poetry run topdown apple record --mode counters --duration 5 \
  --output /tmp/m1-guided-branch-001 -- /tmp/apple-m1-controls branch_random 700000000
```

The tested host still has Command Line Tools selected, so these commands set
`DEVELOPER_DIR` for one process without changing the system-wide selection.
After the shared M6 GPU and host-provenance changes, a fresh M1 wide-ALU CLI
capture still exported 4,062 valid guided intervals and left all Intel `tma`
roots null. SQLite stored the profile unchanged, retained the recorded M1,
kernel and Xcode labels, and created zero Linux metric samples. Its command,
hashes, cycle-weighted shares and round-trip check are in
[the post-sync M1 smoke](apple-m1-post-sync-smoke.json).
The same binary also completed a fresh `apple stat` run through
`RUSAGE_INFO_V6`: performance-core cycles were 99.83% of its process-lifetime
total, the `other` remainder reconciled exactly, and Intel TMA remained null.
That count and its source hash are in the same smoke artifact.
After the user accepted Xcode's license, the guided probe returned
`counters_present`; the earlier CLI rejected this Xcode version because it
looked only for `cpu-pmc-value`. Controlled Xcode captures yielded:

| Control | Delivery | Discarded | Processing | Useful |
|---|---:|---:|---:|---:|
| Wide independent ALU | 0.01% | 0.01% | 16.67% | 83.31% |
| Random branch | 57.48% | 34.85% | 0.00% | 7.66% |
| Random pointer chase | 0.07% | 0.01% | 99.44% | 0.48% |
| Compact NOP stream | 0.17% | 0.00% | 0.01% | 99.82% |
| Compact stream, large unexecuted text | 0.18% | 0.01% | 0.01% | 99.81% |
| Large executed NOP stream | 72.82% | 0.01% | 0.01% | 27.17% |

All six records completed their target, and more than 99.9% of the valid
interval cycles were on P cores. The raw `MetricTable` XML hashes, exact ratios,
commands, and event captures are in [M1 Xcode validation](apple-m1-xcode27-validation.json).
Launching with `/usr/sbin/taskpolicy -b` put more than 99.95% of measured
cycles on E cores. The guided E-core results also formed a four-way breakdown:

| E-core control | Delivery | Discarded | Processing | Useful |
|---|---:|---:|---:|---:|
| Wide independent ALU | 16.69% | 0.02% | 0.02% | 83.27% |
| Random branch | 43.79% | 40.59% | 0.28% | 15.34% |
| Random pointer chase | 0.10% | 0.02% | 99.01% | 0.88% |
| Compact NOP stream | 0.13% | 0.01% | 0.02% | 99.84% |
| Large executed NOP stream | 47.80% | 0.01% | 0.02% | 52.17% |

The wide ALU loop's useful share is near 83% on both core kinds, consistent
with the earlier fixed-counter IPC observations divided by their respective
eight- and four-uop peak widths. The remaining share is classified as
Processing on P cores and Delivery on E cores, a reminder that these are
core-specific native classifications, not a portable slot formula.
The guided view provides the practical four-way diagnosis on this host. The
custom `m1`/`m1-map`/`m1-fetch` groups below expose individual hardware events
for calibration and deeper causes; they do not themselves produce slot shares.

## M1 top-down feasibility

On an M1 MacBook Air with macOS 27.0.1, launched and PID-interval `stat` counts
worked, including V6 performance-core instructions, cycles, user time and system
time. The `other` remainder was nearly zero in ordinary CPU-bound runs. Running
the same ALU control through `/usr/sbin/taskpolicy -b` moved more than 99.95% of
its counted cycles into `other`, confirming the M1 efficiency-core interpretation
for this host. Three runs gave median 6.645 instructions/cycle on P cores and
3.312 on E cores. These are instruction-throughput observations, not pipeline-slot
shares; both include process startup and any kernel work. The full controls and
counts are in [M1 validation](apple-m1-validation.json).

The M1 KPC interface reports two fixed and eight configurable counters. The
installed event database has 58 programmable events. Their names and selectors
match the T8103 allowlist in [Apple's XNU source](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/arm64/cpc_arm64_events.c).
The running T8103 kernel has the same UUID as the locally inspected kernel
binary. All 58 installed programmable name/selector pairs occur in its event
table. Offline disassembly of its `kpc_set_config` path shows a nonzero event
selector compared against this table and an error for unmatched selectors.
Published XNU code sets a release kernel's default policy to restrict events
to this known list
([policy definitions](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/arm64/cpc_arm64.h),
[secure-build control](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/cpc.h)).
The compiled gate recognizes the named events. Xcode 27.1's Instruments collector
successfully recorded all three requested M1 event groups; the validation data is
in [M1 Xcode validation](apple-m1-xcode27-validation.json). This account's direct
KPC configuration and counter
reads returned `EPERM`; root was unavailable. Apple's
[KPC sysctl implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_kpc.c)
checks ktrace access (root or the blessed PID) before those operations, while
counter-count queries are public. The unprivileged `EPERM` therefore does not
show that the M1 event selector itself was rejected. Developer mode was enabled
on this host, but it did not satisfy the direct KPC access gate. Full Xcode was
subsequently installed, and Instruments obtained programmable-event samples.
An authorized root KPC/PET collector is a second way to measure these events;
the original [mperf implementation](https://github.com/tmcgilchrist/mperf)
documents a `sudo` requirement and timer-sampled per-thread snapshots. Its
first/last deltas can miss short-lived threads, so whole-run values would still
need checks against Recount and long, stable controls. Root access was not
available for this investigation.
Apple introduced the guided CPU Bottlenecks mode in
[Xcode 26](https://developer.apple.com/documentation/xcode-release-notes/xcode-26-release-notes);
Xcode 27.1's initial four-category mode produced counter data on this M1, as
measured above. Its deeper Instruction Delivery mode also produced named
Bandwidth and Latency ratios on a random-branch control: 27.377% and
30.116% of P-core cycles, summing to 57.493%, close to the initial Delivery
share in a separate run. An attempted Instruction Processing mode produced no
`MetricTable`, and that mode was absent from the trace's M1 mode catalogue;
the recording-option identifier was inferred, so this is not an exhaustive
UI-level support test. The initial [Xcode 26 release notes](https://developer.apple.com/documentation/xcode-release-notes/xcode-26-release-notes)
also flagged missing macOS data in “Instruction Processing Bottlenecks” and
“Streaming SME Bottlenecks” modes. The commands, ratios, and trace hashes are
in [M1 Xcode validation](apple-m1-xcode27-validation.json). Apple's [Processor Trace hardware
requirements](https://developer.apple.com/documentation/xcode/analyzing-cpu-usage-with-processor-trace)
start at M4 for Mac recording, so Processor Trace cannot supply a hardware
execution trace on this M1.
Fresh Instruction Delivery captures broadened that check. All three exported
complete cycle, Delivery Bandwidth, and Delivery Latency intervals on the
intended M1 core kind:

| Control | Bandwidth | Latency | Sum | Root Delivery in an earlier run |
|---|---:|---:|---:|---:|
| E-core random branch | 19.52% | 24.33% | 43.85% | 43.85% |
| P-core large executed NOP stream | 0.01% | 73.37% | 73.38% | 72.82% |
| E-core large executed NOP stream | 0.01% | 47.94% | 47.96% | 47.79% |

The differences from the separate root captures were 0.007, 0.554, and
0.163 percentage points, respectively. The large instruction streams were
classified almost entirely as Delivery Latency in this mode, while the random
branch control split its Delivery share between Bandwidth and Latency. These
are Apple-native cycle ratios from separate recordings; they do not prove
same-interval parent/child equality or Intel slot-TMA equivalence. The
recording options, commands, checksums, XML hashes, and complete interval
counts are in [M1 delivery subcategories](apple-m1-delivery-subcategories.json).
Three other modes advertised by this M1's CPU Counters trace exported
`MetricTable` rows, but their values were integer counts with `is-ratio=0`:

| Mode and control | Sampled P-core intervals | Selected native counts |
|---|---:|---|
| Discarded sampling, random branch | 3,380 | 249,573,259 incorrectly predicted branches; 249,572,215 conditional and 1,044 other |
| Discarded indirect sampling, random branch | 3,189 | 1,026 incorrectly predicted indirect branches; 495 indirect calls; 137 returns |
| L1D miss sampling, pointer chase | 6,130 | 76,167,883 load misses; 18,141,988 store misses; 76,900,813 L1D TLB misses |

The Discarded branch count equaled conditional plus other in every sampled
interval. Indirect branch did not equal calls plus returns in four of 3,189
intervals, so those names do not establish another additive split. All rows
in these captures had complete event sets and no overlapping same-thread/core
intervals. These counts are totals over sampled intervals, not a whole-run
census or shares of the four bottleneck categories. They expose mechanisms
without filling the missing Apple Processing breakdown or Intel slot-TMA
mapping. Recording options, checksums and XML hashes are in
[M1 deeper counting modes](apple-m1-deeper-counting-modes.json).
Apple's [CPU counter subsystem overview](https://github.com/apple-oss-distributions/xnu/blob/main/doc/observability/cpu_counters.md)
identifies KPC as the manager of configurable core counters; Monotonic and
Recount expose fixed instructions/cycles. Those fixed-counter paths cannot
provide the missing pipeline events.

### Experimental M1 event collection

The local KPEP library accepted the following group without enabling any PMU
counters. Run `python3 examples/apple-m1-kpep-plan.py` on an M1 to reproduce its
counter assignments. The command fails on a different CPU family.

| Counter | Event | What its database description measures |
|---:|---|---|
| 0, 1 | `FIXED_CYCLES`, `FIXED_INSTRUCTIONS` | Fixed cycles and retired instructions |
| 2 | `SCHEDULE_UOP` | Uops issued to execution units |
| 3 | `CORE_ACTIVE_CYCLE` | Cycles while the core was active; compare with fixed cycles to establish the denominator |
| 4 | `MAP_DISPATCH_BUBBLE` | Cycles with no map input and no map stall |
| 5 | `BRANCH_MISPRED_NONSPEC` | Architecturally executed mispredicted branches |
| 6 | `MAP_STALL_DISPATCH` | Cycles with dispatch backpressure at map |
| 7 | `RETIRE_UOP` | Retired uops |
| 8 | `L1D_CACHE_MISS_LD` | Load cache misses |
| 9 | `L1I_CACHE_MISS_DEMAND` | Demand instruction-cache misses |

`RETIRE_UOP` and `INST_ALL` both require configurable counter 7 and KPEP rejected
them in one configuration. The separate fixed instruction counter avoids that
collision. [Linux's M1 PMU driver](https://github.com/torvalds/linux/blob/master/drivers/perf/apple_m1_cpu_pmu.c)
independently documents the counter restrictions. KPEP acceptance only proves a
configuration can be constructed; it does not prove that Instruments or the
kernel will count these events on this host.
Apple's [Xcode 14.3 release notes](https://developer.apple.com/documentation/xcode-release-notes/xcode-14_3-release-notes/)
record a fix for `RETIRE_UOP` failing to count on Apple silicon in an older CPU
Counters instrument. Positive native values still need verification with the
actual Xcode build.
The installed `MAP_LDST_UOP` description also mentions SME instructions, while
`hw.optional.arm.FEAT_SME` is zero on this M1. Treat the database text as a
candidate description shared across chips, not proof of every M1 event detail.

A second KPEP plan replaces branch, cache and scheduler diagnostics with map
pipeline events. Run `python3 examples/apple-m1-kpep-plan.py --group map-state` to
inspect its assignments. It places `MAP_DISPATCH_BUBBLE`, `MAP_STALL`,
`MAP_REWIND`, `MAP_STALL_DISPATCH`, `MAP_INT_UOP`, `MAP_LDST_UOP`,
`MAP_SIMD_UOP`, and `RETIRE_UOP` on all eight configurable counters, alongside
the two fixed events. `MAP_REWIND` counts cycles blocked during a flush/restart;
it does not label the cause of the flush. `MAP_STALL` is for any map stall, so
`MAP_STALL_DISPATCH` may be a subset. The three mapped-uop classes have no
published guarantee that they exhaust map throughput or correspond one-to-one
with retired uops. In [Dougall Johnson's Firestorm NOP
measurements](https://dougallj.github.io/applecpu/measurements/firestorm/NOP.html),
a stream of roughly 80,000 retired NOP uops had only 200 `MAP_INT_UOP`
counts and no reported load/store or SIMD map-class counts. Those execution-unit
classes omit eliminated NOP uops, even without meaningful bad speculation.
[Icestorm NOP measurements](https://dougallj.github.io/applecpu/measurements/icestorm/NOP.html)
show the same mismatch on E cores.
A separate experimental `m1-map` package requests this group;
each recording uses one group and retains raw vectors.

A third KPEP-accepted group, `fetch`, pairs `L1I_CACHE_MISS_DEMAND`,
`L1I_TLB_MISS_DEMAND`, `L2_TLB_MISS_INSTRUCTION`, and `FETCH_RESTART` with
`SCHEDULE_EMPTY`, `MAP_DISPATCH_BUBBLE`, `RETIRE_UOP`, and `CORE_ACTIVE_CYCLE`.
It keeps fixed cycles and instructions in the same ten-event configuration.
Run `python3 examples/apple-m1-kpep-plan.py --group fetch` to inspect its
counter assignment; `m1-fetch` requests the matching Instruments package.
This group is designed to test whether the footprint sweep below produces
instruction-cache misses while instruction-TLB misses stay low. The two empty
pipeline signals and fetch restarts are diagnostics, not a slot-width or
front-end-bound percentage; the events' scopes and relationships need hardware
calibration. KPEP accepted this group offline, and Xcode 27.1 recorded a native
capture on the M1.

An experimental `m1` mode requests this exact order through a local Instruments
package and exports the raw counter vector with the stack and core label:

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
xcrun clang -O3 -g examples/apple-m1-controls.c -o /tmp/apple-m1-controls
poetry run topdown apple probe --mode m1 --output /tmp/m1-event-probe-001
poetry run topdown apple record --mode m1 --duration 5 \
  --output /tmp/m1-event-alu-001 -- /tmp/apple-m1-controls alu_wide 4000000000
poetry run topdown apple probe --mode m1-map --output /tmp/m1-map-probe-001
poetry run topdown apple record --mode m1-map --duration 5 \
  --output /tmp/m1-map-alu-001 -- /tmp/apple-m1-controls alu_wide 4000000000
poetry run topdown apple probe --mode m1-fetch --output /tmp/m1-fetch-probe-001
```

The output directory must be new. All three M1 modes require full Xcode's `xctrace`
and `instrumentbuilder`. All three probes returned `counters_present` with 540–547
sampled rows on the tested M1 after Xcode 27.1 was installed. The parser
validates the trace's configured event order and timer trigger, retains each
uint64 vector position without assigning an event name, and never adds sampled
rows or reports TMA percentages. The requested order is empirically consistent:
the first two positions are within about 2% of independent whole-process
`stat` runs for ALU, branch, and NOP controls (the memory control differs by
about 6% in instructions), common event positions agree across three
configurations, and the
branch, memory, and instruction-footprint controls each change the expected
positions. The exported vector has no per-position name metadata, so the code
keeps `event_value_order_verified=false`. The M1 probes require at least three
positive vector positions in one sample containing the control function, beyond
what the two fixed events alone can explain. The packages are scoped to one explicit
target. The controls include wide ALU, serial dependence,
predictable and unpredictable branches, and random pointer chasing; each prints
a deterministic checksum. Use the matching `apple stat` capture and its V6
P/other split to reject runs with appreciable core migration.

An independent [Xcode counter calibration report](https://tahrioui.com/blog/no-ebpf-for-macos)
found unnamed vectors, counter resets between samples, and absolute totals that
did not match a known instruction count on another Apple chip. Its recording
used Xcode's guided CPU Bottlenecks mode, whose exported values include derived
metrics, so its vector interpretation cannot be copied to these custom raw-event
packages. These new M1 captures show that summing the first two sampled positions
recovers roughly 98% of process-accounted cycles and instructions in separate
runs of the ALU, branch, and NOP controls; memory's initialization and sampling
window produced a larger difference. Individual rows can reflect context-switch timing and should not
be treated as a continuous cumulative count. Treat other positions as empirical
event observations rather than validated slot shares.
If `record` reports a tool error but retains a trace,
inspect its logs and try `topdown apple export` into a new directory; a trace's
existence alone does not qualify the counter data.

For the instruction-cache event, compile a separate large-code control:

```sh
xcrun clang -O3 -g examples/apple-m1-icache.c -o /tmp/apple-m1-icache
xcrun clang -O3 -g -DTOPDOWN_SMALL_ICACHE examples/apple-m1-icache.c \
  -o /tmp/apple-m1-icache-hot
xcrun clang -O3 -g -DTOPDOWN_NOP_BLOCK=40960 -DTOPDOWN_NOP_GROUPS=1 \
  examples/apple-m1-icache.c -o /tmp/apple-m1-icache-160k
xcrun clang -O3 -g -DTOPDOWN_SMALL_ICACHE -DTOPDOWN_COLD_NOP_BLOCK=65536 \
  examples/apple-m1-icache.c -o /tmp/apple-m1-icache-cold
poetry run topdown apple stat --output /tmp/m1-icache-stat-001 \
  -- /tmp/apple-m1-icache 200000
poetry run topdown apple stat --output /tmp/m1-icache-hot-stat-001 \
  -- /tmp/apple-m1-icache-hot 200000
poetry run topdown apple stat --output /tmp/m1-icache-160k-stat-001 \
  -- /tmp/apple-m1-icache-160k 320000
poetry run topdown apple stat --output /tmp/m1-icache-cold-stat-001 \
  -- /tmp/apple-m1-icache-cold 200000
poetry run topdown apple record --mode m1 --duration 10 \
  --output /tmp/m1-icache-events-001 -- /tmp/apple-m1-icache 200000
poetry run topdown apple record --mode m1-fetch --duration 10 \
  --output /tmp/m1-fetch-icache-001 -- /tmp/apple-m1-icache 200000
poetry run topdown apple record --mode m1-fetch --duration 10 \
  --output /tmp/m1-fetch-icache-hot-001 -- /tmp/apple-m1-icache-hot 200000
```

The default function contains 65,536 `nop` instructions. Smaller builds repeat
a compact `nop` block to keep nominal work around 13.107 billion NOPs per
capture; nondivisor block sizes use `TOPDOWN_NOP_GROUPS=1` and a matching
repetition count recorded in the validation JSON. The 160 KiB build uses
320,000 repetitions. This host reports
196,608 bytes of P-core L1I and 131,072 bytes of E-core L1I through
`hw.perflevel*.l1icachesize`. Three fresh `stat` runs of each of 18 builds on
each core kind produced:

| NOP block | Compiled text bytes | P-core median IPC | E-core median IPC |
|---:|---:|---:|---:|
| 256 | 1,300 | 7.945 | 3.963 |
| 8,192 | 33,044 | 7.959 | 3.963 |
| 16,384 | 65,812 | 7.970 | 3.955 |
| 32,768 | 131,348 | 7.955 | 3.883 |
| 33,024 | 132,372 | 7.955 | 3.665 |
| 33,792 | 135,444 | 7.949 | 3.117 |
| 34,816 | 139,540 | 7.952 | 2.645 |
| 36,864 | 147,732 | 7.956 | 2.071 |
| 38,912 | 155,924 | 7.945 | 2.074 |
| 40,960 | 164,116 | 7.957 | 2.073 |
| 49,152 | 196,884 | 7.881 | 2.073 |
| 49,408 | 197,908 | 7.218 | 2.073 |
| 50,176 | 200,980 | 5.690 | 2.073 |
| 51,200 | 205,076 | 4.478 | 2.074 |
| 53,248 | 213,268 | 3.190 | 2.074 |
| 57,344 | 229,652 | 2.140 | 2.073 |
| 61,440 | 246,036 | 2.164 | 2.073 |
| 65,536 | 262,420 | 2.159 | 2.074 |

The 128 KiB NOP-block row uses two inner-loop passes. An additional control
used one pass and 400,000 repetitions, matching the just-over-128 KiB builds'
loop structure and nominal NOP work. Its three-run medians were 7.954 IPC on P
cores and 3.888 on E cores, close to the original 128 KiB row; the next 129 KiB
block still fell to 3.665 E-core IPC. Its six raw counts and binary hash are in
the validation JSON.
Repeating both boundary pairs three times in oversized-first order reproduced
the separation: E-core medians were 3.664 at 129 KiB versus 3.882 at 128 KiB;
P-core medians were 7.218 at 193 KiB versus 7.890 at 192 KiB. All twelve
additional runs used the same binaries and expected checksums; their counts
and execution order are also in the validation JSON.
An unexecuted 256 KiB NOP function enlarged the text section to 263,448 bytes
while the executed loop stayed at 256 NOPs. Three runs of this binary measured
7.950 P-core and 3.964 E-core median IPC, close to the compact hot-loop
baseline despite total text exceeding both L1I sizes. This separates the
*executed* instruction footprint from binary size; the six raw counts and
binary hash are in the validation JSON. The earlier sweep's source hash refers
to its recorded commit, before this optional cold-function control was added.

The E-core throughput starts falling between 128.3 KiB and 129.3 KiB of text,
then reaches about 2.07 IPC by 144.3 KiB. P-core throughput starts falling
between 192.3 KiB and 193.3 KiB, then reaches about 2.15 IPC by 224.3 KiB.
The three-run IPC ranges on each side of both initial steps do not overlap.
The boundaries are consistent with the reported L1I capacities, and the
opposite core kind stays near its prior throughput at each initial step.
The NOP block itself is exactly
128 or 192 KiB at the respective near-limit points; the text section also
contains loop and other program code. The host uses 16 KiB pages, so even the
largest text section touches at most 18 pages regardless of alignment. An
independent [M1 ITLB measurement](https://jia.je/hardware/2024/12/26/apple-m1/)
places the P/E L1 instruction TLB capacities at 192/128 pages. A simple ITLB
capacity limit therefore does not explain the observed drops, although this
experiment does not exclude translation conflicts or other fetch effects.
All 108 sweep runs completed with the expected checksum,
and at least 99.4% of cycles were accounted to the intended core kind. Smaller
blocks execute more loop branches, and process startup, frequency and thermal
state also affect the fixed counts. Xcode 27.1 subsequently provided that
programmable-counter capture: the requested L1I-demand-miss vector position in
`m1-fetch` held 772,845 in the large executed stream versus 6,586 in the
compact stream and 6,563 with
large unexecuted text. Normalized by sampled instructions, that is about 60,115
versus 127 and 126 misses per billion instructions. Instruction-TLB counts also
rose, but their L2 count exceeded the L1 count in the large run; these event
names must not be interpreted as a strict nested miss hierarchy without more
calibration. On E cores, the same requested L1I position rose from 11,118 in
the compact stream to 175,274,637 in the large executed stream; normalized by
sampled instructions, that is about 815 versus 15.5 million per billion.
All five E-core custom-event captures completed with more than 99.9% of sampled
cycles on E cores. [M1 Xcode validation](apple-m1-xcode27-validation.json) records the
raw vectors. [M1 validation](apple-m1-validation.json)
summarizes the points and binary hashes; [raw sweep
counts](apple-m1-icache-sweep.csv) retain every run. The validation JSON also
retains an earlier two-size comparison.
The compact throughputs are consistent with the independently measured eight-
and four-uop P/E limits; fixed *instruction* counts and process overhead do not
prove a slot denominator or four-way partition. The `nop` stream is a fetch
footprint control.
[Firestorm measurements](https://dougallj.github.io/applecpu/firestorm.html)
find that `nop` retires without issuing to an execution unit, so this workload
must not calibrate useful work from scheduler issues or retired instructions.

### Same-trace M1 formula calibration

A separate [calibration package](../examples/apple-m1-tma-calibration.instrpkg)
records `RETIRE_UOP`, `CORE_ACTIVE_CYCLE`, `MAP_STALL`, `MAP_REWIND`, and the
three mapped-uop classes alongside Xcode's guided CPU Bottlenecks instrument
in one trace. Xcode exported both its named `MetricTable` ratios and the
custom PMU vector. The vector has no event labels and reordered the requested
events; its position labels below are inferred from ALU, branch, memory, and
NOP controls. They are not certified by Xcode. The same package under a Blank
template produced a ten-position vector whose workload signatures aligned
with the requested order. Adding the guided CPU Counters template produced a
twelve-position vector with a different effective order, even though both
traces listed identical `pmc-events` metadata. This is why the profiler
retains raw positions without assigning unverified event names. Run
`python3 examples/apple-m1-kpep-plan.py --group calibration` to check that
the eight programmable events fit distinct hardware counters; that KPEP
assignment is not the combined trace's raw vector order.
Use `python3 examples/apple-m1-kpep-plan.py --group calibration --swap RETIRE_UOP MAP_DISPATCH_BUBBLE`
to check one proposed pairwise event-order change without enabling counters.

An [M1 event-order experiment](apple-m1-event-order-experiment.json) tested the
TOC metadata directly. Swapping requested `RETIRE_UOP` and
`MAP_DISPATCH_BUBBLE` exchanged the normalized signatures at vector positions
2 and 3 in a standalone custom capture (611/300 million to 301/611 million
per billion fixed cycles). With the guided CPU Counters template added, the
TOC requested order changed, but the positions corresponding to those
signatures stayed at 7 and 9 (611/301 million versus 611/300 million).
The unchanged position equations still matched 2,859 guided intervals to
export rounding, covering 84.24% of guided sampled cycles. One capture per
configuration cannot certify every selector, but it directly shows that the
combined trace's `pmc-events` list does not label the exported vector order.
A second feasible guided permutation swapped requested `CORE_ACTIVE_CYCLE` and
`MAP_INT_UOP`; the corresponding normalized vector positions 2 and 5 again
stayed near 998 million and 2.11 billion per billion fixed cycles, and 2,882
matched intervals reproduced the guided ratios. Reversing all eight requested
programmable events failed KPEP planning and Xcode capture with a conflicting
`RETIRE_UOP` assignment. Event-order experiments therefore need a feasibility
check before recording; the artifact preserves this failed case too.

Let `A` be active cycles, `W` an assumed peak width of eight P-core or four
E-core uops per cycle, and `M` the sum of `MAP_INT_UOP`, `MAP_LDST_UOP`, and
`MAP_SIMD_UOP`. The following candidate equations reconstructed Xcode 27.1's
four Apple categories on this host:

| Apple category | Empirical candidate |
|---|---|
| Useful | `RETIRE_UOP / (W × A)` |
| Processing | `MAP_STALL / A` |
| Discarded | `max(0, M - RETIRE_UOP + W × MAP_REWIND) / (W × A)` |
| Delivery | `max(MAP_DISPATCH_BUBBLE / A, 1 - Useful - Processing - Discarded)` |

Some `MetricTable` intervals end at a raw PMU sample time. Matching by end
time, thread, core, and active-cycle count joined 40,234
intervals across sixteen same-trace controls. All four equations reproduced
every matched Xcode ratio to its nine-decimal fractional export precision;
the largest absolute difference was below 0.0000001 percentage points.
Those matches covered 89.0–89.8% of valid guided sampled cycles in nine
P-core controls and 49.1–50.6% in seven E-core controls. Across all sixteen,
27,783 valid guided intervals had no raw counter vector at the exact interval
end time, and 17,506 had a vector for the same time, thread, and core but a
different active-cycle count. None failed only because of a thread/core
disagreement at the same time. The [join-coverage diagnostics](apple-m1-tma-match-coverage.json)
retain per-control counts, sampled cycles, and verified source XML hashes.
These categories describe what the two Instruments exports can join; they do
not establish why samples differ or how many hardware events were lost. The
exact-join result validates individual-interval equations only where both
exports match, especially on E cores.
Consecutive raw vectors give a second, wider check. For each thread and core,
the validator groups guided intervals whose end times fall between two raw
sample times, requires their starts to stay inside that window, and compares
their summed cycles with the raw active-cycle count. Across the same sixteen
controls, 53,059 windows met that cycle check, including 17,165 with multiple
guided intervals. They covered 95.3–97.8% of guided sampled cycles in the nine
P-core controls and 86.1–92.3% in the seven E-core controls. Applying the
candidate equations to each raw vector reproduced the cycle-weighted guided
categories to export rounding in 53,057 of those windows. The two exceptions
were E-core windows whose individual guided intervals had positive Discarded
shares but whose summed raw events fell below the nonnegative clamp; their
weighted guided Discarded shares were 0.71% and 3.97%. Another 4,844 windows
had guided-cycle totals different from raw active cycles and are excluded.
This explains much of the low exact-join coverage as split guided intervals,
while retaining the evidence that aggregate raw events cannot always recover
the per-interval classification. Counts, errors, exceptions, and XML hashes are
in [the window validation](apple-m1-tma-window-validation.json).
A paired E-core ALU control then requested 500 µs instead of 1,000 µs in the
custom instrument package. Xcode warned that the requested 500,000 ns differed
from its configured 1,000,000 ns sampling period. Both captures still had a
1,000,083 ns median gap between raw vectors on the same thread and core.
The exact-match guided-cycle coverage was 45.77% at the 500 µs request and
46.35% at 1,000 µs; all matched equations remained within export rounding.
One capture per setting supports no broader rate claim, but changing this
custom package setting did not recover the missing E-core joins in this test.
The [sampling-rate control](apple-m1-sampling-rate-control.json) includes the
source and XML hashes, effective gap distribution, Xcode warning and validator
output.
Turning on Xcode's `useHighFrequencyForGuidedMode` option in a second short
E-core ALU pair requested a 100,000 ns period and produced the same binding
warning against a configured 1,000,000 ns period. Both option settings had
a 1,000,042 ns median raw-vector gap; exact-join coverage was 47.70% with the
option and 47.32% without it. This pair also did not recover the missing joins.
The recording-options JSON hashes, output checksums, XML hashes and validator
results are in the same control artifact.
The controls included P/E ALU, branch, pointer chase, SIMD, compact and large
executed NOP streams, and random branches mixed with 8, 16, or 64 NOPs per
iteration. Another 17,757 active raw samples had no complete matching guided
interval and were excluded from this comparison. All targets exited
successfully, and 99.9% or more of each run's guided cycles were on the
intended core kind. The P/E SIMD controls counted 7.9 and 3.8 billion in the
inferred `MAP_SIMD_UOP` position. The requested order, inferred position
sums, XML hashes, matched counts, and errors are in
[M1 TMA calibration](apple-m1-tma-calibration.json).
In a separate [mixed-core control](../examples/apple-m1-mixed-core.c), one
process ran a foreground ALU thread and a background random-branch thread.
Its guided cycles split 80.7% P and 19.3% E. Applying the width for each
matched interval's actual core reproduced all four shares for another 2,721
intervals to export precision. This supports keeping P/E denominators
separate within one process; the overall CLI summary remains cycle-weighted.
The window check covered 95.19% of this mixed process's guided sampled cycles
in 3,362 cycle-aligned raw windows, including 819 with multiple guided
intervals; every window matched export rounding with its own core width. Exact
single-interval joins covered 81.67%. The mixed result and XML hashes are in
[the window validation](apple-m1-tma-window-validation.json).
Both worker threads had guided samples on both P and E cores, with twelve
observed transitions between core kinds. Split by core
kind, exact joins covered 89.18% of P-core guided cycles and 50.31% of E-core
guided cycles. Cycle-aligned windows raised that coverage to 96.48% and 89.77%;
all 2,075 P windows and 1,287 E windows matched export rounding. The validator
now reports these per-kind counts so migration cannot mask weaker E-core join
coverage in the combined number.
As a negative control on the same exact joins, using an eight-wide denominator
on E cores changed Useful by a median 7.13 percentage points; using a four-wide
denominator on P cores changed it by a median 83.33 points. This tests the
candidate formula's width choice against Xcode's exported ratios, but does not
prove a physical slot width or Intel TMA equivalence.

A new [dependent integer-division control](../examples/apple-m1-divide.c)
adds an instruction class absent from the sixteen controls. The optimized
binary contains `udiv`, and separate intended P/E captures returned the same
checksum. Xcode classified 90.54% of sampled P-core cycles and 89.82% of
sampled E-core cycles as Processing, compared with 16.67% and 0.013% in the
earlier wide-ALU controls. The candidate equations reproduced every category
to export rounding on 699 P-core and 1,612 E-core exact joins, covering
83.79% and 48.08% of guided sampled cycles. [The divide evidence](apple-m1-divide-control.json)
contains the source/binary and XML hashes, core fractions, native shares and
join diagnostics. This expands the empirical workload range but leaves the
event labels and Intel slot classification unproved.
The window check also reproduced the division control's cycle-weighted guided
ratios in all 641 P-core and 2,046 E-core cycle-aligned windows, including 56
and 975 windows with multiple guided intervals. Those aligned windows covered
84.49% and 75.00% of its guided sampled cycles, respectively; the other
windows remain outside that comparison. The results and source XML hashes are
included in [the window validation](apple-m1-tma-window-validation.json).
On the division control's exact joins, swapping the width denominator changed
Useful by a median 9.39 percentage points on the intended P capture and 5.04
points on the intended E capture. This independently checks the width choice
for another instruction class; the [division artifact](apple-m1-divide-control.json)
records its match counts and XML hashes.

The nonnegative clamp matters: a plain `M - RETIRE_UOP` term produces a
strongly negative Discarded value for eliminated NOPs. In the 16-NOP mixed
branch control, `M - RETIRE_UOP` was also negative, but adding the rewind
term gave 13.5448% Discarded, matching Xcode's 13.5447%. With 64 NOPs, the
combined term was negative and Xcode reported almost no Discarded share.
Delivery's maximum also matters: on many ALU and SIMD intervals, the full
map-bubble share slightly exceeds the residual, so Xcode's four reported
ratios sum slightly above one. A residual-only Delivery estimate does not
reproduce those intervals. This is strong empirical agreement for these
Apple categories, not proof of
Xcode's private formulas, the raw vector labels, or Intel slot-TMA
equivalence. The CLI therefore uses Xcode's named guided metrics and leaves
the Intel `tma` fields unset.

Build the package and record a control with:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcrun clang -O3 -g examples/apple-m1-controls.c \
  -o /tmp/apple-m1-controls-xcode271
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcrun instrumentbuilder -i examples/apple-m1-tma-calibration.instrpkg \
  -o /tmp/m1-calibration.instrdst -l 'CPU Counters'
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcrun xctrace record --template 'CPU Counters' \
  --package /tmp/m1-calibration.instrdst \
  --instrument 'Topdown M1 Calibration' --time-limit 20s \
  --output /tmp/m1-calibration-example.trace --launch -- \
  /tmp/apple-m1-controls-xcode271 branch_random 700000000
```

The mixed branch/NOP control is in
[apple-m1-branch-nop.c](../examples/apple-m1-branch-nop.c); compile with
`-DNOP_COUNT=8`, `16`, or `64` to repeat the threshold test.
The SIMD control is in [apple-m1-simd.c](../examples/apple-m1-simd.c).
Export the trace's `counters-profile` and `MetricTable` tables with
`xcrun xctrace export --xpath`, then pass both XML files and the launched PID
to [apple-m1-validate-tma.py](../examples/apple-m1-validate-tma.py). The
validator joins interval end times to PMU samples and fails if any matched
category differs beyond Xcode's export rounding. It rejects duplicate raw
match keys and reports unmatched raw samples, unmatched guided intervals, and
the fraction of guided sampled cycles represented by matched intervals, plus
whether unmatched guided intervals lacked an exact-time vector or disagreed
on active-cycle count. A successful formula check applies only to that
matched fraction.
In a fresh M1 branch control on 2026-10-23, 2,875 of 4,855 guided intervals
matched 3,086 active raw samples and represented 84.55% of guided sampled
cycles. The equations matched Xcode's export rounding on those joined
intervals; 1,980 guided intervals remained outside the formula check.

### What Intel slot-TMA still needs

Intel's [Top-Down Microarchitecture Analysis Method](https://www.intel.com/content/www/us/en/docs/vtune-profiler/cookbook/2024-1/top-down-microarchitecture-analysis-method.html)
classifies pipeline slots as Retiring, Frontend Bound, Backend Bound, or Bad
Speculation. The M1 events above provide useful signals but no established
disjoint four-way slot classification. For example, `BRANCH_MISPRED_NONSPEC`
counts branches, not the slots lost during their recovery. `MAP_DISPATCH_BUBBLE`
counts fully empty map cycles, not all empty delivery slots. `SCHEDULE_UOP`
counts execution-unit issues, not all allocated uops:
[Firestorm measurements](https://dougallj.github.io/applecpu/firestorm.html)
show instructions whose issue and retired-uop counts differ. Subtracting
`RETIRE_UOP` from `SCHEDULE_UOP` therefore does not measure bad speculation.
The Firestorm NOP throughput control counted 100 scheduled uops beside 80,204
retired uops; the Icestorm control likewise counted about 100 beside 80,204
([P-core](https://dougallj.github.io/applecpu/measurements/firestorm/NOP.html),
[E-core](https://dougallj.github.io/applecpu/measurements/icestorm/NOP.html)).
Our compact NOP captures reinforce this: the requested `SCHEDULE_EMPTY`
position was about 84% of sampled cycles on P cores and 94% on E cores, while
Xcode's separate guided captures reported about 99.8% Useful on both. A high
scheduler-empty count is therefore not an instruction-delivery bottleneck by
itself when the processor retires eliminated NOPs.
For the compact NOP controls, `RETIRE_UOP / (8 × CORE_ACTIVE_CYCLE)` on P cores
was 99.83%, versus 99.82% guided Useful; the corresponding four-wide E-core
estimate was 99.85%, versus 99.84% guided Useful. These are separate captures,
so the close agreement supports an interpretation of Useful as normalized
retired-uop throughput but does not certify Xcode's formula or the other
categories. The five P/E footprint comparisons are retained in the Xcode
validation JSON.
The same calculation on six wide-ALU, random-branch, and pointer-chase controls
on P and E cores differed from guided Useful by at most 0.066 percentage
points. Within each custom-event capture, `CORE_ACTIVE_CYCLE / FIXED_CYCLES`
was 0.996–0.999 on P cores and 0.987–0.988 on E cores. This checks the
candidate denominator on active workloads, but the guided and custom-event
runs were separate, the event vectors lack per-position labels, and the peak
widths are assumed. Exact identity and cross-core aggregation remain unproved.
The same research finds an eight-uop P-core pipeline limit and [up to four
retired uops per E-core cycle](https://dougallj.github.io/applecpu/icestorm.html).
These black-box throughput limits do not establish which pipeline stage defines
the slot denominator for mixed P/E execution or prove the observable fixed-cycle
event matches the relevant active pipeline cycles.

Arm's [Neoverse V1 top-down methodology](https://documentation-service.arm.com/static/668cffe969e89f01e39c7ba7)
illustrates the missing information: its four-way formulas require a documented
slot width, speculative and retired operation counts, total/front-end/back-end
stalled slots, and a flush-recovery correction. The installed M1 database has no
events named `OP_SPEC` or `STALL_SLOT_*`; `RETIRE_UOP` is only a candidate for
Arm's retired-operation count. M1's cycle-level map signals cannot be inserted
into those slot formulas without proving their widths, overlap and recovery
semantics. This comparison does not rule out a different M1-specific formula.
The M1 database also lacks `MAP_UOP` and `MAP_DISPATCH_BUBBLE_SLOT`, though
both names occur in other event catalogues shipped on this macOS installation.
It also lacks `MAP_DISPATCH_BUBBLE_IC`, `MAP_DISPATCH_BUBBLE_ITLB`, and
`ARM_STALL_FRONTEND`, which appear in newer [Apple event
catalogues](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/arm64/cpc_arm64_events.c).
The three M1 mapped-uop class counters and its *cycle*-level
`MAP_DISPATCH_BUBBLE` need separate validation; those slot-level event names
cannot be assumed to describe M1 hardware.
An undocumented `dispatch uop` selector `0x78` appears in independent
[Firestorm measurements](https://dougallj.github.io/applecpu/measurements/firestorm/NOP.html),
but it is absent from the installed M1 event database and Apple's
[T8103 release-kernel event list](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/arm64/cpc_arm64_events.c).
The release kernel's known-event policy rules it out as a stock macOS fallback.
In both the [Firestorm](https://dougallj.github.io/applecpu/measurements/firestorm/NOP.html)
and [Icestorm](https://dougallj.github.io/applecpu/measurements/icestorm/NOP.html)
NOP throughput measurements, selector `0x78` counted only 100 dispatch uops
beside 80,204 retired uops. Even if raw access were available,
this selector would not supply an all-uop speculative count for eliminated
instructions.

| TMA category | M1 signal to investigate | Missing measurement or proof |
|---|---|---|
| Retiring | `RETIRE_UOP` | Per-core-kind slot denominator, counter scope and retire width under each workload |
| Frontend Bound | `MAP_DISPATCH_BUBBLE`, instruction-cache/TLB misses, `SCHEDULE_EMPTY` | Partially empty delivery slots; cycle events and misses cannot identify every empty slot or its cause |
| Bad Speculation | `BRANCH_MISPRED_NONSPEC`, `FLUSH_RESTART_OTHER_NONSPEC`, `MAP_REWIND` | Wasted speculative uops plus recovery slots; these count flushes or rewind cycles, not all lost slots |
| Backend Bound | `MAP_STALL_DISPATCH`, `MAP_STALL`, cache/TLB misses | Partial backend stall slots and non-overlap with rewind/front-end stalls |

The same-trace calibration group pairs `CORE_ACTIVE_CYCLE` with
`FIXED_CYCLES`. Their sampled ratio ranged from 0.9934 to 0.9981 on the P
controls and from 0.9854 to 0.9885 on the E controls. This supports active
cycles as the candidate denominator, but the event's database description
does not establish its precise thread scope or equivalence to Intel slots.
An independent [M1/Asahi Linux counter experiment](https://blog.cyyself.name/tso-apple-m1/)
observed `CORE_ACTIVE_CYCLE`, `MAP_STALL`, `MAP_STALL_DISPATCH`, and
`MAP_REWIND` change with memory-ordering behavior. In its two workloads,
`MAP_STALL_DISPATCH` was close to `MAP_STALL`; that is a useful directional
check for the map-state group, not a universal equality or a macOS calibration.
Its reported active cycles per instruction are consistent with reciprocal IPC
from `perf` cycles/instructions within the rounding of the reader and writer
cases. This makes `CORE_ACTIVE_CYCLE` a credible M1 denominator candidate, but
the same-run macOS comparison above does not establish its precise scope or
equivalence to Intel slots.

The map-state controls found mapped-uop sums close to retired uops for wide
ALU, above retired uops for random branches, and far below retired uops for
eliminated NOPs. This rules out treating the mapped sum as a general all-uop
count or subtracting it directly from `RETIRE_UOP` to measure discarded work.
`MAP_STALL` and `MAP_STALL_DISPATCH` were close on the P-core ALU and memory
controls, but their overlap with rewind cycles and partially filled map
cycles is not established. A cycle count for "no uops" cannot reveal how many
other cycles delivered fewer than the pipeline's possible uops. An accepted
KPEP event plan alone does not establish these identities on hardware.

Use the three packages as paired experiments on stable workloads. Each capture
has its own event group, so compare normalized responses across repeated runs
rather than subtracting one group's raw sample from another's. The immediate
checks are:

| Control comparison | Event response to check | Interpretation if absent |
|---|---|---|
| Random versus predictable branch | Mispredicted branches in `m1`; `MAP_REWIND` in `m1-map` | Recheck the executed branch, vector positions, event support and sample scope |
| Pointer chase versus wide ALU | Load-cache misses in `m1`; map backpressure in `m1-map` | Recheck cache footprint and distinguish memory latency from unrelated stalls |
| Large versus compact `nop` stream | L1I misses versus instruction-TLB misses in `m1-fetch`, plus map bubbles and scheduler-empty cycles | Recheck the instruction footprint, translation behavior, vector mapping and sample scope |
| Wide ALU on P versus E cores | Retired/mapped uops, fixed cycles and Recount core-kind split | Keep separate denominators; reject captures with substantial migration |

On the same map-state samples, check whether `MAP_STALL_DISPATCH` is bounded by
`MAP_STALL`, whether `MAP_REWIND` overlaps those cycles, and whether the three
mapped-uop classes account for the observed ALU work. A directional response
is evidence for an event's sensitivity, not a slot-classification formula.

Before enabling `tma_available`, verify the inferred vector labels and
sampling semantics independently, establish counter scope, loss and
multiplexing behavior, and test the candidate equations across more
instruction classes and migrations. In particular, show that Apple's
discarded-uop and recovery treatment corresponds to Intel's Bad Speculation
slots, and that the other three categories use compatible slot boundaries.
Apple's [CPU Bottlenecks
categories](https://developer.apple.com/documentation/xcode/addressing-cpu-bottlenecks)
partition maximum sustainable CPU bandwidth into Useful, Instruction Delivery,
Instruction Processing and Discarded; their correspondence to Intel slot TMA
must be tested rather than assumed. Until then, native event names and units
remain separate from the Linux TMA database and funnel.
Apple's [CPU Optimization Guide](https://developer.apple.com/documentation/apple-silicon/cpu-optimization-guide)
advertises event and metric lists but requires a registered developer account
and a separate agreement for the full guide. Public Xcode documentation does
not provide a validated M1 formula for this four-way partition.

## Fixed-counter samples and stacks

`fixed` mode requests only `FIXED_CYCLES` and `FIXED_INSTRUCTIONS`, with a timer
trigger. It avoids the programmable-event configuration rejected on this M6:

```sh
poetry run topdown apple probe --mode fixed --output /tmp/m6-fixed-check-001

xcrun clang -O3 -g examples/apple-probe.c -o /tmp/topdown-apple-probe
poetry run topdown apple record --mode fixed --duration 4 \
  --output /tmp/apple-fixed-001 -- /tmp/topdown-apple-probe

poetry run topdown apple export /path/to/fixed-recording.trace \
  --mode fixed --output /tmp/apple-fixed-export-001
```

This mode requires Xcode's `instrumentbuilder` and `xctrace`. The bundled package
is compiled into the new capture directory and supplied through `--package`;
nothing is installed into Instruments or the OS. The capture keeps the package
source, its hash, build commands/logs, raw trace, XML, and `profile.json`.

Each exported sample contains `event_values`, stack frames, PID/TID, timestamp and
the native core label. Values are read from the native `counters-profile` vector;
the event order and timer trigger must match the trace's metadata. A timer stack
alone cannot satisfy this mode. Rows with absent counter vectors are counted as
`unmeasured_rows`, rather than filled with zero. Other PIDs and rows outside the
requested interval are excluded. Native values remain integers and are neither
summed nor treated as calibrated instruction costs, IPC or slot-TMA shares.

The final CLI probe completed with its expected checksum and 370 scoped
fixed-counter samples, 367 containing `alu_work`. A separate reconstruction from
the raw XML matched every exported counter row. These establish counter observations and stack
association; sampling loss, per-core accounting and exact cost attribution remain
unvalidated. See [CLI validation](apple-m6-cli-validation.json) for the controls,
root experiments, hashes and test results.

## Hardware execution trace through CLI

The tested M6 also supports the **Processor Trace** template without root or
programmable-event configuration. Use `trace` mode for decoded execution
intervals and their native instruction/cycle counts:

```sh
xcrun clang -O2 -g -c examples/apple-trace-probe.c -o /tmp/apple-trace-probe.o
xcrun clang -g /tmp/apple-trace-probe.o -o /tmp/apple-trace-probe
xcrun dsymutil /tmp/apple-trace-probe -o /tmp/apple-trace-probe.dSYM

poetry run topdown apple record --mode trace --duration 1 \
  --output /tmp/apple-trace-001 -- /tmp/apple-trace-probe 100000

poetry run topdown apple export /path/to/recording.trace \
  --mode trace --output /tmp/apple-trace-export-001
```

The example has a five-second alarm, a bounded recurrence and a known checksum.
The default 100,000 steps produce checksum `2700328192655336907`. Unlike the large
fixed-counter probe, this example is intended for short hardware traces. Trace
recordings can still be large: an initial subsecond recording used about 140 MB.
The unattended `apple probe` command does not support `trace`; use
`record --mode trace` with an explicitly bounded target.

`profile.json` contains `intervals` and `interval_count`, rather than samples.
Each interval retains its native label, binary offset, call depth and frames,
start/duration, `self_instructions`, `self_cycles`, and `self_duration_ns`.
Unreported self duration stays null. The exporter also requires and retains the
native gap table as `gaps.xml`, with its hash and selected PID/thread markers.
`coverage_complete` remains false, even for a window without gap markers.

The time window includes only intervals wholly inside the selected range.
`boundary_rows_excluded` counts overlapping intervals that cannot be subdivided
without inventing instruction/cycle attribution. Call intervals can overlap at
different depths; summing their durations would double-count elapsed time.
Gap markers are points rather than measured missing durations, so their count
cannot establish a coverage percentage or guarantee complete decoding.

Some native labels identify an address inside a function. Frames retain the
binary UUID and load address for offline symbol lookup. With a matching dSYM,
`xcrun atos -o /path/to/binary.dSYM/Contents/Resources/DWARF/binary -l LOAD_ADDRESS ADDRESS`
resolved the tested loop to its function and source line. Retain the object file
until generating the dSYM. An explicit `xctrace symbolicate` attempt exceeded the
experimental timeout; capture/export does not run it automatically.

On this Xcode build, disassembly of a 100,000-step serial loop predicts 500,004
instructions, which the trace reproduced in multiple captures. Longer 200,000-step
controls differed from their predicted count by 1 and 12 instructions despite
no in-loop gap markers. These observations support retaining decoded costs for
investigation, with their accuracy limits. They do not establish calibrated
function costs or four-category TMA. Apple's
[Processor Trace explanation](https://developer.apple.com/videos/play/wwdc2025/308/?time=923)
describes its hardware tracing and executable-based reconstruction.

The follow-up also exercised `thread_selfcounts` and `PROC_PIDTHREADCOUNTS`:
native counters are available separately for Super, Performance and Efficiency
cores. A separate C reader matched the Python reader's bytes for a stopped owned
target. Thread lifetime counts differed from process-accounted lifetime totals;
these sources remain separate and the per-thread interface is not exposed as a
new product command. A follow-up self-exec control found exact agreement of thread
and process instruction deltas within ten individual phases, while their cumulative
offset changed across exec. The offset's precise cause remains unresolved; phase
agreement does not establish lifetime equivalence. Serial/parallel arithmetic
controls produced different native instruction/cycle ratios, which cannot identify
a unique stall cause.
See [follow-up validation](apple-m6-trace-validation.json) for the measured controls
and limitations.

## System and cluster firmware context

`apple soc` reads private native IOReport channels without root, Xcode, or new runtime
dependencies. It does not launch or attach to a workload. Run the workload separately
and use a new directory:

```sh
poetry run topdown apple soc --duration 2 --output /tmp/apple-soc-001
```

On the tested M6, the command exports 27 channels: six PMP stall distributions
(`Core Block Slot`, `M$ Stall`, `DRAM Stall` for EACC0/PACC0), nine DCS bandwidth
histograms, and twelve core performance-state distributions. It retains native
channel/state IDs, labels, units, exact unsigned counter deltas, transition deltas,
read timestamps, before/after snapshots, collector hash, and worker logs. The
private native reader runs in a subprocess with a duration-plus-30-second timeout;
missing channels, changing layouts, counter resets and invalid values fail the run.
Other machines may expose different channels or reject subscriptions.

The PMP channels use native **events**, including those read through a function
named `IOReportStateGetResidency`. These values are observation counts, not
nanoseconds. The CPU performance-state channels use native `24Mticks`.
`share_of_reported_values` divides each state's delta by that channel's total;
zero totals produce null shares. The percentage and GB/s strings are native bucket
labels. Their boundaries, sample cadence and activity denominator remain unknown.
No exact bandwidth, clock frequency, process stall percentage, or TMA share is
computed. Results include unrelated activity across the system/cluster.

Controlled, randomized workload experiments found a reproducible PACC0 `DRAM Stall`
response to large dependent pointer-chasing. One-worker 8 KiB–4 MiB controls remained
near the first bucket, while 64–256 MiB controls moved strongly toward high buckets.
Disjoint 128 MiB chains retained the high response with one, two and six workers;
shared chains behaved differently, exposing cache-sharing as a confound. This
supports a coarse contextual memory signal under these controls. It does not
calibrate the labels as a workload's percentage of stalled cycles. `Core Block
Slot` remained near its first bucket in the arithmetic and memory controls; its
responsiveness and meaning are unvalidated. No automatic bottleneck classifier is
provided. A separate static branch-ring control expanded its code footprint from
256 bytes to 4 MiB and slowed by about 37 times, while `Core Block Slot` stayed in
the first bucket. This channel therefore did not identify that control's slowdown;
the experiment does not isolate instruction-cache misses from branch-target effects.
See [deep CLI validation](apple-m6-deep-validation.json).

## Trace scheduling context

When a Processor Trace recording includes a `thread-state` table, export also
retains `thread-state.xml` and adds `scheduling` to each decoded interval. Native
Running/Blocked/other state spans are intersected with that interval and thread.
The annotation reports covered and uncovered nanoseconds, native state durations,
and observed Running cores. A single core is assigned only when the complete
positive-duration interval was observed Running on that core. Missing spans,
blocking, migration and unknown core identity prevent that assignment.

The measured serial-loop fixtures and native exports resolve to both S-core and
E-core execution in separate recordings. Counts remain the original native
instruction/cycle values: they are never prorated across scheduling states or
cores. Call intervals contain inclusive time and can overlap. Native thread-state
model boundaries can be synthetic, so no process-wide waiting percentage is
computed. A two-sleep control measured 110.039 ms and 103.388 ms in the target;
corresponding native Blocked spans were 110.003 ms and 103.351 ms. Other Blocked
spans also occurred, so they are not all classified as those sleeps. Complete
scheduling coverage does not establish complete instruction trace decoding.
Traces without this table remain exportable and report
`scheduling_available: false`.

## Remaining OS counter barriers

The uncore interface accepts ownership and reports two monitors with 24 counters,
but an exhaustive disabled sweep of all 65,536 16-bit selectors accepted only zero;
all 65,535 nonzero selectors returned `EPERM`. The probe never enabled counters and
closed/reset its ownership. Together with the earlier programmable-core rejection
and running-kernel inspection, this closes another direct acquisition route on
this installed kernel. No host security setting, kernel, or boot configuration was
changed.

IOReport enumerates 207 AMC memory-controller channels, but subscriptions to each
individual channel failed without root. Enumeration alone does not establish
readable counters. A root comparison still requires interactive CLI authentication
and has not been completed. A development-kernel collection was subsequently built
and inspected as described below; boot and runtime event access remain untested.
The existing kernel gate explains a blocker; it does not establish that
patching one branch would provide valid event mappings, safe counter programming,
or a complete top-down model.

A support recheck on October 21 found no remedy in the retrieved
[Apple engineer's forum response](https://developer.apple.com/forums/thread/849179).
A separate participant reports the failure on macOS 27.2 Beta; this is a user
report, not a local test or Apple confirmation. Apple has released
[Xcode 27.1](https://developer.apple.com/news/releases/?id=10152026a), but the
retrieved 27.1/27.2 release notes supplied no M6/KPC fix. A local recheck on
October 22 with Xcode 27.1 (27A9275), on the unchanged macOS 27.0.1 kernel,
still produced no CPU Counters bottleneck table. CPU Profiler failed with KPC
`EPERM`; its recording window contains a kernel log rejecting event `0x0011`,
the installed database's `CORE_ACTIVE_CYCLE`. Fixed-counter profiling completed
successfully. The UUID-matched release kernel rejects all nonzero programmable
selectors, while the M1 kernel shipped with the same OS has an event lookup
table. Newer macOS builds have not been tested locally.

## Matching M6 development kernel

The follow-up obtained the exact macOS 27.0.1 / 26A434 KDK through a mirror and
verified its enclosing installer as signed Apple Software. Its T8152 release kernel
is byte-for-byte identical to the installed kernel. The package also contains
`kernel.development.t8152`, UUID `DB731926-AEDA-363D-A653-49575E91EE34`.

Offline disassembly and a separate Mach-O byte decoder confirm that this development
binary exposes `kern.cpc.secure` with writable flags (`0xc0600002`). The file
initializes its event policy to enum 1, and its handler can switch between enums
1 and 2. The counter configuration path permits nonzero selectors under enum 1.
This is a concrete development-kernel candidate for removing the observed policy
barrier, without modifying the supplied binary. It has not been booted or shown to
produce programmable counts.

The current KDK README explicitly says installing its kernel variants on Apple
silicon is unsupported. The older HTML resource contains obsolete Intel/High Sierra
instructions; those are not a procedure for this M6. Native `kmutil` collection
build attempts using the extracted KDK, explicit SDK paths and an isolated volume
root required a matching installed KDK/SDK. After the user installed the signed KDK
from their terminal, the local collection build succeeded. A separate Mach-O parser
verified the embedded development-kernel UUID, and all 308 manifest-required driver
identities are present. The resulting 127 MiB collection contains the same 387
driver identities and versions as the stock collection. No kernel binary was
patched; five DriverKit entries were supplied from the matching running OS.

The installed `kmutil(8)` manual describes a separate custom-boot mechanism that
requires Recovery and Permissive Security. That does not establish compatibility
of this development kernel with a custom collection. No kernel was activated,
startup policy changed, or reboot performed. See the
[KDK validation record](apple-m6-kdk-validation.json) for hashes, binary evidence,
builder errors and remaining steps.
The [resume handoff](apple-m6-resume.md) records how to continue after reboot.

## Unattended capability probe

One command compiles and launches the fixed-work probe, checks completion and the
known checksum, inspects the exported native data, and prints **only JSON** to
stdout. The output directory must be new:

```sh
.venv/bin/topdown apple probe --mode fixed --output /tmp/m6-counter-check-001
```

The same JSON is retained as `probe.json` beside the source, compiler logs and
capture artifacts. Use `--mode fixed` for the working fixed-counter path,
`--mode cpu` for cycle sampling or `--mode time` for a timer-profile check.
Default mode remains `counters`, which requests the standard CPU Counters
template and is still blocked on this machine. Each invocation performs at most
one recording; there are no automatic retries, GUI interactions or permission
changes. The target has an independent eight-second alarm. Tool calls also have
timeouts, including compile (30 seconds) and record (four seconds plus 30 seconds
for tool completion).

| Exit | JSON `status` | Meaning |
|---|---|---|
| 0 | `counters_present` | Scoped, nonzero native PMC values and probe completion observed |
| 0 | `profile_present` | Selected stack profile contains probe work; checksum/exit verified |
| 1 | `probe_failed` | Build, data, checksum, output or other probe failure |
| 2 | `permission_denied` | Tool explicitly reports a permission denial |
| 3 | `counter_data_missing` | Recorder completed but the required counter table is absent |
| 4 | `tool_unavailable` / `unsupported_host` | Required tooling/template absent or host unsupported |
| 5 | `timeout` | A bounded tool call exceeded its deadline |
| 130 | `interrupted` | Probe interrupted |

Automation should inspect `status`, `exit_code`, `target_completed` and
`sample_count`. Exit zero establishes native observations, not calibrated event
semantics or TMA. `tma_available` remains false and the four TMA values remain null.
Permission classification requires an explicit tool error; an empty counter table
is not silently relabelled as a permissions failure. Evidence is preserved even
when the command returns a nonzero exit status.
`target_completed` means both the expected checksum and successful target exit
were verified; false means unverified. The original three modes were checked on M6:
counters exited 3, CPU Profiler exited 2, and time mode exited 0 with 368 samples.
[Probe validation](apple-probe-validation.json) records hashes and the 419 passing
tests; the original capture validation below remains unchanged.

This automates the CLI capability check. It does not establish whether a separate
interactive Instruments launch would behave differently or fix the macOS KPC issue.

## OS-level follow-up on the tested M6

Rechecked on macOS 27.0.1 (26A434), Xcode 27.0 (27A266a), with Developer Tools
Security already enabled. Root CLI recordings still failed in CPU Profiler and
produced no PMU table with the standard CPU Counters template. Direct KPC fixed
counts worked as root. All 134 installed programmable events were rejected in
both execution scopes through both manual KPC and KPEP apply paths (536 tests),
as were four fully populated configurations. An empty configurable-counter
configuration was accepted.

Offline disassembly of `kernel.release.t8152`, whose UUID matches the running
kernel, shows the setter checking each selected event word's low 16 bits and
returning EPERM for a nonzero selector. That establishes the compiled restriction
behind these failures; the source/build omission that produced this policy remains
unproven. The binary hash and inspection details are retained in the validation
summary. The other shipped `t8132` kernel contains a 104-entry selector lookup
where this `t8152` path rejects nonzero selectors. A custom fixed-cycle PMI request
also failed with `event unavailable`;
the supported fixed-counter sampled path uses a timer trigger.

`kern.cpc.secure` is 1 and its live sysctl flags lack write permission. Apple's
published source makes the control writable only in DEVELOPMENT/DEBUG builds,
and its ARM KPC path returns EPERM for disallowed events. Developer Tools
authorization does not change those build-time restrictions.
[CPC build control](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/cpc.h),
[sysctl implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_cpc.c),
[KPC event check](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/arm64/kpc.c).

SIP and authenticated-root protection remain enabled. No development KDK/kernel
is installed. Booting a custom kernel would require a compatible kernel and
changes to startup security; enabling Developer Tools alone does not provide
that environment. [Apple's startup-security description](https://support.apple.com/guide/security/startup-disk-security-policy-control-sec7d92dc49f/web)
explains that custom XNU kernels use Permissive Security. No kernel, boot-policy,
permission or security-setting change was used for the working CLI paths above.

## Original Instruments template validation

Checked 20 October 2026 on Apple M6, macOS 27.0, Xcode/xctrace 27.0 (27A266a):

| Mode | Native measurement | Local result |
|---|---|---|
| `time` | Timer-sampled stacks | Capture and JSON export work |
| `cpu` | Cycle-sampled stacks | Recorder fails configuring KPC: operation not permitted |
| `counters` | Native PMC event values | Recorder exits zero, but trace has no counter table; export correctly fails |

The smoke probe completed its 268,435,456-step recurrence with the expected checksum
`9087321014236724183`. The first CLI Time Profiler run exported 369 stack samples,
367 with `alu_work` as the leaf. These observations demonstrate capture/export;
they do not establish unbiased cost fractions or timing accuracy. The earlier
CPU Counters trace contained 363 timer samples, also deliberately excluded from PMU
mode. Logs and raw traces remain in the local `.local-runs/` directory.

The final smoke using the committed example source exported 372 timer samples with
the same expected target checksum. [Validation summary](apple-m6-validation.json)
retains commands, hashes, test/coverage results and observed limitations.

Developer Tools Security reported disabled, but this observation does **not**
establish the cause or a remedy. No privilege/security setting was changed.
[Apple's M6 issue thread](https://developer.apple.com/forums/thread/849179) reports
the same KPC failure; the Apple engineer requests diagnostics and offers no fix in
the retrieved answer. Do not retry PMU collection unchanged or describe this as
working M6 TMA.

## Record and export

Install from the checkout using the project's usual Poetry environment, then:

```sh
poetry run topdown apple doctor
xcrun clang -O3 -g examples/apple-probe.c -o /tmp/topdown-apple-probe

# Launch an explicit target. The output directory must not already exist.
poetry run topdown apple record --mode time --duration 4 \
  --output /tmp/apple-time-001 -- /tmp/topdown-apple-probe

# Request real PMU values; there is no timer fallback if counters are absent.
poetry run topdown apple record --mode counters --duration 4 \
  --output /tmp/apple-pmu-001 -- /tmp/topdown-apple-probe

# Export an existing trace, including one saved from the Instruments GUI.
poetry run topdown apple export /path/to/capture.trace \
  --mode counters --output /tmp/apple-export-001
```

`--mode cpu` selects CPU Profiler instead. The `record` command launches one target
and uses `--no-prompt`; it does not attach to unrelated processes or record all
processes. Programs should have bounded lifetimes, as the included probe does.
On recorder failure/interruption, target termination is unverified; inspect the
retained recorder/target artifacts before another capture. The probe's alarm also
bounds its lifetime independently of the recorder.

Exports select one run (`--run`, default 1). The mode selects the measurement
schema; `trace` also requires its gap table. Exports use the launch PID
from trace metadata; pass `--pid` for an ambiguous or different target. Optional
`--start-ns`/`--end-ns` select a trace-relative, half-open interval. Defaults include
startup and shutdown. Signposts help select an interval in Instruments, but this
implementation does not automatically translate a signpost into the export window.

The output directory contains commands, recorder/export logs, a raw trace for new
captures, `toc.xml`, and `profile.json` when export succeeds. Sampled modes retain
`samples.xml`; trace mode retains `intervals.xml`, `gaps.xml`, and, when available,
`thread-state.xml`. New
captures also retain `capture.json` with executable/collector hashes, tool version,
host metadata and outcome. The JSON includes XML checksums, target exit metadata,
per-row PID/TID/time, and mode-specific native measurements. Sampled modes retain
core labels; trace intervals retain code locations and gap associations.
Raw artifacts may contain local paths/device metadata; keep them local and publish
only reviewed summaries and hashes.

## Units and validation limits

`cpu-pmc-value` event values stay unsigned integers, including values above 2^53.
They retain native semantics: the importer does not assume cumulative values or
deltas, sum them across samples/cores, or derive IPC. `cpu-profile` requires the
native cycle-weight type. `time-profile` retains its native weight and reports
leaf **sample counts** separately. Malformed schemas, missing/cyclic references,
ambiguous tables, inconsistent PID scope and empty windows fail the export.

Every profile includes the four TMA roots with JSON `null`. Apple documents native
bottleneck categories and its own bandwidth/time denominators; equivalence to an
Intel pipeline-slot tree has not been established. No native samples are imported
into the existing TMA funnel, so a timer profile cannot become a 100% retiring or
backend-bound claim. [Apple's analysis workflow](https://developer.apple.com/documentation/xcode/addressing-cpu-bottlenecks)
and [Instruments presentation](https://developer.apple.com/videos/play/wwdc2025/308/)
are the primary methodology references.

The `cpu-pmc-value` and cycle importers are tested against schema-shaped synthetic
rows, based on the installed Xcode declarations. The fixed `counters-profile`
importer also has a measured M6 fixture and successful native captures. M1
programmable-event groups have native captures, but their exported vector
positions still lack verified event labels. Xcode's guided M1 bottleneck shares
are available separately; derived shares from raw programmable events remain
experimental. Before using raw events for a broader TMA claim, verify their
labels, formulas, denominators, sampling, loss, and heterogeneous-core semantics
with scoped controls and matched conditions.

## Development evidence

Started from upstream `71dea5e`; local branch `feat/apple-silicon-profiling`.
The original test suite exposed two Intel-specific command assertions that depended
on the host architecture. They now explicitly simulate x86, with an added ARM
test retaining the existing system-wide behavior. No collector scope was changed.

The mandatory CPU-perf consultation used local MCP stdio, server 0.1.1, bundled
library `deb5a0bac467`. `library_status` and contextual `ask` succeeded, but the
library had 0/287 sources indexed and supplied no supporting passages. Its unread
suggestions were not treated as evidence. Calls/outputs are retained locally in
`.local-runs/cpu-perf/`. Actual implementation/review was one sequential Codex
session; exact serving model and token costs are unknown. No independent agent,
optimization population, performance acceptance, upstream PR or publication is claimed.

## Native GPU profiling

`topdown apple gpu record --output /tmp/gpu-001 --mode alu --store` captures a
checksum-verified Metal control, profiles its replay and stores native encoder
counters. Use `topdown apple gpu report --run-id RUN_ID` to rank its costs and
limiters, or `apple query RUN_ID --section encoders` for paginated raw counters.
Existing `.gputrace` captures use `apple gpu profile`. Replay requires full Xcode
and Apple M3/A17 Pro or later; stored GPU reports work on any supported OS.

MCP exposes `collect_apple_gpu_profile`, `profile_apple_gpu_trace` and
`get_apple_gpu_report`, alongside the shared import/query tools. See the
[GPU guide](apple-gpu-experiment.md#cli-database-and-mcp) for commands, native
limiter interpretation, M6 measurements and the separate M1 Instruments path.
