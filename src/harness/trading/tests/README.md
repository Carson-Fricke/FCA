# trading/tests

Behavior fixtures and tests for the owning capability. No production implementation belongs here.

- `accounting_failure.rs`: Failed accounting must leave the public book unchanged, including fill identity.

- `notional_limits.rs`: Exact-decimal risk allowances must remain within the cap after rounding.
