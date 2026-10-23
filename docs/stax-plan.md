# Stax pilot and integration plan

Status: proposed; no Stax installation, profiling pilot, or integration has been completed.

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
  on the actual hosts rather than assuming identical coverage.
- Run the same defined Redis workload on each platform, recording the dataset,
  command mix, client configuration, warmup, and measurement duration. Keep
  comparisons within each host; matching workloads do not make different CPUs
  directly comparable.
- Collect repeated unprofiled and profiled runs, interleaving them to reduce drift.
  Measure throughput and latency changes, record variability, and inspect profiler
  resource use. Begin with a 30-second capture and adjust duration or frequency if
  sample quality or overhead requires it.
- Inspect thread breakdowns, self/total hotspots, flamegraphs, and one hot function's
  disassembly. Record unresolved symbols, truncated stacks, dropped events, and
  whether expected threads and wait information are visible.
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
  output. Evaluate the documented Vox RPC interface only if needed.
- Handle availability, permissions, duration, cancellation, subprocess failures,
  and an already-active Stax recording explicitly. Do not stop unrelated recordings.
- Reuse collector metadata and add explicit capability information. Reuse benchmark labels such as
  `git_hash`, `git_branch`, `test_name`, `topology`, and `build_variant`, and associate
  matched captures through a shared benchmark execution identifier.
- Associate saved artifacts and Stax version information with runs. Define artifact
  ownership, retention, and missing-file behavior before relying on file paths.
- Store sampling summaries with explicit units and denominators, separately from
  the TMA hierarchy. Preserve existing TMA queries and stored-run compatibility.
- Reuse native Apple launch/attach and metadata conventions where applicable. If
  Stax supports collection by process name, provide portable discovery rather than
  routing macOS through the Linux `/proc` resolver or permission checks.
- Test lifecycle failures, capability reporting, storage compatibility, and unit
  separation. Use real captures for platform acceptance checks.

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
