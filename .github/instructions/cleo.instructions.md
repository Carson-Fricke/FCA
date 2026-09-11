---
applyTo: "**"
---

# CLEO Agent Guide

These are project-scoped operating rules for CLEO `v2026.8.9`. Read them before changing task state.

## Repository Discovery

- Never encode or assume a workstation-specific FCA checkout path. Resolve the repository root from Git with `git rev-parse --show-toplevel` when already inside the checkout.
- When the current directory is not a Git checkout, locate the FCA checkout by its `origin` URL or CLEO project registration, not by a fixed drive, home directory, username, or parent folder.
- Confirm the selected checkout with `git remote get-url origin`; its repository name must be `FCA` before mutating Git or CLEO state.
- Run every `cleo` and repository `git` command with the resolved repository root as the tool working directory. Prefer the tool's `workdir` option over changing directories inside a shell command.
- In PowerShell, capture a discovered root with `$repoRoot = git rev-parse --show-toplevel`. In POSIX shells, use `repo_root="$(git rev-parse --show-toplevel)"`. Do not persist the resulting absolute value.

## Session Lifecycle And Handoff

- Run `cleo briefing --json` once at session start, alongside the repository
  discovery commands. It restores the last handoff, current task focus, next
  tasks, blockers, and a BRAIN memory digest in one call.
- Focus the session with `cleo start <task-id>` when picking up work, and clear
  it with `cleo stop` when done. Do not leave a completed task focused;
  `cleo current` and briefings otherwise report stale focus.
- Orient on a task with `cleo focus <task-id>` — a single envelope holding
  identity, scope, blockers, ready wave, attached docs, and BRAIN context
  (about 1 500 tokens) that replaces roughly eight separate calls. Use
  `cleo show <id> --full --json` only when the focus envelope lacks a field
  you need. Do not orient with repeated `cleo show` calls.
- Record durable discoveries with `cleo memory observe` and session-scoped
  decisions with `cleo session record-decision` as they happen, not
  retroactively. Cross-session architectural decisions belong in
  `cleo memory decision-store`.
- Before ending a session or before context compaction, run `cleo memory
  precompact-flush` to flush in-flight observations and checkpoint the WAL.
- End working sessions with `cleo safestop --reason <text> --handoff` so the
  next session's briefing has a handoff to restore. Use `--no-session-end` to
  write the handoff without ending the session, or `cleo session suspend` to
  pause a session that will resume in a different context window.
- After significant BRAIN writes at session end, run `cleo memory consolidate`
  (idempotent) and `cleo refresh-memory` so `.cleo/memory-bridge.md` reflects
  current learnings and decisions.
- Session-end hygiene runs per session. It does not replace the wave-level
  Validation Checklist, which still runs once per reconciliation batch.

## Fast Lookups: BRAIN And Nexus

The local CLEO fork makes BRAIN semantic memory and the nexus code graph
fully functional. Consult them BEFORE re-deriving knowledge the hard way —
these read-only commands are safe for every agent and subagent, including
subagents doing focused task work.

- Search durable knowledge first: `cleo memory find "<query>"` (BM25 across
  observations/decisions/learnings/patterns), `cleo memory search-hybrid
  "<query>"` (adds vector ranking), and `cleo memory decision-find "<topic>"`
  before grepping for architectural rationale. Decisions D001+ record the
  ruff/ty-only rule, uv-only verification, wave ownership policy, and the
  SQLite-repair policy.
- Trace a symbol's knowledge: `cleo memory code-memories-for-code <symbol>`
  lists memories anchored to code, and `cleo graph context <symbol>` shows the
  graph neighborhood. Graph node IDs are prefixed — use `decision:D001`, not
  `D001`, in `memory related|context|trace`; bare IDs silently return empty.
- Check code structure before structural changes: `cleo graph search <name>`,
  `cleo graph resolve <symbol>`, `cleo graph deps <symbol>` (callers/callees),
  and `cleo graph impact <symbol>` for blast radius before moving or renaming
  public symbols. `cleo graph clusters|flows|hot-paths` summarize architecture;
  `cleo graph discover` is the quick structural overview. Re-index after large
  refactor waves with `cleo graph sync` (add `--incremental` for small edits).
- Session context: `cleo briefing` (session start) already includes a BRAIN
  digest; `cleo memory digest --json` and `cleo memory recent --since 24h`
  give focused memory recaps mid-session.
- JIT budget: at most three BRAIN or nexus lookups per task phase. If the
  budget is exhausted and context is still missing, report the task prompt as
  underspecified instead of re-deriving knowledge with open-ended grep sweeps.
- Blast radius is mandatory before structural moves: run `cleo graph impact
  <symbol>` before renaming or relocating any public symbol. HIGH/CRITICAL
  findings stop the change and surface as integration findings.
- Trust requires freshness: if `cleo graph status` or `cleo nexus status`
  reports a high stale-file count, nexus results are untrustworthy — run
  `cleo graph sync --incremental` before relying on impact/context answers,
  or say that `git grep` was used as ground truth instead.
- Write-backs are covered by the Session Lifecycle section above: observe
  durable discoveries (`cleo memory observe`), store architectural decisions
  (`cleo memory decision-store`), and keep embeddings current with
  `cleo brain maintenance` or the backfill script after bulk writes.

## Transcript Extraction Via Opencode

- opencode sessions are not hook-visible to CLEO, so `llmExtraction` never
  fires on its own. Make transcripts available with
  `node .github/scripts/convert-opencode-transcript.mjs` (optionally passing a
  session id; it defaults to the most recent opencode session for this
  repository). It writes Claude-format JSONL under `~/.claude/projects/` where
  CLEO's transcript scanner finds it.
- Run `cleo transcript extract <session-id> --dry-run` first, then without
  `--dry-run` to store memories. Reliable on the local fork (which fixes the
  upstream prompt/schema contradiction, kryptobaseddev/cleo#1219); on the
  published 2026.8.9 package it fails with "response did not match schema" —
  the root-cause chain is preserved in brain observation O-mtngiqim-0.
  Extraction pins the `extraction` role; keep it on a schema-disciplined
  model. The source JSONL is deleted after successful extraction, and a
  re-run of `node .github/scripts/backfill-brain-embeddings.mjs` (or
  `cleo brain maintenance` on the fork) embeds the new rows.
- The transcript path resolves the `openai` provider profile through
  `CLEO_HOME/plugins/model-providers/openai-openrouter-redirect.mjs`, which
  redirects its base URL to OpenRouter (works around kryptobaseddev/cleo#1216
  for this call site). That plugin file is machine-local and is not tracked by
  the repository; recreate it from brain observation O-mtn8k38i-0 if absent.

## CLEO Fork Install

- The `cleo` binary on this machine is a locally patched fork, not the
  published npm package: `Carson-Fricke/cleo`, branch
  `fix/subsystem-defects-2026.8.9` (commit `310c4e986` on top of v2026.8.9),
  installed globally from tarballs. It repairs upstream issues #1216 (openai-
  compatible transport base URLs), #1217 (embedding activation deadlock),
  #1218 (`graph sync` no-op + `graph discover` routing), and #1219 (transcript
  extraction prompt/schema contradiction).
- Clone lives at `<repo-sibling>/cleo` (`C:\Users\csfri\source\repos\cleo`).
  Rebuild with `pnpm install --frozen-lockfile && pnpm run build` (the repo
  pins `script-shell` to Git Bash for `rm -rf` build scripts), then
  `pnpm --filter @cleocode/core pack` and `pnpm --filter @cleocode/cleo pack`,
  and install both tarballs in one
  `npm install -g <core.tgz> <cleo.tgz> --allow-scripts=@cleocode/core,tree-sitter,...`
  invocation.
- Revert to upstream with `npm install -g @cleocode/cleo@2026.8.9`. The clone
  must keep `core.autocrlf false`: CRLF migration files hash differently from
  the LF journal entries written by the npm install, which replays drizzle
  migrations against existing tables.
- When upstream ships fixes for #1216/#1217/#1218/#1219, reinstall the
  published package, retire the local workarounds, and close T189/T205 per
  their acceptance criteria. The global `cleo.db` journal was manually
  reconciled on 2026-09-04 (t11363-consolidation + t11622-agent-registry-rename
  marked applied) — if per-command "table already exists" reconciliation
  warnings reappear, re-run that journal repair before re-probing DDL.

## Portable Paths

- Paths persisted in tasks, acceptance criteria, notes, evidence, configuration, generated artifacts, or `.cleo-export.json` must be relative to the repository root and use `/` separators.
- Use paths such as `.github/verification/T067-validation.json`, never drive-letter paths, UNC paths, `/Users/<name>/...`, `/home/<name>/...`, or temporary-directory paths.
- Pass repository-relative values to `--files`, `test-run:`, `--params-file`, `--file`, and `-o` whenever the path can be retained in CLEO state or an export.
- Temporary files may be used for transient command input only. Do not reference them in CLEO evidence, task metadata, configuration, or committed output.
- Do not write the resolved checkout root into instructions, scripts, task descriptions, notes, or verification records. Use `<repo-root>` only in explanatory prose.
- Before publishing an export, run `node .github/scripts/validate-cleo-export.mjs`; it rejects common machine-specific filesystem paths.

## Inspect Before Mutating

- Start with `cleo status --json`, `cleo list --json --limit 100`, `cleo tree T001 --withDeps`, and `git status --short --branch`.
- Compare task status with tracked implementation files and Git history. Do not mark planned service work done based on a markdown plan.
- Use `cleo show <id> --full --json` when acceptance criteria, evidence, notes, or verification matter. The default projection omits these fields.
- An active CLEO session may exist even when `cleo current --json` has no focused task. `cleo add` and `cleo add-batch` require an active session.
- Use `cleo --help` or `cleo <command> --help`; `cleo help` is not a command. `cleo tree` requires a root ID, and its dependency flag is spelled `--withDeps`.
- `cleo list` defaults to a short page, so always provide a limit large enough to reconcile the complete project.

## Hierarchy And Batches

- CLEO enforces `Saga -> Epic -> Task -> Subtask`. It rejects `Saga -> Task` and other shortcuts.
- Decompose epics with the WorkGraph pipeline, not repeated `cleo add` calls:
  build the scaffold JSON, validate it with `cleo workgraph validate --file
  <scaffold.json> --dry-run` (returns `wouldCreate`/`wouldUpdate`/
  `wouldDelete` with zero writes), then apply once with `cleo workgraph apply
  --file <scaffold.json>` for a single-transaction insert. Check structure
  with `cleo workgraph structure --file <scaffold.json>` for cycles, depth
  violations, and orphans before applying.
- For plain multi-task creation, use `cleo add-batch --file <json>` — payload
  `{ "tasks": [...] }` with `acceptance`, `labels`, `depends`, and `files` as
  JSON arrays — in chunks of at most 20 tasks per transaction. Never loop
  single `cleo add` calls to create more than two tasks.
- Use `cleo add-batch --file <json> --dry-run` first. A successful dry run returns `wouldCreate` and `validatedCount` without writes.
- Prefer `--file` or `--params-file` on Windows. PowerShell 5.1 invocation can strip quotes from inline `--params` JSON before CLEO parses it.
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
- `cleo next --json` can rank a root saga above executable leaf tasks. Filter
  recommendations by type and dependency state; do not rely on task IDs listed
  in documentation as currently actionable.

## Completion And Evidence

- Use `cleo complete`, not `cleo update --status done`.
- NEVER complete multiple tasks with separate `cleo complete` invocations. When
  a wave (or any set of tasks) has more than one task to close, bulk-complete it
  in a single SQLite transaction with
  `scripts/cleo-bulk-complete.ps1 -ManifestPath <manifest.json>` (one `sqlite3`
  call, one commit, ADR-051 `verification_json` evidence preserved). Reserve
  single `cleo complete` for a lone task. A per-task loop costs O(N x ~25 s
  process startup) plus a lingering handle per call and is a known
  anti-pattern; the bulk script closes the same set in seconds.
- Bulk-completion manifest shape:
  `{ "testRunPath": ".cleo/test-runs/<file>.json", "tasks": [ { "id": "T251",
  "commit": "<sha>", "files": ["src/harness/..."] } ] }`. `files` is optional and is
  derived from `git show --name-only` when omitted. Back up `.cleo/cleo.db`
  first, constrain updates by task ID and non-terminal status (the script does),
  and run the Validation Checklist once after the batch.
- Completion requires acceptance-criterion evidence bindings. Verification gates alone may not satisfy old free-text acceptance rows.
- `cleo verify --all` enforces gate-specific evidence. `testsPassed` requires recognized test evidence and `qaPassed` requires a recognized tool or PR; an unknown project type may have no resolvable default test command.
- Structured `test-run:` evidence uses Jest-style summary keys in this CLEO version: `numTotalTests`, `numPassedTests`, `numFailedTests`, `numPendingTests`, and `numTodoTests`. A generic `totalTests` field is ignored.
- Keep `test-run:` evidence under a repository-relative path when it will be exported. Absolute temp paths are retained verbatim in `.cleo-export.json` and make the package machine-specific.
- This repository configures `audit.command` in `.cleo/project-context.json` to run `.github/scripts/validate-cleo-export.mjs`. Use `tool:audit` as repeatable QA evidence for portable export changes.
- For a pre-existing task that demonstrably predates evidence bindings, use an explicit audited `--waive-ac ... --waive-reason ...` only when repository evidence independently proves completion.
- Completing the final pending child can auto-complete its epic or saga. Add planned siblings before completing a placeholder child, or immediately verify parent status, lifecycle, verification, and timestamps.
- `cleo complete` may warn that `task/<id>` does not exist during worktree integration. This warning does not negate a successful task-state mutation when no task branch was expected.

## Verification Commands And Evidence

- Run Python verification only through `uv` from the resolved repository root.
  Do not call `.venv\Scripts\python.exe`, `.venv/bin/python`, or any other
  virtual-environment interpreter directly.
- Use the canonical command table in
  `.github/instructions/copilot-instructions.md` for lock, lint, format, type,
  test, build, and entrypoint checks.
- Use `ruff` for all lint and format checks and `ty` for all type checks. Do not
  record evidence from `mypy`, `pyright`, `flake8`, `pylint`, `black`, or any
  other lint/type/format tool.
- Record the same canonical `uv` commands in verification records, evidence
  `command` values, and workflow references. Do not record direct interpreter
  paths.
- Use `node <script>` only for the explicitly Node-based repository validators,
  such as `node .github/scripts/validate-cleo-export.mjs`.

## Architecture Documentation

- Treat `.github/architecture/*.md` as the source of truth for repository
  structure, package boundaries, product responsibilities, and deployment
  shape.
- Update the affected architecture document in the same task whenever a task
  adds, removes, renames, or repurposes a package or capability directory, or
  changes public facades, entrypoints, dependency direction, or deployment
  shape.
- Keep architecture documentation factual and current; do not describe planned
  behavior as implemented, and update the `## Status` section when a planned
  capability is implemented.
- Architecture-documentation changes for a wave are integration-owned unless
  the wave explicitly assigns that file.

## Verification Budget And Batching

- Treat one user-approved implementation wave as one CLEO reconciliation
  batch. Define task IDs and non-overlapping file ownership before fan-out.
- Run repository discovery commands once per session. Repeat them only when the
  repository root, branch, CLEO session, or task graph changed externally.
- Subagents run focused tests plus focused Ruff and ty checks for their owned
  behavior. They do not run the complete repository suite, mutate CLEO, or
  write final verification records.
- After all subagents finish, freeze edits and run `cleo graph sync
  --incremental` and `cleo nexus refresh-bridge` (integration-owned) so nexus
  impact, context, and bridge lookups reflect the wave's changes. Then run
  repository-wide lock, Ruff, format, ty, pytest, Bicep, and workflow checks
  once as applicable. A second full run is permitted only after fixing a
  failure from the first integration run.
- If a suite hangs or times out, do not rerun it unchanged. Isolate the hanging
  test with a hard timeout, fix the cause, then run one final full suite.
- Keep completed task evidence immutable when later work increases the global
  test count. Task records contain stable focused evidence; store global
  integration results in one wave-level artifact and reuse them with
  `--shared-evidence` when appropriate.
- Assemble every evidence atom before the first `cleo verify` call: capture
  the commit sha, file list, test-run JSON, and tool results up front, then
  make one `cleo verify <id> --all` invocation covering implemented, test,
  QA, and acceptance evidence. Iterative per-gate verify attempts are an
  anti-pattern — a failed gate forces evidence recapture, and files changed
  after an earlier `verify` trigger staleness re-verification.
- Use `tool:audit` only for CLEO export portability and task-state publication,
  not as generic application QA evidence.
- Run the Validation Checklist once after all task-state mutations in the
  reconciliation batch, immediately before export or commit. Do not run it
  after every task.
- Leave tasks with unmet live or external acceptance criteria pending. Do not
  implement dependent tasks speculatively unless the user explicitly approves
  that work.

## Reconciliation Edge Case

- During the 2026-08-31 reconciliation, completing initialization task `T002` auto-closed `T001` because it was then the only saga child. The saga was reopened before the service epics were added.
- The auto-close set `pipeline_stage=contribution`, and new descendants inherited that terminal stage. The public update API is forward-only and could not move it backward.
- The stale metadata was normalized in `.cleo/cleo.db`: pending saga and epics use `research`, pending implementation tasks use `implementation`, `T001.completed_at` is null, and root verification was reset.
- Do not repeat ad-hoc direct SQLite mutation for normal CLEO work; it was a
  one-time repair for a CLI-generated state that the public CLI could not
  reverse. The single sanctioned exception is the reviewed bulk-completion
  bridge `scripts/cleo-bulk-complete.ps1`, which performs one transactional
  `UPDATE` per task to avoid O(N) `cleo complete` process startups. Back up the
  database first and constrain updates by ID and non-terminal status.
- `cleo update <done-parent> --status pending` can reopen status but does not clear `completed_at`, verification, or a terminal `pipeline_stage`. Once reopened this way, `cleo restore task` refuses to operate because the task is already pending.
- Although help lists `contribution` as resettable, `cleo lifecycle reset <id> contribution` rejects that stage in this version. Resetting lifecycle state also does not change `tasks_tasks.pipeline_stage`.

## Storage And Checkpoints

- Canonical task state is `.cleo/cleo.db`. It is intentionally ignored by project Git and the isolated checkpoint Git repository under CLEO's deny-by-default policy.
- `cleo checkpoint` does not persist the database; it checkpoints allow-listed CLEO documents such as ADRs and RCASD artifacts.
- Use `cleo backup add` for SQLite and runtime-state recovery snapshots.
- Confirm database recovery with `cleo backup verify --json`. In this repository, a healthy `tasks` snapshot is authoritative; missing optional or legacy-role databases do not mean task state is missing.
- Publish portable task state with `cleo export-tasks T001 --subtree --include-deps -o .cleo-export.json`. In this CLI, the long `--output` flag is intercepted as a renderer mode; use short `-o` for the export file path.
- Validate the export with `node .github/scripts/validate-cleo-export.mjs` and `cleo import-tasks .cleo-export.json --dry-run --on-conflict skip --on-missing-dep fail`.
- Do not run `cleo push` while its isolated checkpoint remote points to the application repository's `master`. The histories are unrelated, the canonical database is excluded anyway, and a force push could replace application history. Use normal Git plus `.cleo-export.json`, or configure a genuinely separate CLEO remote or branch.
- Project Git tracks `.cleo/rcasd/**`. Task creation can generate RCASD research stubs that appear as untracked files; inspect and include them when committing the task decomposition.
- Do not manually edit `.cleo/cleo.db` unless repairing an unrepresentable CLI state as described above, or running the sanctioned bulk-completion bridge `scripts/cleo-bulk-complete.ps1`.

## Validation Checklist

After all CLEO state changes in a reconciliation batch, run this checklist
once:

```powershell
cleo check schema todo --json
cleo check coherence --json
cleo sequence check --json
cleo deps cycles --json
cleo deps validate --scope all
cleo tree T001 --withDeps
cleo plan --json
```

The original reconciled roadmap baseline contained:

- `137` total tasks: two root sagas, `20` epics, and `115` tasks. Later
  implementation and process tasks may increase this count; query CLEO for the
  current total.
- The lake saga is `T001`; the quant-harness saga is `T068` with milestone epics `T069` through `T077`, implementation tasks `T078` through `T136`, and roadmap publication task `T137`.
- Original harness source task identities are retained as `harness.h001` through `harness.h058` labels; `T121` is an added OPRA WebSocket plan-gap task.
- At baseline, dependency cycles were absent and schema and coherence checks
  passed. Use the Validation Checklist for the current state.
- The only full-graph dependency-validator findings are the known root-saga `E_ORPHAN` defects for `T001` and `T068`.

## Known CLEO Diagnostics

- `cleo doctor` and `cleo db-substrate` in this consolidated-database release still probe legacy `.cleo/tasks.db` and can falsely report zero tasks or missing databases even though `.cleo/cleo.db` is healthy and `cleo list` returns all tasks.
- Prefer `cleo check schema todo`, `cleo check coherence`, `cleo sequence check`, normal task queries, and a direct SQLite integrity check on `.cleo/cleo.db` when evaluating project task health.
- `cleo check task <existing-id>` currently reports that the ID already exists as a uniqueness violation, so it is not useful for validating an existing task in this version.
- `cleo deps validate --epic` reports valid external dependencies as missing references. Use full-graph validation and inspect its complete issue list.
- CLEO may emit non-fatal warnings when no LLM provider is provisioned. Core task mutations still succeed; use each command's success envelope as the result.
- On Windows, CLEO's evidence runner cannot directly spawn the npm PowerShell `cleo` shim for a configured tool. Configure repository audit commands as directly executable `node <script>` commands instead.
- The global `~/.agents/AGENTS.md` currently references `~/.cleo/templates/CLEO-INJECTION.md`, which is absent on this machine. This project guide is therefore referenced directly from the repository `AGENTS.md` and must not rely on that global template being present.
