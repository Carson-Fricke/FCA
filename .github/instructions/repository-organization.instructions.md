---
applyTo: "src/**/*.py,tests/**/*.py,strategies/**/*.py"
---

# Repository Organization

Organize Python code by product and capability before organizing it by
technical layer. Preserve the product roots under `src/lake` and `src/harness`;
do not create a generic shared package without at least two real consumers and
a stable contract.

Extend the existing documented structure consistently rather than introducing
a parallel taxonomy. Reading the architecture documents is optional reference,
not a required orientation step.

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
