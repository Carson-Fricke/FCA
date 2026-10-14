# Workspace shell

These Svelte components present workspace navigation and shell dialogs. They
consume the API facade and view state; runtime execution and deletion durability
belong to backend services.

- `BackendSettingsDialog.svelte`: Selects the configured API backend.
- `CommandPalette.svelte`: Searches shell navigation, strategies, and widgets.
- `CopilotPanel.svelte`: Hosts the workspace conversation in the side pane.
- `CreateWorkspaceDialog.svelte`: Collects workspace creation input.
- `DataModeBadge.svelte`: Displays the active backend mode.
- `DeleteWorkspaceDialog.svelte`: Confirms the full deletion scope, submits the
  API request, presents retryable failures, and clears browser state on acceptance.
- `Dialog.svelte`: Shared modal presentation and close affordances.
- `Sidebar.svelte`: Workspace navigation, counts, pinned widgets, and shell actions.
- `TopBar.svelte`: Current page, workspace context, activity, and copilot controls.
- `WorkspaceSwitcher.svelte`: Selects workspaces and exposes creation and deletion.
