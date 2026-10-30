# Program authoring SDK

Owns packaged/serving SDK agreement, reference reads and minimal source templates.
It depends on the program value contracts and an injected metadata/source port,
without workspace repositories, Git, submissions or workflow control. Application
composition supplies the existing MCP client/provider as that source.

- `__init__.py` exposes SDK capabilities and canonical source hashing.
- `ports.py` defines the lifecycle consumer's `AuthoringSdk` capability and the
  narrower `SdkReferenceSource` transport capability.
- `identity.py` discovers the canonical packaged sources and reads their complete
  four-crate inventory, version, ABI and newline-normalized source hashes.
- `references.py` implements identity fencing, serving response validation,
  local/remote source reads, discovered source-path selection and matched template
  rendering. Friendly aliases come from the single SDK-owned
  `strategy-sdk/reference-topics.json` manifest; `index` lists available references.
- `reading.py` selects explicit LF line ranges and returns whole source or exact
  token continuations; committed program reads reuse this stateless operation.
- `templates.py` owns the minimal study, strategy and indicator source assets.

The SDK owner receives only its source port and local SDK root. It is never
passed `WorkspacePrograms`, its repository or a broad composition context.
