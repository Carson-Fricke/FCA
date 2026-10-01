# Workspace strategy artifacts

This package owns immutable Rust strategy designs, their canonical digests,
workspace authoring, and the published-only Strategies UI projection. It does
not own HTTP routes, workflow tools, harness compilation, or Cosmos storage.

- `__init__.py` exposes the public strategy contracts and authoring service.
- `bundles.py` freezes source, tests, and configuration into a canonical,
  content-addressed `StrategyBundle` and rejects unsafe or overlapping paths.
- `contracts.py` parses the closed design mapping with one `src/lib.rs`
  entrypoint and Rust test files under `tests/`.
- `validation.py` preserves the `StrategySandboxValidator` injection seam.
  `StructuralStrategyValidator` checks source structure without compiling or
  executing Rust and makes no sandbox-isolation claim.
- `artifacts.py` provides `WorkspaceStrategyArtifacts.author`, `list`, and
  `verify_completion`, bound to `WorkspaceRepositoryProvider`.

Accepted files live at `strategies/<safe-slug>/<version>/`, with `manifest.json`
as the artifact source of truth. The canonical bundle digest includes tests and
design metadata; `sourceSha256` is separately provided for the current harness
`bundle_hash` single-source contract.

The service journals accepted bytes in `.git/thesis-strategies.sqlite3` before
publication. A process lock serializes authoring, and `GitCoordinator` commits
exact accepted bytes through a temporary index under a HEAD fence. Provider
publication follows the commit and precedes a durable success receipt. A retry
recovers the same accepted commit and retries provider publication. Projection
and completion verification use tracked manifests, reachable verified Git bytes,
and matching confined workspace files, never the checkout-local journal. The
manifest embeds deterministic publication identity before acceptance. A separate
tracked `publication.json` marker identifies the original artifact commit and
attests that provider publication of that commit succeeded. Marker creation
follows that success, and the marker is then published in a second commit. An
initial provider failure produces no marker and remains invisible. If publishing
the marker fails, the artifact commit was already accepted by the provider;
the local marker remains an attestation of that success and a retry publishes it.
Clones reproduce the original completion receipt without any local journal.
The journal is a retry outbox, not the artifact source of truth.
Each version also tracks `.gitattributes` with `* -text` so checkout line-ending
conversion cannot change accepted artifact bytes on a fresh Windows clone.
Retries restore missing accepted artifact and marker files through the public
`GitCoordinator.recover_artifacts` method. Recovery verifies reachable commit
bytes under the process fence and never replaces differing files or symlinks.
Git subprocesses discard ambient Git repository, index, credential, and global
configuration overrides, then apply controlled publication identity and indexes.
Version projection follows oldest-first reachable Git topology rather than
timestamps or lexical version text. A declared `parentVersion` must be a
published, verified version of the same strategy in the selected workspace;
self-references and slug collisions with another strategy are rejected.

Structural validation examines text markers and regex patterns only. It cannot
prove Rust syntax, successful test execution, or filesystem, network, process,
or memory isolation. JIT compilation itself is not a security sandbox either.
