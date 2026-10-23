# M1 Metal command-buffer lane control

This source-only example links an M1 Metal app to Stax's `stax-target` API.
The Swift app reports completed command-buffer intervals in the host Mach
clock domain. The Rust static library captures the submitting CPU origin and
passes each interval to a named Metal lane. The fake daemon supplies **no
kperf records**; it is only for testing target-span ingestion without root.
Never use it to validate CPU sampling or Apple hardware counters.

The [M1 result](../../docs/stax-m1-gpu-target-control.json) was produced with
Stax `aedd85e08894fc3250ec975a486544d091e11281`, Xcode 27.1, and
300 checksum-verified GPU command buffers. Stax ingested 299 spans after its
capture gate became active, saved a v3 archive, and reproduced `threads`,
`top`, `flame`, and `diagnose` exactly after reopening it. The 299 unlinked
origins are expected: the fake daemon provided zero CPU samples. Stax's
comparison field called `pet_samples` includes synthetic target markers in
this case, while the archive's run summary correctly records zero CPU PET
samples.

To rebuild, use a disposable checkout of the pinned Stax revision. From this
repository's root, copy the example into that workspace:

```sh
cp -R examples/stax_m1_metal_lane /private/tmp/stax-m1-pilot/stax-metal-bridge
install -d /private/tmp/stax-m1-pilot/staxd/examples
cp examples/stax_m1_metal_lane/fake_daemon.rs \
  /private/tmp/stax-m1-pilot/staxd/examples/fake_daemon.rs
```

Add `"stax-metal-bridge",` to the workspace `members` list in Stax's root
`Cargo.toml`. The example's `Cargo.toml` then resolves `../stax-target`.

```sh
cd /private/tmp/stax-m1-pilot
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  CARGO_BUILD_JOBS=2 CARGO_PROFILE_RELEASE_DEBUG=0 \
  cargo build --release -p stax-m1-metal-bridge
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  CARGO_BUILD_JOBS=2 CARGO_PROFILE_RELEASE_DEBUG=0 \
  cargo build --release --locked -p staxd --example fake_daemon
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcrun swiftc -O stax-metal-bridge/metal_lane.swift \
  target/release/libstax_m1_metal_bridge.a \
  -o /private/tmp/stax-m1-metal-lane
codesign --force --sign - target/release/examples/fake_daemon
codesign --force --sign - /private/tmp/stax-m1-metal-lane
```

On this Xcode host, directly linking a Rust `cdylib` failed with a misaligned
Mach-O LINKEDIT string pool. The static-library bridge above linked and ran.
The production Stax server and CLI can be the unmodified pinned binaries;
the fake daemon is needed only for this rootless control. Start them on
private sockets, then launch the app under Stax:

```sh
install -d -m 700 /private/tmp/stax-m1-gpu-demo
/private/tmp/stax-m1-pilot/target/release/examples/fake_daemon \
  /private/tmp/stax-m1-gpu-demo/helper.sock &
GPU_FAKE_DAEMON_PID=$!
STAX_SERVER_SOCKET=/private/tmp/stax-m1-gpu-demo/server.sock \
  STAX_SERVER_WS_BIND=127.0.0.1:0 stax-server &
GPU_STAX_SERVER_PID=$!
trap 'kill "$GPU_STAX_SERVER_PID" "$GPU_FAKE_DAEMON_PID" 2>/dev/null || true' EXIT
sleep 2
STAX_SERVER_SOCKET=/private/tmp/stax-m1-gpu-demo/server.sock \
  stax record --daemon-socket /private/tmp/stax-m1-gpu-demo/helper.sock \
  -- /private/tmp/stax-m1-metal-lane 300
STAX_SERVER_SOCKET=/private/tmp/stax-m1-gpu-demo/server.sock \
  stax threads -n 0
STAX_SERVER_SOCKET=/private/tmp/stax-m1-gpu-demo/server.sock \
  stax top -n 20 --sort self
```

The app reports one span per completed **command buffer**. That timestamp
does not separate multiple kernels within a command buffer and carries no
GPU hardware counters. Native CPU stack origins, waits, and PMU data alongside
this GPU lane remain unverified without a real Stax capture.

## Raw CPU identity software-path control

For raw CPU identity, a separate [proposed Stax patch](../stax-m1-core-identity.patch)
adds an optional CPU ID to each saved PET sample. Apply it to a **clean**
checkout of the pinned Stax revision; it does not require the experimental
configurable-PMU patch. Copy [fake_core_id_daemon.rs](fake_core_id_daemon.rs)
into `staxd/examples/` in that checkout. Build `stax-server` and the
`fake_core_id_daemon` example with the pinned lockfile and full Xcode selected,
then sign both ad hoc. The fake daemon accepts fixed-counter class mask 1 and
alternates invented kdebug CPU IDs 0 and 4. Start it and the patched server
on sockets inside a private `0700` directory, then use the `stax record` and
`stax save` commands above with the fake daemon socket and this Metal app.
The patch uses zero context to keep the checked-in diff whitespace-clean;
apply it with `git apply --unidiff-zero` only at the pinned revision.
The [M1 control](../../docs/stax-m1-core-identity-control.json) archived 348
invented CPU samples and 299 real Metal target spans. The topdown importer
retained the raw IDs but did not infer P/E core type or PMU scope. This is a
software-path fixture, not a hardware-counter validation.

## Cumulative KPC software-path control

The [cumulative fixture](fake_cumulative_daemon.rs) emits invented per-thread
KPC snapshots that rise at each PET tick. Apply the
[core-identity patch](../stax-m1-core-identity.patch), then the
[per-thread delta patch](../stax-m1-kpc-delta.patch), to a clean checkout of the
pinned Stax revision using `git apply --unidiff-zero` for each. Build and ad hoc
sign the patched `stax-server` and the `fake_cumulative_daemon` example as in
the raw CPU identity control. Use the private-socket `stax record` and
`stax save` commands above with the Metal app.

The [M1 control](../../docs/stax-m1-kpc-accumulation-control.json) compares a
core-ID-only server against the server with per-thread differencing. Both
archived 348 invented PET/PMC samples and 299 real Metal spans. The patch
rebases on the first sample, a counter decrease, vector shape change, or an
all-zero KPC error vector. XNU emits an all-zero vector when a thread counter
buffer is not ready; treating that as a baseline would inflate the next
sample. The test proves the software path, not hardware counter validity.

## Synthetic CPU/PMU software-path control

The separate [fake sample daemon](fake_sample_daemon.rs) verifies the software
path through the experimental M1 PMU patch, Stax's parser and archive, and the
optional collector merged from PR #47. It rejects any session configuration other than
class mask `3` with the M1 config words `[163, 0, 0, 203, 0, 0, 0, 0]`. It
then emits an invented user-stack sample every 100 ms with cycles `100`,
instructions `200`, L1D misses `13` in slot 2, and branch mispredicts `17` in
slot 5. **These are fixture values, not measured counters or real CPU stacks.**

Copy the source to the pinned Stax checkout's `staxd/examples/` directory and
build it with full Xcode selected:

```sh
cp examples/stax_m1_metal_lane/fake_sample_daemon.rs \
  /private/tmp/stax-m1-pilot/staxd/examples/fake_sample_daemon.rs
cd /private/tmp/stax-m1-pilot
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  CARGO_BUILD_JOBS=2 CARGO_PROFILE_RELEASE_DEBUG=0 \
  cargo build --release --locked -p staxd --example fake_sample_daemon
codesign --force --sign - target/release/examples/fake_sample_daemon
```

Start this fake daemon on a private socket, run the opt-in patched Stax server
with `STAX_EXPERIMENTAL_APPLE_PMU=1`, and attach Stax to the Metal app. The
[captured M1 control](../../docs/stax-m1-synthetic-pmu-control.json) used the
PR #47 adapter with only its root-owned helper-socket preflight bypassed in
the test process. It stored 78 invented CPU samples and all 299 real GPU
spans; every stored CPU sample retained the four expected fixture values.
The pinned CLI's `compare` counted 377 samples, while the proposed corrected
CLI counted 78 CPU samples and 299 target spans.

## Rootless CPU origin-link control

For an opt-in software-path check, set `STAX_M1_ORIGIN_TID_FILE` to a writable
file in a private directory for both the Metal app and `fake_sample_daemon`.
The app writes its `pthread_threadid_np` value before creating the command
queue. The fake daemon reads that file for each invented PET/PMC record and
samples every 20 ms in this mode; without the variable, it keeps the previous
fixed thread ID and 100 ms cadence. The file may not exist at the first sample,
so early records can use the fixture ID `42`.

The [M1 origin control](../../docs/stax-m1-origin-link-control.json) used this
mode with a real M1 Metal app, the experimental Stax server, and PR #47's
capture adapter. It completed 300 checksum-verified command buffers, retained
299 target spans, and linked all 299 to the fake CPU stack on the app's actual
submitting thread. Import into SQLite kept each span's origin thread ID.
This checks Stax's thread/time join and archive path. The CPU stack and PMU
values are invented; it does not establish real kperf sampling, meaningful
CPU attribution, hardware counters, or GPU utilization. The adapter's
root-owned helper-socket preflight was bypassed only inside the rootless
test process, with no privileged helper running.

The [cadence control](../../docs/stax-m1-origin-cadence-control.json) repeated
this rootless M1 capture twice at each of three invented sample periods. The
requested Stax frequency remained 99 Hz throughout. At 20 ms, 299 and 298
of 299 spans linked; at 50 ms, 287 and 286 linked; at 100 ms, 147 linked in
both runs. Stax uses a 50 ms maximum origin-to-stack distance. The 50/100 ms
controls were built from a copied fake daemon source with its sleep changed
to that period. These are single-workload software controls, not measured
hardware sampling rates or a universal link-completeness curve.
