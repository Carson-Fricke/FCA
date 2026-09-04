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
