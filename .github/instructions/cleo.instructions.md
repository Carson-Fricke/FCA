---
applyTo: "**"
---

# CLEO Agent Guide

These are project-scoped operating rules for CLEO `v2026.8.9`. Read them before changing task state.

## Project Location

- The FCA repository is `C:\Users\csfri\source\repos\FCA`.
- `C:\Users\csfri\source\fca` is an empty directory and is not the repository.
- Run every `cleo` and repository `git` command with the actual repository as the working directory. CLEO discovers project state from the current directory.

## Inspect Before Mutating

- Start with `cleo status --json`, `cleo list --json --limit 100`, `cleo tree T001 --withDeps`, and `git status --short --branch`.
- Compare task status with tracked implementation files and Git history. Do not mark planned service work done based on a markdown plan.
- Use `cleo show <id> --full --json` when acceptance criteria, evidence, notes, or verification matter. The default projection omits these fields.
- An active CLEO session may exist even when `cleo current --json` has no focused task. `cleo add` and `cleo add-batch` require an active session.
- Use `cleo --help` or `cleo <command> --help`; `cleo help` is not a command. `cleo tree` requires a root ID, and its dependency flag is spelled `--withDeps`.
- `cleo list` defaults to a short page, so always provide a limit large enough to reconcile the complete project.

## Hierarchy And Batches

- CLEO enforces `Saga -> Epic -> Task -> Subtask`. It rejects `Saga -> Task` and other shortcuts.
- Use `cleo add-batch --file <json>` for multiple tasks. Its payload is `{ "tasks": [...] }`; `acceptance`, `labels`, `depends`, and `files` are JSON arrays.
- Prefer `--file` or `--params-file` on Windows. PowerShell 5.1 invocation can strip quotes from inline `--params` JSON before CLEO parses it.
- Run `cleo add-batch --file <json> --dry-run` first. A successful dry run returns `wouldCreate` and `validatedCount` without writes.
- Large atomic batches can exceed a two-minute caller timeout even when valid. Use a long timeout and confirm the task count afterward. A timed-out transaction observed here rolled back without partial task inserts.
- New child acceptance criteria are automatically projected onto parent epics and the root saga as `Complete child ...` criteria.

## Dependencies

- Set task dependencies by task ID, not title: `cleo update <id> --depends T001,T002`.
- Mirror cross-epic task dependencies at the owning epic level. `cleo deps validate` reports `E_CROSS_EPIC_GAP` otherwise.
- Validate with `cleo deps cycles --json` and `cleo deps validate --scope all` after bulk changes.
- In CLEO `v2026.8.9`, `cleo deps validate --scope all` reports the root saga as `E_ORPHAN` because it has no parent. Treat that single root-saga finding as a validator defect only when it is the sole issue.
- Do not use `cleo deps validate --epic <id>` as proof for an epic with cross-epic edges. The scoped validator excludes legitimate external targets, then reports them as `E_MISSING_REF`. It is useful only when all dependencies stay inside the selected epic.
- `cleo deps waves` can misclassify cross-epic prerequisites as ready within a saga projection. Use `cleo plan --json`, `cleo next --json`, or the task's `blockedBy` projection for actionable scheduling.
- `cleo deps overview --json` currently reports the reconciled graph as valid even though `cleo deps validate --scope all` emits the known root-saga defect. Use both cycle and scoped validation rather than relying on one projection.
- `cleo next --json` can rank the root saga above executable leaf tasks. Filter recommendations by type and dependency state; the current ready implementation leaves are `T013` and `T016`.

## Completion And Evidence

- Use `cleo complete`, not `cleo update --status done`.
- Completion requires acceptance-criterion evidence bindings. Verification gates alone may not satisfy old free-text acceptance rows.
- `cleo verify --all` enforces gate-specific evidence. `testsPassed` requires recognized test evidence and `qaPassed` requires a recognized tool or PR; an unknown project type may have no resolvable default test command.
- For a pre-existing task that demonstrably predates evidence bindings, use an explicit audited `--waive-ac ... --waive-reason ...` only when repository evidence independently proves completion.
- Completing the final pending child can auto-complete its epic or saga. Add planned siblings before completing a placeholder child, or immediately verify parent status, lifecycle, verification, and timestamps.
- `cleo complete` may warn that `task/<id>` does not exist during worktree integration. This warning does not negate a successful task-state mutation when no task branch was expected.

## Reconciliation Edge Case

- During the 2026-08-31 reconciliation, completing initialization task `T002` auto-closed `T001` because it was then the only saga child. The saga was reopened before the service epics were added.
- The auto-close set `pipeline_stage=contribution`, and new descendants inherited that terminal stage. The public update API is forward-only and could not move it backward.
- The stale metadata was normalized in `.cleo/cleo.db`: pending saga and epics use `research`, pending implementation tasks use `implementation`, `T001.completed_at` is null, and root verification was reset.
- Do not repeat direct SQLite mutation for normal CLEO work. It was a one-time repair for a CLI-generated state that the public CLI could not reverse. If a similar case occurs, back up first, constrain updates by ID and status, then run all validation checks below.
- `cleo update <done-parent> --status pending` can reopen status but does not clear `completed_at`, verification, or a terminal `pipeline_stage`. Once reopened this way, `cleo restore task` refuses to operate because the task is already pending.
- Although help lists `contribution` as resettable, `cleo lifecycle reset <id> contribution` rejects that stage in this version. Resetting lifecycle state also does not change `tasks_tasks.pipeline_stage`.

## Storage And Checkpoints

- Canonical task state is `.cleo/cleo.db`. It is intentionally ignored by project Git and the isolated checkpoint Git repository under CLEO's deny-by-default policy.
- `cleo checkpoint` does not persist the database; it checkpoints allow-listed CLEO documents such as ADRs and RCASD artifacts.
- Use `cleo backup add` for SQLite and runtime-state recovery snapshots.
- Confirm database recovery with `cleo backup verify --json`. In this repository, a healthy `tasks` snapshot is authoritative; missing optional or legacy-role databases do not mean task state is missing.
- Project Git tracks `.cleo/rcasd/**`. Task creation can generate RCASD research stubs that appear as untracked files; inspect and include them when committing the task decomposition.
- Do not manually edit `.cleo/cleo.db` unless repairing an unrepresentable CLI state as described above.

## Validation Checklist

After changing CLEO state, run:

```powershell
cleo check schema todo --json
cleo check coherence --json
cleo sequence check --json
cleo deps cycles --json
cleo deps validate --scope all
cleo tree T001 --withDeps
cleo plan --json
```

Expected current state after the reconciliation and publication task were created:

- `67` total tasks: one root saga, `11` epics including completed initialization, and `55` implementation tasks.
- `T002` is done; all service implementation work is pending.
- Dependency cycles are absent, schema and coherence checks pass, and the ID sequence ends at `T067`.
- The only full-graph dependency-validator finding is the known `T001` root-saga `E_ORPHAN` defect.

## Known CLEO Diagnostics

- `cleo doctor` and `cleo db-substrate` in this consolidated-database release still probe legacy `.cleo/tasks.db` and can falsely report zero tasks or missing databases even though `.cleo/cleo.db` is healthy and `cleo list` returns all tasks.
- Prefer `cleo check schema todo`, `cleo check coherence`, `cleo sequence check`, normal task queries, and a direct SQLite integrity check on `.cleo/cleo.db` when evaluating project task health.
- `cleo check task <existing-id>` currently reports that the ID already exists as a uniqueness violation, so it is not useful for validating an existing task in this version.
- `cleo deps validate --epic` reports valid external dependencies as missing references. Use full-graph validation and inspect its complete issue list.
- CLEO may emit non-fatal warnings when no LLM provider is provisioned. Core task mutations still succeed; use each command's success envelope as the result.
- The global `~/.agents/AGENTS.md` currently references `~/.cleo/templates/CLEO-INJECTION.md`, which is absent on this machine. This project guide is therefore referenced directly from the repository `AGENTS.md` and must not rely on that global template being present.
