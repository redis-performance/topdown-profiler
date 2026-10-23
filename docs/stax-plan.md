# Stax pilot and integration plan

Status: pilot in progress. The optional integration is being tested; the acceptance
criteria below remain required before claiming platform support.

An optional v3 CLI/SQL/MCP integration is available for experimental testing.
The [Linux VM validation](stax-vm-pilot.md) includes real captures and measured
overhead; its low-pipeline latency result does not meet the declared budget.
The [Intel Linux pilot](stax-linux-pilot.md) remains blocked on that host’s
permissions. The [M1 pilot](stax-m1-pilot.md) has rootless controls but no
privileged CPU capture. M6 requires its own host-specific acceptance check.

The initial source review pins Stax to
`aedd85e08894fc3250ec975a486544d091e11281` (2026-10-20). That revision writes
archive format v3 and requires Rust 1.92, despite CLI help and some published docs
still referring to v2 and Rust 1.91. Record the binary digest as well as its version.

## Goal

Evaluate Stax for Redis development on macOS and Linux, then use the pilot results
to design an optional sampling collector alongside the existing Linux collectors
and native Apple profiling support.
Stax can add function hotspots, thread waits, and annotated disassembly to our
label-based profiling workflow.

Current `main` already provides native Apple counters, stacks, traces, artifact
storage, and MCP hotspot queries. The pilot must establish Stax's additional value
for live cross-platform inspection and thread-wait analysis, and reuse existing
storage and query conventions where their measurement semantics match.

Keep the measurements distinct: sampled CPU time, off-CPU durations, and hardware
counters are not Intel TMA pipeline-slot percentages. A hot function does not by
itself establish which function caused an aggregate TMA bottleneck. Apple Silicon
profiles must not be presented as Intel TMA funnels.

## Phase 1: Redis pilot on macOS and Linux

- Pin and record the Stax revision, Redis revision, compiler, build flags, OS,
  architecture, CPU, and capture settings for each host.
- Use an optimized Redis build with debug information and frame pointers
  (`-fno-omit-frame-pointer`). Document installation and permissions, including
  signing and the required privileged helper on macOS. Check supported capabilities
  on the actual hosts rather than assuming identical coverage. Verify M1 and M6
  independently: a running helper does not establish that programmable counters
  are available on a particular kernel. Preserve unavailable-counter status.
- Run the same defined Redis workload on each platform, recording the dataset,
  command mix, client configuration, warmup, and measurement duration. Keep
  comparisons within each host; matching workloads do not make different CPUs
  directly comparable.
- Collect repeated unprofiled and profiled runs, interleaving them to reduce drift.
  Measure throughput and latency changes, record variability, and inspect profiler
  resource use. Begin with a 30-second capture and adjust duration or frequency if
  sample quality or overhead requires it.
- Include a separate latency-sensitive, low-pipeline control. In the
  [Linux VM pilot](stax-vm-pilot.md), pipeline-16 median throughput and PING
  p99 met the declared budget while pipeline-1 PING p99 exceeded it sharply;
  that result motivates a host-specific tail check and does not predict M1/M6
  behavior. Report the probe's latency unit and scope separately from the
  main workload's end-to-end latency.
- Inspect thread breakdowns, self/total hotspots, flamegraphs, and one hot function's
  disassembly. Record unresolved symbols, truncated stacks, dropped events, and
  whether expected threads and wait information are visible.
- For Apple Silicon PMU fields, record the sampled CPU/core identity and test
  counter meaning on performance and efficiency cores separately. The pinned
  Stax v3 path discards `KdBuf.cpuid` before saving PET samples, as shown in
  the [M1 core-attribution audit](stax-m1-core-attribution-audit.json). An
  importer cannot reconstruct that identity from the archive. Keep per-core
  claims unavailable until upstream preserves it or captures are independently
  verified to be core-scoped. A proposed
  [core-identity patch](../examples/stax-m1-core-identity.patch) preserves the
  raw trace ID in a rootless control, but this is not proof of thread residency.
- Validate macOS KPC counter semantics before adding or interpreting PMU
  totals. XNU returns cumulative per-thread counters; the pinned Stax path
  treats each PET snapshot as a delta. The
  [M1 accumulation control](stax-m1-kpc-accumulation-control.json) demonstrates
  the inflation with synthetic snapshots and tests a proposed per-thread
  differencing patch. A first snapshot, counter reset, or all-zero buffer error
  requires a new baseline;
  report lost baseline coverage and native record loss. Record conversion
  provenance in the archive before an importer treats PMU fields as deltas.
  Even a valid thread delta may span several cores between samples, so a trace
  CPU ID alone cannot assign the whole delta to a P or E core.
  Store conversion provenance with each captured run and preserve it through
  open/save. Stax currently writes `SavedRunArchiveProvenance` from the saving
  server and drops that manifest field when reconstructing `SavedRunArchive`.
  A server-build marker on the manifest could therefore mislabel an old run
  after it is reopened and saved again. Test that older archives remain
  `unknown` after such a round trip.
- Save a `.stax` artifact, reopen it, and verify that the required views survive.
  Exercise baseline/candidate comparison and its JSON output.
- On macOS, compare the workflow and diagnostic coverage with existing
  `topdown apple` captures in separate matched runs. Record which Stax features
  add useful information and which duplicate current native functionality.
- On an Intel Linux host, collect TMA in a separate matched workload run initially.
  Evaluate concurrent collection only after checking counter contention and added
  overhead; separate captures are correlated evidence, not instruction-level
  attribution of TMA metrics.

Example capture after installing Stax and starting the benchmark:

```bash
# Replace 12345 with the Redis PID.
stax record --pid 12345 --time-limit 30
stax threads -n 20
stax top -n 20 --sort self
stax top -n 20 --sort total
stax flame -d 6
# Substitute a symbol observed among the hot functions if needed.
stax annotate dictFind
stax save redis-baseline.stax
```

Deliverable: a short pilot report with commands, environment details, saved profile
locations, stack-quality findings, overhead measurements, and a recommendation per
platform. Decide an acceptable overhead budget before interpreting the results.
Proceed when the required views are useful and repeatable and overhead meets that
budget; otherwise document the limitation or narrow the supported configuration.

## Phase 2: Optional collector and artifact storage

- Add a Stax subprocess adapter alongside the existing collectors; keep installation optional.
  Start with CLI orchestration. Verify a stable structured extraction path before
  implementing persistent hotspot queries; do not assume every CLI view has JSON
  output. Pin and validate the archive schema against real saved captures. Vox is
  a binary RPC protocol, not JSON-RPC; evaluate it only if archive ingestion is
  insufficient. Do not parse human-readable tables into persistent measurements.
- Own a private server and socket for each capture. Save before shutting down that
  server: `save` addresses its current query state and has no run selector. Never
  adopt a shared server or stop its current recording. Serialize captures that
  could contend for host-wide profiling facilities, even with separate servers.
- Handle availability, permissions, duration, cancellation, subprocess failures,
  and an already-active Stax recording explicitly. Do not stop unrelated recordings.
- Reuse collector metadata and add explicit capability information. Reuse benchmark labels such as
  `git_hash`, `git_branch`, `test_name`, `topology`, and `build_variant`, and associate
  matched captures through a shared benchmark execution identifier.
- Associate saved artifacts and Stax version information with runs. Define artifact
  ownership, retention, and missing-file behavior before relying on file paths.
- Store sampling summaries with explicit units and denominators, separately from
  the TMA hierarchy, with a separate result document linked to the shared run.
  Separate sample counts, scheduler intervals, and synthetic target-span durations;
  do not assume their totals form a wall-time utilization denominator. Preserve
  requested sampling frequency separately from observed sample count and
  capture duration, because configured cadence does not prove delivered samples.
  Preserve existing TMA queries and stored-run compatibility. The
  [M1 shared SQLite control](stax-m1-storage-coexistence-control.json)
  verifies Apple and Stax run routing with real native Apple data and a
  separate rootless Stax archive.
  Preserve optional raw per-sample CPU IDs when a Stax archive provides them;
  [the rootless M1 core-identity control](stax-m1-core-identity-control.json)
  verifies a proposed upstream parser-to-archive patch and topdown import.
  A raw ID is not a validated P/E core label or PMU scope.
  In the pinned Stax v3 implementation, comparison JSON's `pet_samples` count
  includes synthetic target-span markers: the
  [M1 Metal lane control](stax-m1-gpu-target-control.json) reports 299 there
  while its archive run summary has zero CPU PET samples. Validate CPU sampling
  from the run summary and keep target marker counts separate. An
  [M1-tested proposed CLI correction](../examples/stax-compare-cpu-samples.patch)
  demonstrates the separation against that saved archive; do not assume the
  pinned upstream CLI includes it.
- Reuse native Apple launch/attach and metadata conventions where applicable. If
  Stax supports collection by process name, provide portable discovery rather than
  routing macOS through the Linux `/proc` resolver or permission checks.
- Test lifecycle failures, capability reporting, storage compatibility, and unit
  separation. For target spans, report linked and unlinked origin counts plus
  `no_thread`, `no_stack`, and `too_far` reasons; keep captured origin thread IDs
  distinct from successfully linked CPU stacks. The
  [M1 rootless join control](stax-m1-origin-link-control.json) covers the
  software path with invented samples. Its
  [cadence control](stax-m1-origin-cadence-control.json) shows that the link
  fraction can fall sharply as delivered samples become sparser even when the
  requested frequency stays fixed. Check observed sample rate and origin-link
  fraction in real captures before accepting CPU-to-target attribution.
- Treat native sampling loss as unknown while the v3 archive lacks daemon and
  parser drop counters. The [M1 source audit](stax-m1-loss-accounting-audit.json)
  locates counters that are currently logged but not retained in a structured
  result. Preserve them across the RPC and archive boundary before claiming
  complete capture; a positive PET sample count alone is insufficient.

## Phase 3: CLI and MCP access

- Expose optional sampling collection and queries for hot functions and thread waits.
  Candidate MCP names are `collect_cpu_profile`, `query_hot_functions`, and
  `query_thread_waits`; finalize their contracts after the pilot and evaluate reuse
  of existing Apple result/hotspot interfaces before adding overlapping tools.
- Include collector, platform, units, sample counts, and relevant capture limitations
  in results. Keep `collect_topdown` and the pipeline funnel specific to TMA.
- Support baseline/candidate artifact comparisons with compatible workload and
  capture metadata. Report incompatible comparisons explicitly.
- Document macOS and Linux setup and a complete Redis example.
- Treat cooperating GPU spans as explicit target instrumentation, not automatic
  GPU hardware-counter collection. Keep native Metal replay on supported devices
  and the M1 Instruments counter path available separately.

## Phase 4: CI evaluation

Collect profiles as diagnostic artifacts first. Add regression gates only after
measuring run-to-run variability on controlled runners and establishing repeatable
workloads. Normalize or hold completed work constant where appropriate: a change
in total sampled time alone is not proof of a performance regression.

## Upstream references

- [Stax repository](https://github.com/bearcove/stax)
- [Platform support and counter coverage](https://stax.bearcove.eu/concepts/platform-support/)
- [Installation](https://stax.bearcove.eu/guide/getting-started/)
- [Stack unwinding and build flags](https://stax.bearcove.eu/concepts/stack-unwinding/)
- [CLI commands, archives, and comparisons](https://stax.bearcove.eu/reference/cli/)
- [Programmatic interface](https://stax.bearcove.eu/reference/rpc-services/)

Upstream behavior must be verified against the revision selected for the pilot.
