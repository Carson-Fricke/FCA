# research/tests

Behavior fixtures and tests for the owning capability. No production implementation belongs here.

- `canonical_trades.rs`: Canonical `metrics.json` trade and equity publication from the job ledger.

- `capital_fraction.rs`: A strategy instance's starting cash comes from its configured capital

- `job_id_contract.rs`: Authored program or behavior fixture for job id contract.

- `lake_parallel.rs`: Multi-symbol lake acquisition for a study window.

- `mixed.rs`: Loaded ABI declarations and exact mixed-input dispatch without engine filters.

- `null_transform.rs`: Signal-destruction null transform behavior and integration.

- `options.rs`: Research acquisition resolves each option once and retains contract attribution.

- `options_acknowledgement.rs`: Resolver acknowledgements must match the requested input before acquisition.

- `study_frames.rs`: Atomic study assembly and dispatch, without changing tick replay.
