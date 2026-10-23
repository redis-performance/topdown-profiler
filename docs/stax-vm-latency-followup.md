# Stax VM latency follow-up

The longer pipeline-one experiment still exceeds the declared latency budget.
With five alternating baseline/profile pairs and 30-second measurement windows,
pinned Stax lost 0.34% median paired throughput while PING p99 increased
1,434.11%. A diagnostic main-thread-only perf prototype reduced the median
increase to 80.43%, with 0.20% throughput loss, but also failed the 20% latency
budget and lost thread-complete/off-CPU data. No source workaround is adopted.

This extends the [initial VM study](stax-vm-pilot.md), using the same Redis 8.2.3
workload, VM and pinned Stax revision. Exact paired values, binary/source hashes,
window checks and timing diagnostics are in
[stax-vm-latency-followup.json](stax-vm-latency-followup.json).

## Controlled changes

Each source control was built separately against
`aedd85e08894fc3250ec975a486544d091e11281`. The unmodified binary was preserved,
and the source was restored after each build. The VM ran one capture at a time;
the longer measurements ran without competing tests or builds.

| Control | Measurement window | Median paired throughput loss | Median paired PING p99 change |
|---|---:|---:|---:|
| Context-switch sideband retained, per-switch stack samples disabled | 3 s | 0.53% | +1218.57% |
| Sampled user callchains excluded | 3 s | 0.44% | +502.02% |
| CPU-clock sampling replaced with a dummy event, quiet repeat | 3 s | 0.02% | +656.32% |
| Context-switch rings disabled | 3 s | 0.39% | +1135.29% |
| Main-thread-only perf prototype | 3 s | 0.27% | +13.10% |
| Main-thread-only prototype repeat | 3 s | 0.17% | +6.73% |
| Pinned Stax, timestamped probe runner | 3 s | 0.41% | +74.73% |
| Pinned Stax | 30 s | 0.34% | +1434.11% |
| Main-thread-only perf prototype | 30 s | 0.20% | +80.43% |

The two short prototype series passed the previously declared median budget,
but their paired changes ranged from -15.73% to +337.88%, and from -93.95% to
+977.93%. The second series also had very large baseline p99 variation. Those
short passes do not establish a reliable latency remedy.

In the longer pinned series, median baseline/profile PING p99 was
0.065459/1.004212 ms. In the longer prototype series it was
0.064792/0.116751 ms. Baseline throughput ranges were 0.40% and 0.45%; baseline
p99 ranges were 12.22% and 5.19%. Percent-change medians are computed per pair,
so they differ from ratios of independent median absolute values.

One earlier CPU-clock-disabled series overlapped a macOS regression run and had
10.35% baseline throughput variation, beyond the declared 10% limit. It is
excluded in full and replaced by the quiet repeat. The original study's separate
discarded overlap remains excluded as described there.

## What the controls establish

The native Linux backend normally opens per-CPU system-wide events and filters
target threads in userspace. The main-thread prototype changes the event opener
to the Redis leader PID. It does not inherit/enumerate other threads, and the
existing decoder does not consume its task-scoped switch records. Its zero wait
count therefore reflects missing collection capability. Hardware PMU behavior
in this mode was not validated; the guest exposes no hardware PMU device.

The new pilot output retains individual PING monotonic start/end timestamps and
RTT. The 30-second profile windows contained roughly 2,900 accepted probes each.
For the pinned run, many of the slowest probes had zero overlap with an archived
Redis-main-thread off-CPU interval, and only some overlapped a CPU sample.
These observations do not identify a kernel cause, client delay or hypervisor
descheduling. No utilization or workload wait percentage is inferred from them.

Stored Stax summaries now report observed/reported off-CPU counts, their
agreement and whether any off-CPU interval was observed. Wait reports derive
these fields for older documents too, without requiring source artifacts.
An empty wait report explicitly does not establish that the workload never
blocked. Missing latency evidence makes a pilot inconclusive; baseline p99
variation is reported without retroactively changing the declared budget.

## Reproduce and review

Run the [pilot runner](../examples/stax/redis_pilot.py) with `--duration 30`,
`--pipeline 1`, `--frequency 99` and `--repeats 5` into a new output directory.
Retain the complete `pilot.json`, archives, logs and database. Use a quiet host
and run the series sequentially. Source controls and their limitations are in
[the diagnostic patch directory](../examples/stax/diagnostics/README.md).

No CI latency threshold is enabled. The optional adapter still uses the pinned,
unmodified Stax collection path. These results support keeping capture
experimental and measuring overhead on the actual workload.

Live MCP cancellation controls also passed over stdio and streamable HTTP with
MCP 1.30.0 and real pinned Linux Stax binaries: queued captures did not start,
active cancellation retained a partial archive and reaped owned children,
borrowed targets survived, no run was stored, and the server stayed responsive.
The stdio notification path is also covered by a repository regression test.
