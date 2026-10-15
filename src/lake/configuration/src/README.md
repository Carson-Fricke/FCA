# Native configuration

Strict configuration parsing and typed values. Consumers compose these values;
this slice performs no provider or job execution.

- `lib.rs` exports the facade and resolves the market-lake configuration root.
- `env.rs` reads explicitly supplied environment overrides.
- `error.rs` defines configuration failures.
- `settings.rs` parses and validates current settings, rejecting unknown fields.
- `universes.rs` validates provider-independent universe selections.
- `yaml.rs` parses the supported YAML mapping subset.
