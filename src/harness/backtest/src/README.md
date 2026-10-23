# Native backtest kernel

This crate owns deterministic replay, numerical results and runtime accounting.
It receives market-data capabilities and strategy runtimes; API admission,
provider acquisition, deployment and authored ABI loading belong elsewhere.

- `lib.rs`: exports the kernel capabilities.
- `configuration_identity.rs`: hashes canonical accepted configurations.
- `backtest/`: plans and runs replay and composes exact results.
- `results/`: computes statistics, bounded reports and typed result encoding.
- `indicators/`: owns native indicator kernels.
- `signal/`: owns pure signal transforms and numerical kernels.
