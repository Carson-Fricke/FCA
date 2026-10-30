# research/tests

Behavior fixtures and tests for the owning capability. No production implementation belongs here.

- `parameters.rs`: Real authored constructor and callback receive configured
  integer, fractional, nested and high-precision numeric parameters.

- `canonical_trades.rs`: Canonical `metrics.json` trade and equity publication from the job ledger.
- `managed_execution.rs`: Compiled strategy context and managed orders reconcile
  long/short partial fills, fees and entry-notional returns to canonical artifacts.
- `prepared_replay.rs`: Cold/warm immutable block replay matches owned-event
  execution, including partial fills and flips. Covers changed source pins,
  corruption rejection, retention bounds, interruption and concurrent readers.

- `capital_fraction.rs`: A strategy instance's starting cash comes from its configured capital

- `job_id_contract.rs`: Authored program or behavior fixture for job id contract.

- `lake_parallel.rs`: Multi-symbol acquisition and streamed merge ordering,
  including equal-time ties at single, odd and maximum cursor counts.

- `mixed.rs`: Loaded ABI declarations and exact mixed-input dispatch without engine filters.

- `null_transform.rs`: Signal-destruction null transform behavior and integration.

- `options.rs`: Research acquisition resolves each option once and retains contract attribution.

- `options_acknowledgement.rs`: Resolver acknowledgements must match the requested input before acquisition.

- `study_frames.rs`: Atomic study assembly and dispatch, without changing tick replay.

- `causal_observations.rs`: Real JIT study/strategy replay verifies prefix and future-perturbation invariance, immutable features, delayed and missing labels, and rejected host-output forgeries.
