import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import WorkflowCard from '$lib/components/chat/WorkflowCard.svelte';
import type { WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';

const execution: WorkflowExecutionRecord = {
	execution_id: 'exec-1',
	chat_id: 'chat-1',
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
	{ name: 'research', status: 'active' },
	{ name: 'report', status: 'pending' }
];

describe('WorkflowCard', () => {
	it('enters the current phase orchestrator from the card body', async () => {
		const onEnterPhase = vi.fn();
		render(WorkflowCard, { props: { execution, phases, status: 'running', onEnterPhase } });

		await fireEvent.click(screen.getByTestId('workflow-card-body'));
		expect(onEnterPhase).toHaveBeenCalledWith('research', { readOnly: false });
	});

	it('enters a finished phase read-only and disables pending phases', async () => {
		const onEnterPhase = vi.fn();
		render(WorkflowCard, { props: { execution, phases, status: 'running', onEnterPhase } });

		const steps = screen.getAllByTestId('phase-step');
		await fireEvent.click(steps[0]);
		expect(onEnterPhase).toHaveBeenCalledWith('plan', { readOnly: true });

		const pending = screen.getByText('report') as HTMLButtonElement;
		expect(pending.disabled).toBe(true);

		await fireEvent.click(steps[1]);
		expect(onEnterPhase).toHaveBeenLastCalledWith('research', { readOnly: false });
	});

	it('surfaces a phase that is waiting on operator input', () => {
		render(WorkflowCard, {
			props: { execution, phases, status: 'running', attentionPhases: ['research'] }
		});

		expect(screen.getByTestId('workflow-card-needs-input')).toHaveTextContent(
			'Input needed for research'
		);
		expect(screen.getByText('research')).toHaveAttribute('data-attention', 'true');
		expect(screen.getByText('plan')).toHaveAttribute('data-attention', 'false');
	});

	it('marks completed, live, and future phases distinctly', () => {
		render(WorkflowCard, { props: { execution, phases, status: 'running' } });

		expect(screen.getByText('plan')).toHaveAttribute('data-status', 'done');
		expect(screen.getByText('research')).toHaveAttribute('data-status', 'active');
		expect(screen.getByTestId('phase-liveness')).toBeInTheDocument();
		expect(screen.getByText('report')).toHaveAttribute('data-status', 'pending');
		expect(screen.getByText('report')).toBeDisabled();
	});

	it('shows the current loop attempt without duplicating the phase', () => {
		render(WorkflowCard, {
			props: {
				execution,
				phases: [{ name: 'strategy_design', status: 'active', attempts: 3 }],
				status: 'running'
			}
		});

		expect(screen.getAllByTestId('phase-step')).toHaveLength(1);
		expect(screen.getByText(/attempt 3/)).toBeInTheDocument();
	});
});
