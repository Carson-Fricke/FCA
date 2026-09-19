<!-- CAAMP:START -->
@~/.agents/AGENTS.md
@.cleo/project-context.json
<!-- CAAMP:END -->

@.github/instructions/cleo.instructions.md
@.github/instructions/copilot-instructions.md
@.github/instructions/repository-organization.instructions.md
@.github/instructions/source-documentation.instructions.md
@.github/instructions/market-lake-local.instructions.md
@.github/instructions/thesis-workflow-development.instructions.md

## FCA Execution Policy

- Never assign the same file to concurrent agents. Treat shared entrypoints,
  provider clients, models, project metadata, and workflows as
  integration-owned unless ownership is explicitly partitioned.
- Subagents run focused checks only. The coordinator owns repository-wide
  verification and runs it once after edits freeze.
- Permit one additional full verification run only after fixing a failure from
  the first integration run. If a suite hangs or times out, isolate the test
  with a hard timeout instead of rerunning the unchanged suite.
- Do not start work beyond the scope the user approved.
- Do not rewrite historical verification records solely because later tasks
  change the repository-wide test count.
- Use `ruff` for all lint and format checks and `ty` for all type checks. Do not
  use MyPy, pyright, flake8, pylint, black, or another lint/type/format tool.
- Run Python verification through `uv` from the repository root using the
  canonical commands in `.github/instructions/copilot-instructions.md`. Never
  invoke `.venv` interpreters directly.
- Treat `.github/architecture/*.md` as the source of truth for repository
  structure, package boundaries, and practices. Update the affected
  architecture document whenever a task changes package layout, public
  facades, entrypoints, dependency direction, or deployment shape.
- Treat architecture-documentation updates as integration-owned unless a task
  explicitly assigns that file.
- Explore cheaply. Check BRAIN first with `cleo memory find "<topic>"` for
  prior knowledge, then read files directly.
- CLEO is a task tracker and BRAIN is a lookup tool. Do not use CLEO
  orchestration, spawn prompts, or worktree machinery to drive implementation;
  implement with ordinary agents and keep CLEO task state updated.
- Create tasks atomically. Decompose epics with `cleo workgraph validate
  --dry-run` followed by `cleo workgraph apply`; for plain multi-task creation
  use `cleo add-batch` with `--dry-run` first, in chunks of at most 20 tasks
  per transaction. Never loop single `cleo add` calls to create more than two
  tasks.
- Assemble every evidence atom before the first `cleo verify` call, then make
  one `cleo verify <id> --all` invocation. Iterative per-gate verify attempts
  are an anti-pattern; each failed gate forces evidence recapture and
  staleness re-verification.
- NEVER co-locate Rust tests inside a `src/**/*.rs` file. Every `#[cfg(test)]`
  test module lives under the package's own `tests/` directory — a peer of
  `src/` and `Cargo.toml` inside that crate. Do not add `#[cfg(test)] mod tests`
  to a source file. See `.github/instructions/rust.instructions.md` for the
  integration-vs-unit placement rule.
- NEVER complete multiple tasks with individual `cleo complete` invocations.
  When more than one task must be closed, bulk-complete the set in one
  transaction with `scripts/cleo-bulk-complete.ps1 -ManifestPath <manifest.json>`
  (one `sqlite3` process, one commit). Reserve single `cleo complete` for a lone
  task. Per-task completion costs O(N x process startup) and is a known
  anti-pattern.
- NEVER use workflow gates, gate tools, or gate events (`workflow.gate.*`). All
  workflow gates, gate resolution schemas, and gate cards are permanently
  removed and prohibited. Operator interaction is conversational and
  agent-first: a phase agent asks from inside its own node with the wrapped
  `request_workflow_input` tool (orange request box on that agent, answer
  accepted for that agent), and advances only by calling
  `request_phase_completion` after the operator accepts. The engine never
  authors, relays, or pauses for operator input, and nothing percolates to the
  parent conversation. The operator always responds via standard chat input.
  See `.github/instructions/thesis-workflow-development.instructions.md`.

