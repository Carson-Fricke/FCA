# Workspace repositories and programs

This package owns portable workspace identity, isolated Git repositories,
catalog projections, and committed Rust program operations. It depends on the
repository provider, fenced Git publication, and an injected harness tool port;
it does not own agent turns, provider projection, or workflow transitions.

- `__init__.py` exposes workspace identity, repository, and catalog contracts,
  plus `WorkspacePrograms` and its artifact/publication errors.
- `azure_catalog.py` provisions the Azure blob catalog and operational scopes,
  using the injected repository provider when configured, and retains workspace
  deletion markers covering all storage owned by the workspace ID.
- `azure_contract.py` defines and serializes the Azure workspace resource locator.
- `catalog.py` derives local workspace summaries from manifests and persists
  catalog activity and atomic workspace deletion markers over the local provider; no ownership migration read path.
- `gitea.py` provisions private ownership-marked remote repositories and manages
  isolated rebuildable native-Git checkouts and publication.
- `local.py` creates and verifies local Git workspaces, scaffolds repository
  directories, confines paths, and resolves the configured workspace root.
- `manifest.py` defines the strict portable manifest and strategy-family charter,
  rejects secret material, and loads, hashes, and writes manifest bytes.
- `programs.py` owns committed study/strategy/indicator authoring, targeted
  updates, bounded reads, ID listing, harness validation receipts, ID-based
  execution, pinned dependency snapshots, and matching SDK references/templates.
  Canonical SDK crates are siblings of the Thesis package in both source and
  wheel layouts; discovery never searches for a repository checkout.
  Strategies declare tunable parameters in the committed manifest; single runs
  and bounded candidate/null/variation batches validate assignments against the
  declaration, and the workflow-scoped data-window policy gates, or for the
  forward holdout rewrites, the committed window before submission.
- `sketch_contract.py` defines closed strategy-sketch contracts and validates
  representation and assumption dependencies without depending on the runtime.
- `sketches.py` stores immutable sketch revisions in Git and CAS-fences edits.
- `exploration.py` projects bounded pages from authoritative job output artifacts.
- `repository.py` defines the structural create/open/resolve/publish/locator port
  shared by local and Gitea providers, and the fenced Git port shared by programs
  and sketches.

Programs live under `studies/<program_id>/`, `strategies/<program_id>/`, or
`indicators/<program_id>/` with `program.rs`, `manifest.json`, and, after validation,
`validation.json`. Workspace reads, validation, and runs use latest committed
contents, never dirty working-tree source. A study read with `job_id` instead reads
the exact immutable accepted harness source, with bounded source ranges and bundle
and source hashes, so later workspace edits cannot change the evidence under review.
Updates require the current content
version and an exact single-match source patch, or a configuration-only change.
Versions cover source and manifest
digests including transitive indicator dependencies; receipt-only commits do not
change the version. Pinned loads resolve all dependencies at the selected reachable
commit. Indicators validate through a study wrapper and cannot execute alone.

Validation compiles through the injected harness and commits an exact-content/SDK
receipt after rechecking for concurrent changes. Runs require accepted validation
matching current content, serving SDK, and compiler identity. Publication commits only the intended
files through a temporary index and refuses staged or dirty changes in those files.
Remote publication failures retain the local commit receipt for explicit
reconciliation, not re-authoring or automatic execution replay.

`read_sdk_reference` supplies bounded source/template ranges, including the
`research_views`, `research_publishing`, and `research_recipes` topics for
saved exhibits, typed evidence and reusable compositions, alongside the
`decimal` topic from the `num` kernel, and `template=basic`
renders exact declared subscriptions. Matching local SDK sources are required;
remote source topics are read from the serving harness after matching its identity.
Wheels and source distributions ship
the canonical `harness/strategy-sdk`, `harness/contracts`, `harness/num`, and
`lake/contracts` Cargo manifests and Rust
sources with portable relative dependencies. Version/ABI metadata is read from
the SDK and checked against the packaged copy; SDK identity hashes all four
crates' manifests and source files. The shared templates in `programs.py` back
reference reads and basic authoring. They are minimal SDK contracts, not research
models with substantive falsifiers or fitted outputs.

Workflow completion uses the public
snapshot read port to verify artifact references without compiling or repairing
source. Agent repair guidance belongs to workflow prompts, not this service.

Focused coverage lives in `tests/thesis/workspaces/test_programs.py` (committed
content, dependency/SDK invalidation, templates, and wheel assets),
`tests/thesis/extensions/tools/test_program_tools.py` (trusted ID-only bindings), and
`tests/thesis/extensions/workflows/hypothesis_development/test_program_references.py`
(accepted references). Fixture helpers and fake receipts are test scaffolding,
not a second production program or validation contract.
