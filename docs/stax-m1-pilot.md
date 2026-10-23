# Stax on Apple M1: pilot evidence and runbook

Status: source, build, and unprivileged server checks completed on 23 October
2026. A live Stax recording is pending installation of its macOS root helper.
This is a platform pilot for [PR #38's Stax plan](https://github.com/redis-performance/topdown-profiler/pull/38),
not a new collector or a claim of Intel TMA support.

## Pinned inputs

| Input | Value |
|---|---|
| Host | MacBookAir10,1, Apple M1, macOS 27.0.1 (26A434) |
| Xcode | 27.1; full Xcode selected only for build commands through `DEVELOPER_DIR` |
| Rust | 1.93.1 |
| Stax | `aedd85e08894fc3250ec975a486544d091e11281` (0.1.1 CLI) |
| Redis | `d85b5f4c2cc597da78989bc48d6b38b895c186f6` (8.2.2) |

The Stax release build used `CARGO_BUILD_JOBS=2 CARGO_PROFILE_RELEASE_DEBUG=0`
and the repository lockfile. The three binaries were staged in `~/.cargo/bin`
and signed ad hoc because this host has no valid Apple Development or Developer
ID identity. Their pre-signing SHA-256 values are:

| Binary | SHA-256 |
|---|---|
| `stax` | `3b2fe0797e8968c157dde5e7f6d96cf8b6333c81af75b7a19e8478b81edc35af` |
| `stax-server` | `2a32a2168d9fb5893298de5784e47340dac83d07186d47de462f1ee150d66fe7` |
| `staxd` | `d79fa50ba7396d7908fefba9b9deb21bb7e39921265384bbb4044405` |

The installed binaries have different hashes after code signing. The pilot
harness records their installed hashes in `metadata.json`; compare those for
actual run identity.

`cargo build --release --locked -p stax -p stax-server -p staxd` failed with
`'cassert' file not found` under the selected Command Line Tools. The same
command succeeded with
`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`. Both `codesign
--verify` and a private, unprivileged `stax-server` smoke test succeeded:
`stax status` returned `no active run`. This check did not attempt collection.

Redis was built from the pinned source with `MALLOC=libc`, no TLS, `-O3`,
`-fno-omit-frame-pointer`, and `-g`. The explicit `OPT` avoids its default
`-flto`; with LTO, `dsymutil` could not reopen the temporary object and did
not create useful standalone debug information. The final Redis binary SHA-256
is `a5ca88abc09c74b1e5e6dde6619b29b976fb69215e40b554272e5f60b871b155`.
Its dSYM DWARF file SHA-256 is
`59c078d59de5f0e0219303ea5c7b45898b2e3e5562dac38b3c2e4e9e9bf9bdcf`,
and both files share Mach-O UUID `87AA7F24-00CC-32A6-8944-CEBB72510943`.
Preserve the exact binary and dSYM alongside any final captured archive.

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

## Live Redis pilot

Stax requires `staxd` as a root LaunchDaemon on macOS. After reviewing the
source and signed binary, the host owner can install it once:

```sh
sudo env STAX_CODESIGN_IDENTITY=- /Users/fco/.cargo/bin/stax setup --yes
test -S /var/run/staxd.sock
```

Build a symbolized Redis from the pinned source and create its dSYM:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  make -j2 OPT='-O3 -fno-omit-frame-pointer' MALLOC=libc BUILD_TLS=no \
  redis-server redis-cli redis-benchmark
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcrun dsymutil src/redis-server -o src/redis-server.dSYM
```

From this repository, run two baseline/profiled pairs. The script starts a
private Stax server and a private Redis Unix socket, warms Redis before each
fixed-count SET/GET benchmark, and uses an ABBA order to reduce drift. It
stops only the recorder it started. Each profiled round saves a `.stax`
archive, reopens it, and checks that the all-thread view, self/total hotspot
views, flame view, and diagnostics are unchanged. It retains raw
benchmark CSV, Stax views, commands, hashes, and environment metadata:

```sh
python3 examples/stax_redis_pilot.py \
  --output /private/tmp/stax-redis-m1-001 \
  --stax-revision aedd85e08894fc3250ec975a486544d091e11281 \
  --redis-revision d85b5f4c2cc597da78989bc48d6b38b895c186f6 \
  --redis-server /private/tmp/redis-m1-stax/src/redis-server \
  --redis-cli /private/tmp/redis-m1-stax/src/redis-cli \
  --redis-benchmark /private/tmp/redis-m1-stax/src/redis-benchmark \
  --stax-build-flags 'release locked debug=0 jobs=2; Xcode 27.1' \
  --redis-build-flags 'OPT=-O3 -fno-omit-frame-pointer MALLOC=libc BUILD_TLS=no'
```

The script defaults to 30 million requests **per** SET and GET command in
each round. Read the raw CSV as well as `metadata.json`: the measured wall
time includes benchmark client effects, and the reported request rates have
their own denominator. The script samples Redis, Stax server, and recorder
resident memory once per second in every round; it does not observe root
`staxd` memory. Record actual stack depth, unresolved symbols, waits,
dropped events, helper resource use, and the disassembly of one observed hot
symbol before accepting the pilot. Use `stax annotate SYMBOL` against the
private server while the script runs, or reopen a saved archive in a separate
server session afterward. Do not infer Intel pipeline-slot shares from Stax
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
