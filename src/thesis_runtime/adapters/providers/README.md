# thesis_runtime/adapters/providers

Native provider clients and pure neutral-to-native live/replay projection. Native protocol shapes do not become runtime session types. Application prompts and retrieval policy stay in FCA composition.

- `__init__.py`: Backend-only model provider profiles and capability preflight.
- `foundry.py`: Credential-injected Azure AI Foundry capability preflight.
- `families/`: Pure model-family tool presentation and decoding, selected by application composition; DeepSeek exposes root object parameter types and equivalent whole-string nonblank patterns while retaining canonical validation.
- `invocations.py`: The single native `ProviderMessage` value and its wire encoder; `ModelCompletion` wraps that message with invocation attribution and accounting. Also owns immutable invocation records, typed provider errors, and role bindings.
- `openrouter.py`: Streamed OpenRouter completions with establishment-only retries; synchronous callers collect the same stream.
- `session_adapter.py`: Shared live/replay projection and request preparation through the transport's encoder. Canonical results retain opaque provider continuation with provider/model origin separately from display reasoning. Composition binds replay to its configured provider/model. Sealed steering/provider-failure partials remain ordinary evidence without executable continuation. Failed large outputs use explicitly labeled bounded excerpts with canonical message references; full evidence remains durable.
- `reasoning.py`: Lossless continuation copying/freezing and separate readable text projection. Stream assembly merges consecutive compatible text/summary fragments; encrypted and unknown blocks remain separate and ordered, even with repeated IDs. Signatures and empty fields survive replay; stream indices are removed from completed blocks.
- `streaming.py`: Strict SSE payload assembly, readable reasoning normalization, private tool-argument accumulation, usage, actual finish reasons and terminal validation. Empty/truncated completions preserve provider identity and token diagnostics for automatic continuation. Numeric error statuses and premature EOF classify recoverable failures without exposing upstream error prose.
- `transports.py`: Concrete local transports for model providers (network lives here), including numeric/date Retry-After parsing and permanent local-protocol rejection.
