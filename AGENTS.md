<!-- CAAMP:START -->
@~/.agents/AGENTS.md
@.cleo/project-context.json
@.cleo/memory-bridge.md
<!-- CAAMP:END -->

@.github/instructions/cleo.instructions.md
@.github/instructions/copilot-instructions.md

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
