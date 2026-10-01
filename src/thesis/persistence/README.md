# Workspace publication

This package owns durable accepted-command journaling and fenced Git publication.
It does not interpret strategy, hypothesis, chat, or operator contracts.

- `__init__.py` exposes persistence contracts.
- `journal.py` records idempotent accepted commands and publication receipts.
- `git.py` commits the accepted manifest and optional exact artifact bytes using
  a temporary index, process lock, and HEAD compare-and-swap. It sanitizes Git
  environment overrides and preserves unrelated index and worktree changes.
  Public read/recovery methods support projections from tracked Git artifacts.
- `reconcile.py` reconciles journal acceptance with publication receipts.
