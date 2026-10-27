# thesis_runtime/adapters/providers/families

Pure model-family adapters selected once by application composition. They
prepare provider-facing messages/schemas and decode calls; they do not execute tools,
repair arbitrary JSON strings, perform I/O, or create session delivery paths.
Canonical tool validation remains authoritative.

- `__init__.py`: Family namespace selection and public adapter contract. Add a
  family's implementation to the small selection table to enable it.
- `base.py`: Shared native message/tool preparation and the family adapter protocol.
- `deepseek.py`: Exposes common union parameter types at the schema root while
  retaining the original branch constraints, tool names, and argument shapes.
  Presents the nonblank substring pattern `\S` as `^[\s\S]*\S[\s\S]*$` in nested
  schemas so endpoints that constrain whole strings can generate prose. This
  preserves accepted values, including multiline text, and leaves other regexes
  and literal data in defaults, examples, enums and constants unchanged.
  Ensures assistant messages without captured reasoning carry the required empty
  `reasoning_content` field; captured continuation is never replaced.

The session adapter uses the selected family for both audited preparation and
live calls. History and explicit call/result correlations retain the shared pure
projection. DeepSeek endpoint behavior can vary; this conservative presentation
is used across that family, with actual upstream identity retained as diagnostics.
