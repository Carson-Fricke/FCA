<!-- CAAMP:START -->
@~/.agents/AGENTS.md
@.cleo/project-context.json
@.cleo/memory-bridge.md
<!-- CAAMP:END -->

@.github/instructions/cleo.instructions.md
@.github/instructions/copilot-instructions.md
@.github/instructions/repository-organization.instructions.md
@.github/instructions/source-documentation.instructions.md
@.github/instructions/market-lake-local.instructions.md

## FCA Execution Policy

- Define the task IDs and exclusive file ownership for a wave before fan-out.
- Never assign the same file to concurrent agents. Treat shared entrypoints,
  provider clients, models, project metadata, and workflows as
  integration-owned unless ownership is explicitly partitioned.
- Subagents run focused checks only. The coordinator owns repository-wide
  verification and runs it once after the wave reaches an edit freeze.
- Permit one additional full verification run only after fixing a failure from
  the first integration run. If a suite hangs or times out, isolate the test
  with a hard timeout instead of rerunning the unchanged suite.
- Do not begin another wave until the current wave is integrated and reconciled
  in CLEO. Stop after the number of waves requested by the user.
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
- Treat architecture-documentation updates as integration-owned unless a wave
  explicitly assigns that file.
- Discover with CLEO before reading files. Every agent, including the
  coordinator, starts exploration from CLEO — `cleo find`/`cleo focus` for
  tasks, `cleo memory find`/`decision-find` for prior knowledge, and `cleo
  graph search`/`deps`/`impact` for code — to identify exactly which files and
  symbols matter before opening them. Run the CLEO lookups and the file reads
  in the same batch when the relevant paths are already known. Treat CLEO
  results as pointers, not ground truth: read the cited files to confirm
  current behavior.
- Orient before exploring. Spawned subagents start from a `cleo focus <taskId>`
  orientation pack plus a BRAIN/nexus JIT budget of at most three lookups,
  instead of open-ended grep/glob/read sweeps. `cleo focus` replaces repeated
  `cleo show` calls.
- Generate every CLEO-task subagent prompt from `cleo orchestrate spawn
  <taskId> --tier 1` output and dispatch it to a CLEO-specific agent from
  `.opencode/agent/` (`cleo-task-executor`, `cleo-explorer`, or
  `cleo-planner`). Do not hand-roll task prompts for built-in agents.
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
  When a wave has more than one task to close, bulk-complete it in one
  transaction with `scripts/cleo-bulk-complete.ps1 -ManifestPath <manifest.json>`
  (one `sqlite3` process, one commit). Reserve single `cleo complete` for a lone
  task. Per-task completion costs O(N x process startup) and is a known
  anti-pattern.
- At every wave edit freeze, run `cleo graph sync --incremental` and `cleo
  nexus refresh-bridge` (integration-owned) before integration verification so
  nexus impact and context lookups reflect the wave's changes.
- Run `cleo graph impact <symbol>` before moving or renaming any public
  symbol. HIGH/CRITICAL blast-radius findings stop the change and surface as
  integration findings.
- NEVER use workflow gates, gate tools, or gate events (`workflow.gate.*`). All
  workflow gates, gate resolution schemas, and gate cards are permanently
  removed and prohibited. Any operator clarification, review, step validation,
  or approval blockage MUST route conversationally through
  `_emit_orchestrator_text` and `SUB_AGENT_NEEDS_INPUT` (`_Fenced`), using the
  exact same conversational message pattern as hypothesis creation. The operator
  always responds via standard chat input.

