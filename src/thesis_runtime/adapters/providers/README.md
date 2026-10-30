# thesis_runtime/adapters/providers

Native provider clients and pure neutral-to-native live/replay projection. Native protocol shapes do not become runtime session types. Application prompts and retrieval policy stay in FCA composition.

- `__init__.py`: Backend-only model provider profiles and capability preflight.
- `foundry.py`: Credential-injected Azure AI Foundry capability preflight and deployment invocation, including explicit per-request output and tool-selection options.
- `families/`: Pure model-family tool presentation and decoding, selected by application composition; DeepSeek exposes root object parameter types and equivalent whole-string nonblank patterns while retaining canonical validation.
- `invocations.py`: The single native `ProviderMessage` value and its wire encoder; `ModelCompletion` wraps that message with invocation attribution and accounting. Also owns immutable invocation records, typed provider errors, and role bindings.
- `openrouter.py`: Streamed OpenRouter completions with establishment-only retries; synchronous callers collect the same stream. An explicit output cap matches the serving context reserve.
- `session_adapter.py`: Shared live/replay projection and request preparation through the transport's encoder. Transcript input mode encodes canonical history as data for checkpoint requests, with per-request output and tool-selection options shared by audit and transport. Canonical results retain opaque provider continuation with provider/model origin separately from display reasoning. Composition binds replay to its configured provider/model. Sealed steering/provider-failure partials remain ordinary evidence without executable continuation. Interrupted reasoning and partial answer text replay completely; context reduction belongs to the checkpoint lifecycle. A late result whose request predates the active window remains neutral incoming evidence with its original correlation.
- `jev.py`: Optional TypeSafe Jev ranking of exact evidence groups, with bounded advisory excerpts and a caller-owned durable audit before every request. Ranking never writes summaries or filters the working model's source. Credentials stay process-local.

- `reasoning.py`: Lossless continuation copying/freezing and separate readable text projection. Stream assembly merges consecutive compatible text/summary fragments; encrypted and unknown blocks remain separate and ordered, even with repeated IDs. Signatures and empty fields survive replay; stream indices are removed from completed blocks.
- `streaming.py`: Strict SSE payload assembly, readable reasoning normalization, private tool-argument accumulation, usage, actual finish reasons and terminal validation. Empty/truncated completions preserve provider identity and token diagnostics for automatic continuation. Numeric error statuses and premature EOF classify recoverable failures without exposing upstream error prose.
- `transports.py`: Concrete local transports for model providers (network lives here), including numeric/date Retry-After parsing and permanent local-protocol rejection.

Native conversation projection omits a tool receipt's arguments only when they
exactly duplicate its explicitly correlated native call. Canonical receipts and
checkpoint transcript input retain the full evidence; unmatched/partial calls
keep their arguments. Live execution and replay share this pure projection.
