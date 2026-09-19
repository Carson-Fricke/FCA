import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it } from 'vitest';
import ToolResultCard from '$lib/components/chat/ToolResultCard.svelte';
import {
	TOOL_RESULT_RENDERERS,
	toolResultComponent
} from '$lib/components/chat/tool-results/registry';
import WorkflowOutcomeCard from '$lib/components/chat/tool-results/WorkflowOutcomeCard.svelte';
import type { ToolResult } from '$lib/types';

const outcome: ToolResult = {
	tool: 'start_workflow',
	datumType: 'workflow_outcome',
	callId: null,
	ok: false,
	summary: 'Hypothesis development exec-1 failed.',
	structured: {
		executionId: 'exec-1',
		status: 'failed',
		phase: 'hypothesis_creation'
	},
	text: 'Hypothesis development exec-1 failed.',
	failure: { kind: 'workflow', message: 'provider down' }
};

describe('WorkflowOutcomeCard', () => {
	it('reports the execution, its status, and the phase it ended in', async () => {
		render(ToolResultCard, { props: { result: outcome, workspaceId: 'w1' } });
		await tick();

		expect(screen.getByTestId('workflow-outcome-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-outcome-status')).toHaveTextContent('failed');
		expect(screen.getByTestId('workflow-outcome-phase')).toHaveTextContent(
			'hypothesis_creation'
		);
		expect(screen.getByTestId('workflow-outcome-execution')).toHaveTextContent('exec-1');
		expect(screen.queryByTestId('generic-tool-card')).toBeNull();
	});

	it('renders a completed outcome without a failure phase', async () => {
		render(ToolResultCard, {
			props: {
				result: {
					...outcome,
					ok: true,
					structured: {
						executionId: 'exec-1',
						status: 'completed',
						phase: 'completed'
					}
				},
				workspaceId: 'w1'
			}
		});
		await tick();

		expect(screen.getByTestId('workflow-outcome-status')).toHaveTextContent('completed');
		expect(screen.getByTestId('workflow-outcome-phase')).toHaveTextContent('completed');
	});

	it('is the registered renderer for the workflow_outcome datum', () => {
		expect(TOOL_RESULT_RENDERERS['workflow_outcome']).toBe(WorkflowOutcomeCard);
		expect(toolResultComponent('start_workflow', 'workflow_outcome')).toBe(
			WorkflowOutcomeCard
		);
	});
});