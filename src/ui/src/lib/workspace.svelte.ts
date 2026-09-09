/**
 * Workspace-scoped client UI state. View state only (selections, pins,
 * transient composer input) — durable state always comes from the API.
 */

export interface Toast {
	id: number;
	message: string;
	error: boolean;
}

function readPins(): Record<string, string[]> {
	if (typeof localStorage === 'undefined') return {};
	try {
		return JSON.parse(localStorage.getItem('thesis.pinnedWidgets') ?? '{}') as Record<string, string[]>;
	} catch {
		return {};
	}
}

function persistPins(pins: Record<string, string[]>): void {
	if (typeof localStorage === 'undefined') return;
	localStorage.setItem('thesis.pinnedWidgets', JSON.stringify(pins));
}

export const ui = $state({
	/** Workspaces the user pinned widgets in: { workspaceId: [widgetId] } */
	pinnedWidgets: readPins() as Record<string, string[]>,
	toasts: [] as Toast[],
	copilotOpen: true,
	lastWorkspace: (typeof localStorage !== 'undefined' ? localStorage.getItem('thesis.workspace') : null) ?? ''
});

export function pinnedWidgets(workspaceId: string): string[] {
	return ui.pinnedWidgets[workspaceId] ?? [];
}

export function togglePin(workspaceId: string, widgetId: string): void {
	const current = ui.pinnedWidgets[workspaceId] ?? [];
	const next = current.includes(widgetId)
		? current.filter((id) => id !== widgetId)
		: [...current, widgetId];
	ui.pinnedWidgets[workspaceId] = next;
	persistPins(ui.pinnedWidgets);
}

export function setLastWorkspace(workspaceId: string): void {
	ui.lastWorkspace = workspaceId;
	if (typeof localStorage !== 'undefined') localStorage.setItem('thesis.workspace', workspaceId);
}

let toastSeq = 0;

export function notify(message: string, error = false): void {
	const id = ++toastSeq;
	ui.toasts.push({ id, message, error });
	setTimeout(() => {
		const index = ui.toasts.findIndex((t) => t.id === id);
		if (index !== -1) ui.toasts.splice(index, 1);
	}, 5000);
}

export function dismissToast(id: number): void {
	const index = ui.toasts.findIndex((t) => t.id === id);
	if (index !== -1) ui.toasts.splice(index, 1);
}
