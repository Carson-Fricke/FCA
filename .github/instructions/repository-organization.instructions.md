---
applyTo: "src/**,tests/**,strategies/**"
---

# Repository Organization

Organize code by product and capability before organizing it by
technical layer. Preserve the product roots under `src/lake` and `src/harness`;
do not create a generic shared package without at least two real consumers and
a stable contract.

Extend the existing documented structure consistently rather than introducing
a parallel taxonomy. Reading the architecture documents is optional reference,
not a required orientation step.

## Implementation Phases

Complete these phases in order before extending a capability. Proceed
autonomously; phases are implementation obligations, not approval checkpoints.
Scale the analysis to the change and follow the existing verification contract.

1. **Understand behavior.** Establish current behavior, the requested change,
   preserved invariants, and observable acceptance criteria.
2. **Trace implementation and blast radius.** Read the actual execution path,
   complete affected functions/classes, shared state and effects, callers,
   contracts, composition, persistence, registrations, tests and documentation.
   Verify documentation against source; resolve material uncertainty before
   changing the affected implementation.
3. **Assign ownership.** Identify internal responsibility groups, their state,
   invariants, dependencies and independent reasons to change. Assign the new
   behavior to a coherent owner before editing. Briefly state ownership and
   dependency direction for structural changes. Preserve atomicity, ordering,
   fencing and canonical writers. A large coherent coordinator can remain intact.
4. **Implement through those boundaries.** Extract misplaced responsibilities
   needed by the change, moving their policy, state and dependencies together.
   Do not merely move methods while passing the original service or a broad
   context object. Policy consumes narrow capabilities; application composition
   supplies concrete adapters. Prefer functions/modules where sufficient.
   Each new interface needs a current consumer and a real boundary; a useful
   port may have one implementation. Update current producers/consumers together.
5. **Close removals and documentation.** Trace superseded behavior through
   interfaces, implementations, factories, registrations, exports, configuration,
   callers and tests. Remove abandoned elements within scope. Update local
   responsibility contracts, folder READMEs and affected architecture documents.
   Broadening a responsibility contract requires an architectural justification.

Responsibility analysis includes cohesion *inside* modules and classes, not only
imports between packages. Review whole affected modules against their contracts;
require concrete competing policies and change coupling for a finding. File
length, class/interface counts, comments and smaller files are not proof of good
design. Static architecture tests enforce explicit boundaries; advisory call,
state and dependency maps support semantic review rather than deciding it.
Do not expand unrelated cleanup or duplicate the existing verification process.

## Canonical Commands

Run verification commands through `uv` from the repository root. Do not call
`.venv\Scripts\python.exe`, `.venv/bin/python`, or any other
virtual-environment interpreter directly. Use the command table in
`.github/instructions/copilot-instructions.md` for environment, lint, format,
type, test, build, and entrypoint checks.

Use `ruff` for lint and format and `ty` for type checks. Do not substitute
`mypy`, `pyright`, `flake8`, `pylint`, `black`, or another lint/type/format
tool.

## Package Shape

- Put related domain records, application behavior, ports, and adapters in a
  named capability package such as `catalog`, `ingestion`, `query`, `execution`,
  `portfolio`, or `reporting`.
- Split a module when it begins combining unrelated capabilities or multiple
  independent concerns such as records, ports, provider adapters, controllers,
  and process construction.
- Keep deployment and process entrypoints thin. CLI commands, HTTP routes, and
  runtime launchers should compose application services rather than own domain
  behavior.
- Expose intentional public imports through package `__init__.py` facades.
  Internal modules should use explicit capability paths instead of depending on
  compatibility facades.
- Prefer a new descriptive subpackage over another unrelated module at a
  product root. Before adding a third related root-level module, establish the
  capability folder and move the related code in the same task when practical.
- Keep provider SDKs, Azure clients, FastAPI, storage engines, and other
  infrastructure dependencies at adapter boundaries. Domain modules must not
  import HTTP, CLI, or deployment code.

## Tests And Planning

- Mirror source capability paths under the product test root, for example
  `src/harness/execution/orders.py` maps to
  `tests/harness/execution/test_orders.py`.
- Keep cross-product contract tests at an explicit integration boundary rather
  than mixing them into one product's unit-test package.
- Changes that add several modules must reserve capability directories,
  not a list of flat product-root files. Include package metadata, entrypoints,
  documentation, and mirrored test paths in the owning task when they change.
- Historical verification records remain immutable when files move. Update
  active imports, workflows, runbooks, and architecture documentation instead.

When an existing design system or package boundary already expresses these
rules, extend it consistently rather than introducing a parallel taxonomy.
