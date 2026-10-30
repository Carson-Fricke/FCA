# Workspace routes

The workspace layout owns the navigation shell and presents route content. It
consumes workspace summaries and the shared API/view-state facades; execution,
authorization and durable workspace changes remain backend responsibilities.

- `+layout.ts`: Resolves the workspace summary and rejects missing workspaces
  before rendering the shell; detailed counts and catalogs load afterward.
- `+layout.svelte`: Composes compact/expanded navigation, workspace switching,
  shell dialogs and the resizable copilot. Fullscreen conversations fill its
  content area; other pages retain normal page padding and scrolling.
- `+page.svelte`: Redirects the workspace root to its overview.

Product pages live under `overview/`, `conversations/`, `strategies/`,
`backtests/`, `deployments/`, `dashboards/`, `screener/`, `artifacts/`,
`exploration/` and `workflows/`. They use the layout's workspace context rather
than creating another workspace or conversation lifecycle owner.
