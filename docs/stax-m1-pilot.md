# Stax on Apple M1: pilot evidence and runbook

Status: source, build, and unprivileged server checks completed on 23 October
2026. A live Stax recording is pending a short-lived macOS root helper session.
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
event order. This probe reads the local event database only. It does not enable
counters or prove that either configurable event can be sampled by a live
recording. Keep live acceptance separate from this configuration check.

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
kernel duration or hardware-counter attribution. The existing native Metal
counter and replay workflow remains the source for GPU bottleneck counters.
This test validates the clock conversion on M1; ingestion, origin linking, and
rendering of a real Metal span still require a live Stax recording.

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
private Stax server and a private Redis instance on loopback, and balances
run order to reduce drift. It
stops only the recorder it started. Each profiled round saves a `.stax`
archive, reopens it, and checks that the all-thread view, self/total hotspot
views, flame view, diagnostics, and any successful `processCommand`
annotation are unchanged. An unavailable annotation is retained as a recorded
error without losing the archive. It retains raw
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
latencies, and observed changes. The client version and CPU topology differ
from Linux. The script samples Redis, Stax server, and recorder resident memory
once per second in every round. Its summary gives median and peak observed RSS
for each process and mode; it does not observe root `staxd` memory. Record actual
stack depth, unresolved symbols, waits,
dropped events, helper resource use, and the disassembly of one observed hot
symbol before accepting the pilot. The default annotation target is
`processCommand`; use `--annotate-symbol SYMBOL` when another function is hot.
Do not infer Intel pipeline-slot shares from Stax
samples or M1 fixed counters.

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
