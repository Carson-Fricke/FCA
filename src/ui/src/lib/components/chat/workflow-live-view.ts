import { getContext, setContext } from 'svelte';
import type { WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';

/**
 * Execution-specific live state shared by root and scoped datum renderers.
 *
 * The transcript provides this at its component root so the registry-selected
 * renderer (`WorkflowExecutionCard`) upgrades a persisted `workflow_execution`
 * datum with the same liveness the legacy `kind=workflow` branch has: the live
 * execution/phases/attention state and the phase navigation callback.
 */
export interface WorkflowLiveView {
	execution: WorkflowExecutionRecord | null;
	phases: WorkflowPhase[];
	attentionPhases: string[];
	onEnterPhase: (phase: string, options: { readOnly: boolean }) => void;
}

/** Reads the current live view; the transcript's props stay reactive through it. */
export type WorkflowLiveViewSource = (executionId: string) => WorkflowLiveView | null;

const WORKFLOW_LIVE_VIEW_KEY = Symbol('workflow-live-view');

export function setWorkflowLiveView(source: WorkflowLiveViewSource): void {
	setContext(WORKFLOW_LIVE_VIEW_KEY, source);
}

export function getWorkflowLiveView(): WorkflowLiveViewSource | null {
	return (
		getContext<WorkflowLiveViewSource | undefined>(WORKFLOW_LIVE_VIEW_KEY) ?? null
	);
}
