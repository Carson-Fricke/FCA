import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import WorkflowScope from '$lib/components/chat/WorkflowScope.svelte';
import { WorkflowScopeController } from '$lib/workflow-scope.svelte';
import type { WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';

/**
 * End-to-end in-pane navigation. Fixture mode supplies the durable events, so
 * the real controller (tree derivation, lazy loading, scope) is exercised
 * without a network.
 */
const execution: WorkflowExecutionRecord = {
	execution_id: 'exec-1',
	chat_id: 'chat-alpha-1',
	trigger_message_id: 'msg-1',
	workflow_id: 'wf-strategy-tune',
	workflow_version: '1.4.0',
	requested_by: 'user-alex',
	state: 'running',
	phase: 'research',
	intent_epoch: 0,
	steering_message_ids: [],
	created_at: '2026-09-08T17:14:01Z'
};

const phases: WorkflowPhase[] = [
	{ name: 'plan', status: 'done' },
	{ name: 'research', status: 'active' }
];

describe('WorkflowScope navigation', () => {
	it('enters the current orchestrator, then backs out and uses the breadcrumb', async () => {
		const controller = new WorkflowScopeController('alpha-lab', 'test-nav');
		const onScopeChange = vi.fn();
		render(WorkflowScope, {
			props: {
				controller,
				workspaceId: 'alpha-lab',
				execution,
				phases,
				onScopeChange
			}
		});

		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());

		// Enter the current phase orchestrator from the workflow card body.
		await fireEvent.click(screen.getByTestId('workflow-card-body'));
		await waitFor(() => expect(screen.getAllByTestId('scope-crumb')).toHaveLength(3));
		expect(screen.getByTestId('sub-agent-element')).toBeInTheDocument();

		// Back one level to the workflow card.
		await fireEvent.click(screen.getByTestId('scope-back'));
		await waitFor(() => expect(screen.getAllByTestId('scope-crumb')).toHaveLength(2));

		// Re-enter, then jump back to the conversation root via the breadcrumb.
		await fireEvent.click(screen.getByTestId('workflow-card-body'));
		await waitFor(() => expect(screen.getAllByTestId('scope-crumb')).toHaveLength(3));
		await fireEvent.click(screen.getAllByTestId('scope-crumb')[0]);
		await waitFor(() => expect(screen.getAllByTestId('scope-crumb')).toHaveLength(1));
		expect(screen.getByTestId('workflow-card')).toBeInTheDocument();
	});
});
