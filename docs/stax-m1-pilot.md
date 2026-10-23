# Stax on Apple M1: pilot evidence and runbook

Status: source, build, and rootless functional checks completed on 23 October
2026. Privileged Stax capture remains unverified because no root helper is in
use on this M1.
This is a platform pilot for the [Stax plan](stax-plan.md),
not a new collector or a claim of Intel TMA support.

## Pinned inputs

| Input | Value |
|---|---|
| Host | MacBookAir10,1, Apple M1, macOS 27.0.1 (26A434) |
| Xcode | 27.1; full Xcode selected for builds and native Instruments through `DEVELOPER_DIR` |
| Rust | 1.93.1 |
| Stax | `aedd85e08894fc3250ec975a486544d091e11281` (0.1.1 CLI) |
| Redis control | `d85b5f4c2cc597da78989bc48d6b38b895c186f6` (8.2.2) |
| Redis mixed pilot | `006e6a6a602ac5265a676b3aebe9f8ba65459190` (8.6.0, matching the Linux pilot) |
| memtier mixed pilot | `f3ff1793d39bf19077957e986f966206eb349de7` (2.5.2) |

The Stax release build used `CARGO_BUILD_JOBS=2 CARGO_PROFILE_RELEASE_DEBUG=0`
and the repository lockfile. The three original binaries were staged in
`~/.cargo/bin` and signed ad hoc because this host has no valid Apple
Development or Developer ID identity. Their pre-signing SHA-256 values are:

| Binary | SHA-256 |
|---|---|
| `stax` | `3b2fe0797e8968c157dde5e7f6d96cf8b6333c81af75b7a19e8478b81edc35af` |
| `stax-server` | `2a32a2168d9fb5893298de5784e47340dac83d07186d47de462f1ee150d66fe7` |
| `staxd` | `d79fa50ba7396d7908fefba9b9deb21bb7e39921265384bbb4044405` |

The installed binaries have different hashes after code signing. The pilot
harness records their installed hashes in `metadata.json`; compare those for
actual run identity.

The staged `staxd` was subsequently replaced with the
[teardown-patched](../examples/stax-m1-teardown.patch) binary. The patch SHA-256
is `60bbd2b9163ea6663fcc783bc8d3b3c4c88cf02df97ec7843caab47e58607fd7`.
The patched binary's pre-signing SHA-256 is
`73d32a600b6bb62569feb9591c7e9b91191c0cdd63ec362a1142919586fee5b0`;
the installed ad hoc signed SHA-256 is
`2abaeafb83ae152dea57c83b1532b64edb6e3a5a481ec87d688a6dcc9c9031fe`.
The patched daemon compiled and its direct-run socket was smoke-tested without
root: the socket was `0666` inside a `0700` directory. Its crate currently has
no unit tests; the real cleanup path still needs a privileged capture check.

`cargo build --release --locked -p stax -p stax-server -p staxd` failed with
`'cassert' file not found` under the selected Command Line Tools. The same
command succeeded with
`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`. Both `codesign
--verify` and a private, unprivileged `stax-server` smoke test succeeded:
`stax status` returned `no active run`. This check did not attempt collection.

Redis 8.2.2 was built from the pinned source with `MALLOC=libc`, no TLS, `-O3`,
`-fno-omit-frame-pointer`, and `-g`. The explicit `OPT` avoids its default
`-flto`; with LTO, `dsymutil` could not reopen the temporary object and did
not create useful standalone debug information. The final Redis binary SHA-256
is `a5ca88abc09c74b1e5e6dde6619b29b976fb69215e40b554272e5f60b871b155`.
Its dSYM DWARF file SHA-256 is
`59c078d59de5f0e0219303ea5c7b45898b2e3e5562dac38b3c2e4e9e9bf9bdcf`,
and both files share Mach-O UUID `87AA7F24-00CC-32A6-8944-CEBB72510943`.
Preserve the exact binary and dSYM alongside any final captured archive.

Redis 8.6.0 uses `MALLOC=libc`, no TLS, `-O2 -fno-omit-frame-pointer`, and `-g`.
Its binary SHA-256 is
`2f668b956b2a6953f77c49b8c35ef4a3e8a041488a8229c25253b2d076593bd9`.
The matching dSYM DWARF SHA-256 is
`6772789a9d3ee667af2fd3824bd42aaccc7e73b4723de4b0ca8fa0df515aeb23`.
memtier 2.5.2 was built locally with TLS disabled; its binary SHA-256 is
`1084c96574d548f919f5cdeb5352a11176cd55bdb38f5071443b9be49b548f8d`.
The previously installed memtier 1.3.0 emitted a zero-duration JSON result
with a rate inconsistent with its completed request count. The 2.5.2 build
reported consistent count, duration, and throughput in a local smoke test.

## M1 counter review

At the pinned Stax revision, the macOS capture client passes an empty
`pmu_event_configs`, requests only `KPC_CLASS_FIXED_MASK`, and sets both
configurable sample indices to `None` in `staxd-client/src/lib.rs`. The live
path therefore requests fixed cycles and instructions only. The upstream
[platform support page](https://stax.bearcove.eu/concepts/platform-support/)
describes L1 miss and branch-mispredict support, but those events are not
wired into this revision's macOS recording path. A zero in their result fields
must be treated as unavailable, not as a measured zero.

The unused `stax-mac-kperf-sys/src/pmu_events.rs` helper also needs correction
before enabling those events. It adds events before forcing KPEP counter
allocation and assumes accepted events occupy consecutive counter slots. The
rootless [M1 KPEP probe](../examples/stax_m1_kpep_probe.py) reproduces the
difference; its [captured output](stax-m1-kpep.json) is from this host:

| Setup | L1D miss add | Branch mispredict add | Sample indices after two fixed counters |
|---|---:|---:|---|
| Current helper order | 0 | 13 (rejected) | L1D: 2 |
| Force allocation first | 0 | 0 | L1D: 2, branch: 5 |

The actual mapping comes from `kpep_config_kpc_map`; it is not the requested
event order. With forced allocation, KPEP returns eight configurable config
slots; only slots 0 and 3 are nonzero (`0xa3` and `0xcb`). The current helper's
single accepted event yields only slot 0. For the fixed-plus-configurable class
mask (`3`), `kpc_get_config_count` returns 8 while `kpc_get_counter_count`
returns 10; the eight-element config vector matches the reported configuration
count. This probe reads the local event
database only. It does not enable counters or prove that either configurable
event can be sampled by a live
recording. Keep live acceptance separate from this configuration check.

An [experimental patch](../examples/stax-m1-pmu-experimental.patch) for the
pinned Stax source moves counter allocation before event additions, uses
`kpep_config_kpc_map` for parser indices, and wires the two events into the
macOS recorder only when its **server process** has
`STAX_EXPERIMENTAL_APPLE_PMU=1`. Its SHA-256 is
`fdf2eff435c67e84239b41e7d228c137d09d64fd2d3ab9d76695da5cda3107a0`.
The patch applies cleanly to the pinned index and compiled in release mode.
It includes a parser regression test for the zero-filled counter slots between
L1D and branch mispredicts; all five macOS parser unit tests pass.
Calling the patched Rust `configure` on this M1, without root, returned class
mask 3, fixed count 2, sample indices `[2, 5]`, and config words
`[163, 0, 0, 203, 0, 0, 0, 0]`. A separately staged, ad hoc signed
`stax-server` using the patch answered `stax status` over a private socket.
Its signed SHA-256 is
`3163a2fdd2cd09aa9eb4026c789f16de28781fb44880152156c4b890ea225be3`.
The normal server and all captures without the opt-in variable retain the
fixed-counter path.
In a [rootless end-to-end negative control](stax-m1-pmu-rootless-control.json),
the opt-in server, CLI, and unprivileged helper reached the helper's `NotRoot`
gate after creating a run;
the run reported zero kperf samples. This exercises the client configuration
path and RPC wiring without claiming hardware collection.

A second [software-path control](stax-m1-synthetic-pmu-control.json) used a
[synthetic sample daemon](../examples/stax_m1_metal_lane/fake_sample_daemon.rs)
that rejects any client configuration other than class mask `3` and the M1
KPEP words `[163, 0, 0, 203, 0, 0, 0, 0]`. It emitted invented PET/PMC
records with cycles `100`, instructions `200`, L1D misses `13` at sample slot
2, and branch mispredicts `17` at slot 5. The patched Stax server captured 78
such samples alongside 299 real M1 GPU spans. PR #47's importer stored all 78
CPU samples with those four exact values, and gave CPU hotspots a denominator
of 78. This proves the opt-in configuration and parser/index wiring through
the archive and SQLite path; **it does not measure Apple PMU hardware**. The
test process bypassed the collector's root-owned helper-socket preflight only
for the fake daemon.

To reproduce the experimental server from a clean pinned Stax checkout, apply
the patch, build `stax-server` with full Xcode selected, and stage it separately
from the baseline server:

```sh
cd /private/tmp/stax-m1-pilot
git apply /private/tmp/topdown-m1-stax/examples/stax-m1-pmu-experimental.patch
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  CARGO_BUILD_JOBS=2 CARGO_PROFILE_RELEASE_DEBUG=0 \
  cargo build --release --locked -p stax-server
install -d -m 700 /private/tmp/stax-m1-pmu-experimental
install -m 755 target/release/stax-server \
  /private/tmp/stax-m1-pmu-experimental/stax-server
codesign --force --sign - /private/tmp/stax-m1-pmu-experimental/stax-server
```

For the separate experimental pilot, set
`STAX_EXPERIMENTAL_APPLE_PMU=1`, add
`--stax-server /private/tmp/stax-m1-pmu-experimental/stax-server` to the
benchmark command below, and choose a new `--output` directory.

The experimental patch has **not** been tested with privileged kperf sampling. Run the
unmodified Stax server first for the baseline pilot; then use the patched
server in a separate capture with a new output directory and the opt-in
variable. The pilot records the server digest and opt-in state. Live acceptance
must confirm nonzero, plausible L1D and branch values, correct placement in
the sampled counter vector, and comparison against separate native event
captures. The pinned CLI does not expose these fields in its `top` table; they
exist in Stax's sample/aggregator data and web UI. Do not label zero or missing
fields as measured event counts.

## M1 Metal clock and Stax target lanes

Stax's optional `stax-target` API accepts nanosecond timestamps based on
`mach_absolute_time` on macOS. Apple's
[command-buffer timing contract](https://developer.apple.com/documentation/metal/mtlcommandbuffer/gpustarttime)
says `gpuStartTime` and `gpuEndTime` are host seconds relative to system mach
time. The existing [Metal probe](../topdown/collector/apple_gpu_probe.swift)
was compiled with Xcode 27.1 and run on this M1. For compute, ALU, memory,
and stream workloads, all twelve completed command buffers had positive GPU
durations and their start/end times, multiplied by `1e9`, fell between
`mach_absolute_time` samples taken before and after the child process. The
[raw check](stax-m1-gpu-clock.json) records the actual nanosecond windows,
source/binary hashes, and the host's Mach timebase (`125/3`). This agrees with
Apple's documented clock relationship; the process brackets alone would not
establish exact correlation.

An instrumented Metal app can therefore report one Stax target span per
completed **command buffer** using these timestamps, capturing a CPU origin at
submission and converting finite, positive host seconds to nanoseconds. The
start/end properties must be read after completion. A command buffer can hold
multiple encoders and dispatches, so these properties do not give individual
kernel duration or hardware-counter attribution. On this M1, the separate
Instruments Metal System Trace counter workflow remains the native GPU
counter path; Xcode's packaged GPU replay profiler requires newer hardware
on this host.
The clock test alone does not validate ingestion or CPU origin linking.

The [rootless Metal lane control](stax-m1-gpu-target-control.json) used a
[source-only Swift/Rust example](../examples/stax_m1_metal_lane/README.md) and
an unprivileged fake helper that streamed **zero** kperf records. The real M1
GPU completed 300 checksum-verified command buffers; after Stax's capture gate
opened, the app reported 299 spans. Stax ingested all 299 under one Metal lane,
showed 21.489 ms of target time in `threads`, `top`, and `flame`, and reopened a
v3 `.stax` archive with those views and diagnostics byte-for-byte unchanged.
The origins were all unlinked for `no_thread`, as expected without CPU samples.
This establishes M1 Metal span ingestion and archive behavior separately from
privileged CPU/PMU sampling.

The same control exposed a unit-label trap in pinned Stax: `stax compare
--json` reported `pet_samples=299` for this archive while its run summary
reported zero CPU PET samples. Its target spans create synthetic sample
markers that the comparison currently includes under that field. The Redis
pilot checks the archived run summary for positive CPU PET samples and records
comparison counts only as sample markers. Target duration is parallel GPU
work, not a CPU utilization denominator.

A proposed [Stax CLI patch](../examples/stax-compare-cpu-samples.patch) uses
the run summary's kernel PET count when available and excludes reserved
synthetic lane IDs from the legacy archive fallback. Its SHA-256 is
`918927718866107970738f30db1244ac82c85f36f8d216618f7cec752edeafcd`.
The [offline control](stax-m1-compare-cpu-sample-control.json) compares the
same saved M1 Metal archive with itself: the original CLI reports 299 PET
samples and 299 target spans, while the patched CLI reports **zero** PET
samples and 299 target spans. All 39 CLI unit tests pass with the patch, and
it applies to the pinned Stax index. The patched CLI is staged only in the
local Stax build; the installed baseline CLI and pilot interpretation remain
unchanged pending upstream review.
On the synthetic CPU plus real GPU control, the original CLI reports 377 PET
samples while the patched CLI reports the 78 invented CPU samples and 299
target spans. This checks that the correction preserves CPU-sample counts
while excluding target markers.
On the linked-origin control, the installed CLI reports 640 PET samples
(341 invented CPU samples plus 299 target markers); the proposed patch
reports 341 CPU samples, 299 spans, and 299 linked origins. An offline
comparison with the 100 ms fixture reports 341 versus 77 CPU samples and
299 versus 147 linked origins, keeping the 299 target spans in each capture.

The concurrent optional collector draft, [PR #47](https://github.com/redis-performance/topdown-profiler/pull/47)
at `c3ba2de4af57f9ad0f8aa4e9b4cfbc9494257579`, also passed an M1
rootless [integration control](stax-m1-pr47-rootless-integration-control.json).
First, its v3 importer loaded the saved 299-span Metal archive and stored it
in SQLite with zero CPU samples and a zero CPU-hotspot denominator. Then its
capture adapter launched the real M1 Metal app through the pinned Stax CLI and
server, saved a directory archive, imported it, and returned all 299 named
target spans. The app completed 300 GPU command buffers with the expected
checksum and exited cleanly. The control substituted an unprivileged helper
that emits no kperf records and bypassed only the adapter's helper-ownership
preflight inside the test process; it does not validate privileged CPU stacks,
PMU counters, or capture acceptance on macOS.
The same control was rerun against PR #47 at
`275036cb30eae3b0fa5aae5155fbe81ca75a55d7`: its new `processes.json`
matched capture metadata, the M1 app again reported 299 spans and exited
cleanly, the synthetic archive imported with 78 CPU samples and one target
lane, and all 112 focused Stax integration tests passed on this host.
Its public `stax import`, `query`, `hotspots`, and `targets` commands also
preserved the 78 invented CPU samples, four fixture PMU fields, and 299 real
GPU spans. The imported `sampling_frequency_hz=99` label is the requested
setting; the fake helper actually supplied about ten samples per second.
Requested frequency and observed sample count must stay distinct in reports.

An additional [rootless origin-link control](stax-m1-origin-link-control.json)
ran against PR #47 at `b6d2897440d62840f7bf3e7bf18fb275fab495e7`.
The Swift app exposed its real submitting thread ID to the fake sample daemon,
which supplied invented CPU stacks every 20 ms. The M1 GPU completed 300
checksum-verified command buffers. Stax recorded 299 spans and linked all 299
to that thread's synthetic CPU stack within its 50 ms link window; the largest
recorded distance was 22.653 ms. PR #47 imported 341 invented CPU samples and
all 299 linked spans into SQLite, preserving the origin thread ID in every
stored span. This verifies the software join and persistence path only. The
stack and PMU fields are fixture data, and neither hardware kperf sampling nor
real CPU-to-GPU attribution has been verified.
Two saved negative controls had 299 spans and zero links each: one had no CPU
samples, and one had 78 invented samples on fixture thread `42` rather than
the app's submitting thread. Both reported `no_thread=299`. The matched-thread
control reported zero `no_thread`, `no_stack`, or `too_far` failures.

Six [rootless cadence captures](stax-m1-origin-cadence-control.json) held the
requested frequency at 99 Hz and used the same 300-command-buffer M1 workload.
At invented sample periods of 20, 50, and 100 ms, two runs linked respectively
299/298, 287/286, and 147/147 of 299 spans. The 50 and 100 ms runs reported
12/13 and 152/152 `too_far` failures; one 20 ms run reported a single
`no_thread` startup miss. This is a software fixture result, but it shows why
the requested frequency and the actual CPU sample count must both be checked
before treating origin links as useful attribution.

## Live Redis pilot

The pinned `staxd` makes its Unix socket world writable (`0666`) and has no
peer UID check. Its generated LaunchDaemon plist does not restrict the socket
through a launchd `Sockets` entry. The pilot therefore uses an ephemeral root
helper socket inside a directory owned by the profiling user with mode `0700`.
Other local UIDs cannot traverse that directory; processes running as the same
user remain trusted. The pinned session driver also returned early after a
partially successful kperf/kdebug setup without teardown. Apply the small
[teardown guard patch](../examples/stax-m1-teardown.patch) before running its
root helper:

```sh
cd /private/tmp/stax-m1-pilot
git apply --unidiff-zero /private/tmp/topdown-m1-stax/examples/stax-m1-teardown.patch
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  CARGO_BUILD_JOBS=2 CARGO_PROFILE_RELEASE_DEBUG=0 \
  cargo build --release --locked -p staxd
install -m 755 target/release/staxd /Users/fco/.cargo/bin/staxd
codesign --force --sign - /Users/fco/.cargo/bin/staxd
codesign --verify /Users/fco/.cargo/bin/staxd
install -d -m 700 /private/tmp/stax-m1-private
```

The host owner starts the helper in a separate Terminal and leaves it running
for the capture. Closing it ends the privileged session; no LaunchDaemon is
installed. The pilot verifies that the socket is root-owned and that its
containing directory is owned by the profiling user with no group or other
permissions:

```sh
sudo /Users/fco/.cargo/bin/staxd \
  --socket /private/tmp/stax-m1-private/staxd.sock
```

Build a symbolized Redis 8.6.0 from the pinned source and create its dSYM;
build memtier 2.5.2 from its pinned source:

```sh
cd /private/tmp/redis86-m1-stax
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  make -j2 OPT='-O2 -fno-omit-frame-pointer' MALLOC=libc BUILD_TLS=no \
  redis-server redis-cli
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcrun dsymutil src/redis-server -o src/redis-server.dSYM
cd /private/tmp/memtier-m1-stax
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
autoreconf -ivf
PKG_CONFIG_PATH=/opt/homebrew/lib/pkgconfig \
  ./configure --disable-tls CXXFLAGS='-O2 -g -fno-omit-frame-pointer'
make -j2
```

From this repository, run three baseline/profiled pairs. This follows the
Linux pilot's Redis 8.6 workload: 10,000 prefilled keys, 64-byte values,
5 seconds of warmup, then 30 seconds of mixed 1:1 SET/GET using two memtier
threads, ten clients per thread, and pipeline depth four. The script starts a
private Redis instance on loopback, and a fresh private Stax server for each
profiled round. Baseline rounds have no Stax server. It balances run order to
reduce drift and stops its own recorder and server. Each profiled round saves a `.stax`
archive, reopens it, and records whether the all-thread view, self/total hotspot
views, flame view, diagnostics, and any successful `processCommand`
annotation are unchanged. The saved v3 archive must have a matching Redis PID
and a positive CPU PET count in its run summary; the baseline/candidate
comparison must return valid JSON. Stax's comparison field named
`pet_samples` can include synthetic target-span markers, so it is recorded as
sample markers and is not used to validate CPU sampling. An
unavailable annotation is retained as a recorded error without losing the
archive. It retains raw
memtier JSON, Stax views, commands, hashes, and environment metadata:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
python3 examples/stax_redis_pilot.py \
  --output /private/tmp/stax-memtier-m1-001 \
  --stax-revision aedd85e08894fc3250ec975a486544d091e11281 \
  --redis-revision 006e6a6a602ac5265a676b3aebe9f8ba65459190 \
  --memtier-revision f3ff1793d39bf19077957e986f966206eb349de7 \
  --workload memtier --pairs 3 \
  --daemon-socket /private/tmp/stax-m1-private/staxd.sock \
  --redis-server /private/tmp/redis86-m1-stax/src/redis-server \
  --redis-cli /private/tmp/redis86-m1-stax/src/redis-cli \
  --memtier /private/tmp/memtier-m1-stax/memtier_benchmark \
  --stax-build-flags 'release locked debug=0 jobs=2; Xcode 27.1' \
  --redis-build-flags 'OPT=-O2 -fno-omit-frame-pointer MALLOC=libc BUILD_TLS=no'
```

For the earlier fixed-count Redis benchmark control, use
`--workload redis-benchmark` with the 8.2.2 Redis and benchmark binaries;
that workload is separate from the Linux pilot. The memtier mode validates its
reported rate against completed requests and duration, and flags runs where
GET misses exceed 1%. Compare measured throughput and p50/p99 latency within
the M1 host; the summary includes per-mode throughput ranges, median p50/p99
latencies, observed changes, and each adjacent baseline/Stax pair's order and
throughput/latency changes. Use the paired values to inspect order effects
before interpreting the pooled medians. The client version and CPU topology differ
from Linux. The script samples Redis resident memory in every round and Stax
server and recorder memory in profiled rounds, once per second. Its summary
gives median and peak observed RSS for each process and mode; it does not
observe root `staxd` memory. Record actual
stack depth, unresolved symbols, waits,
dropped events, helper resource use, and the disassembly of one observed hot
symbol before accepting the pilot. The default annotation target is
`processCommand`; use `--annotate-symbol SYMBOL` when another function is hot.
The host had about 2.4 GiB free during preparation. The runner records free
space and uses a 1.5 GiB reserve before each round and archive save; set
`--min-free-gib` explicitly only if the available disk budget changes. A
reserve failure retains completed round artifacts.
Do not infer Intel pipeline-slot shares from Stax
samples or M1 fixed counters.

A [four-round rootless Redis control](stax-m1-synthetic-redis-pilot.json)
exercised the full runner with the synthetic sample helper and the opt-in PMU
server. It exposed two preflight/lifecycle problems: `stax-server --version`
stays running instead of returning a version, and retaining one server across
all rounds carried large archive state into baseline measurements. The runner
now records the server binary digest without invoking its version flag, starts
a fresh server and socket for each profiled round, and runs baseline rounds
without a Stax server. In the corrected control, both profiled archives had
positive invented PET counts, preserved the fixture PMU values, reopened with
all five checked views unchanged, and compared offline after both servers
stopped. All four Redis rounds had zero GET misses. The final unprofiled round
shifted markedly, and the helper emitted invented records at about 10 Hz;
those throughput and latency values are **not** a Stax overhead measurement.

The existing native Apple fixed-counter stack capture and M1 event-group
experiments provide separate comparison evidence. Run native Apple capture
outside the Stax session because the tools may compete for host-wide PMU and
trace facilities. Stax target lanes can show GPU spans only when a cooperating
application reports them; they are separate from native Metal hardware
counters and replay diagnostics.

One separate [native M1 Redis control](stax-m1-native-control.json) completed
before a Stax recording was available. `topdown apple stat` observed the pinned
Redis PID for 35 seconds while the 30-million-request SET/GET benchmark took
22.81 seconds. It returned 349,636,130,111 instructions and 69,575,181,942
cycles through `RUSAGE_INFO_V6`; the result also splits performance-core and
other-core counts. The observation includes startup and idle time around the
benchmark, so those totals are proof of native counter access rather than a
per-request estimate or a matched Stax comparison. The raw local `stat.json`
SHA-256 is in the control summary.

A separate [native fixed-counter stack capture](stax-m1-native-fixed.json)
launched the same Redis binary and benchmark through Xcode 27.1. It produced
23,844 target samples, one unresolved stack sample, and 320 distinct named
hotspots. `processCommand` appeared in 11,213 stacks and was the leaf in 872;
the report keeps those denominators separate. This capture includes startup
and a 1-million-request warmup, so it establishes symbol and stack quality
for the native path but is not a paired Stax overhead measurement. The raw
45 MB profile remains local; its SHA-256 is in the compact summary.

The cross-platform workload shape also has a
[native M1 control](stax-m1-memtier-native-control.json): pinned Redis 8.6.0
and memtier 2.5.2 completed 16,078,148 mixed operations in 30 seconds
(535,938 operations/s computed from count and duration), with 0% GET misses,
0.151 ms p50, and 0.327 ms p99 reported latency. A separate 35-second
`topdown apple stat` observation of Redis returned 204,615,507,802
instructions and 63,963,907,601 cycles. That observation includes idle time
around the client run and does not establish Stax overhead. There was no CPU
affinity on this M1; the performance/other-core split remains in the summary.

A second [unprofiled repeat control](stax-m1-memtier-repeat-control.json) ran
six sequential rounds of that Redis 8.6.0/memtier 2.5.2 workload with a fresh
10,000-key prefill and five-second warmup before each 30-second measurement.
All six had zero GET misses. Throughput was 537,365–539,125 operations/s in
rounds 1–4, then 581,123 and 577,153 operations/s in rounds 5–6. The six-round
median was 539,077 operations/s, and the largest deviation from it was 7.80%.
p99 latency ranged from 0.327 to 0.359 ms. The reason for the late throughput
rise is unknown; this is observed unprofiled variation, not profiler overhead.
The local raw metadata and command logs remain at
`/private/tmp/stax-m1-baseline-variability-001`; their metadata SHA-256 is in
the compact control. For the live pilot, use the planned interleaved pairs and
treat an apparent effect that overlaps this drift as inconclusive. The working
acceptance budget, set before examining a profiled result, is at most 10%
median throughput loss and at most 20% median p99 latency increase, with zero
GET misses and no material Stax event loss. More rounds are required if paired
results straddle either limit or show comparable order effects.
