# thesis_runtime/adapters/providers

Native provider clients and pure neutral-to-native live/replay projection. Native protocol shapes do not become runtime session types. Application prompts and retrieval policy stay in FCA composition.

- `__init__.py`: Backend-only model provider profiles and capability preflight.
- `foundry.py`: Credential-injected Azure AI Foundry capability preflight.
- `families/`: Pure model-family tool presentation and decoding, selected by application composition; DeepSeek exposes object parameter types at the schema root while retaining branch validation.
- `invocations.py`: Immutable invocation records, typed provider errors, and role bindings.
- `openrouter.py`: Streamed OpenRouter completions with establishment-only retries; synchronous callers collect the same stream.
- `session_adapter.py`: Shared live/replay projection, family/version request auditing, and upstream provider/generation response metadata. Sealed steering/provider-failure partials project received reasoning/text as ordinary assistant evidence in the next request. Failed large outputs use explicitly labeled bounded excerpts with canonical message references; full evidence remains durable.
- `streaming.py`: Strict SSE payload assembly, readable reasoning normalization, private tool-argument accumulation, usage, actual finish reasons and terminal validation. Empty/truncated completions preserve provider identity and token diagnostics for automatic continuation. Numeric error statuses and premature EOF classify recoverable failures without exposing upstream error prose.
- `transports.py`: Concrete local transports for model providers (network lives here), including numeric/date Retry-After parsing and permanent local-protocol rejection.
