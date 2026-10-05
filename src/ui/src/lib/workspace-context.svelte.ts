import { getContext, setContext } from 'svelte';
import type { WorkspaceSnapshot, WorkspaceSummary } from './types';

const WORKSPACE_CONTEXT_KEY = Symbol('thesis.workspace-context');

/**
 * Workspace shell state shared by the workspace layout, sidebar, top bar, and
 * pages. The summary is available before render; the snapshot detail loads
 * after render.
 */
export interface WorkspaceContext {
	readonly summary: WorkspaceSummary;
	detail: WorkspaceSnapshot | null;
}

/** Provide the workspace context from the layout during component init. */
export function provideWorkspaceContext(summary: WorkspaceSummary): WorkspaceContext {
	const context = $state<WorkspaceContext>({ summary, detail: null });
	setContext(WORKSPACE_CONTEXT_KEY, context);
	return context;
}

/** Read the workspace context from a descendant component or page. */
export function useWorkspaceContext(): WorkspaceContext {
	return getContext<WorkspaceContext>(WORKSPACE_CONTEXT_KEY);
}

/** Snapshot fields used until the detail request resolves. */
export function placeholderSnapshot(summary: WorkspaceSummary): WorkspaceSnapshot {
	return {
		workspaces: [summary],
		overview: {
			workspace: summary,
			activity: [],
			openChats: [],
			health: 'operational'
		},
		counts: {
			strategies: 0,
			chats: 0,
			backtests: 0,
			deployments: 0,
			dashboards: 0,
			screeners: 0
		}
	};
}
