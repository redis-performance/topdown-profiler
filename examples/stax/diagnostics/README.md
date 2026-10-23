# Isolated Linux latency controls

These patches reproduce source ablations from
[the VM latency follow-up](../../../docs/stax-vm-latency-followup.md).
Apply **one patch at a time** to a disposable Stax checkout at
`aedd85e08894fc3250ec975a486544d091e11281`, build `stax-server` with
`cargo build --release --locked -p stax-server`, and retain its binary hash.
Keep the pinned binary separately and restore the source between controls.

These are diagnostic controls, not production fixes. They change the data
collected: one removes sampled user stacks, another removes CPU sampling,
another removes scheduler rings, and the main-thread prototype omits other
threads and cannot decode its task-scoped switch records. No control established
a reliable latency remedy. Hardware PMU behavior remains unverified.

The profiler does not apply these patches or select their binaries automatically.
