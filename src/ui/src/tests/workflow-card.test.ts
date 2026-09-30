import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import WorkflowCard from '$lib/components/chat/WorkflowCard.svelte';
import type { WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';

describe('WorkflowCard phase transition graph', () => {
	const execution: WorkflowExecutionRecord = {
		execution_id: 'exec-123',
		chat_id: 'chat-1',
		trigger_message_id: 'msg-1',
		workflow_id: 'hypothesis-development',
		workflow_version: '1.0.0',
		requested_by: 'user-alex',
		state: 'running',
		phase: 'falsification_trials',
		intent_epoch: 1,
		steering_message_ids: [],
		created_at: '2026-01-01T00:00:00Z'
	};

	const phases: WorkflowPhase[] = [
		{ name: 'hypothesis_creation', status: 'done', attempts: 1 },
		{ name: 'falsification_trials', status: 'active', attempts: 1 },
		{ name: 'verdict', status: 'pending' },
		{ name: 'strategy_design', status: 'pending' },
		{ name: 'strategy_backtesting', status: 'pending' },
		{ name: 'strategy_rca', status: 'pending' },
		{ name: 'optimization', status: 'pending' }
	];

	function getPhaseNode(container: HTMLElement, phase: string): HTMLElement {
		const node = container.querySelector<HTMLElement>(
			`[data-testid="phase-step"][data-phase="${phase}"]`
		);
		if (!node) throw new Error(`Phase node ${phase} not found`);
		return node;
	}

	it('renders the graph nodes with correct statuses and labels', () => {
		const { container } = render(WorkflowCard, {
			props: {
				execution,
				phases,
				status: 'running',
				summary: 'Running falsification trials',
				attentionPhases: []
			}
		});

		expect(screen.getByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-card-status')).toHaveTextContent('running');
		expect(screen.getByTestId('workflow-card-summary')).toHaveTextContent(
			'Running falsification trials'
		);

		// Stepper container preserved
		expect(screen.getByTestId('workflow-phase-stepper')).toBeInTheDocument();

		// All 7 nodes rendered
		const stepNodes = screen.getAllByTestId('phase-step');
		expect(stepNodes).toHaveLength(7);

		const trialsNode = getPhaseNode(container, 'falsification_trials');
		expect(trialsNode).toHaveAttribute('data-status', 'active');
		expect(trialsNode).not.toBeDisabled();

		const creationNode = getPhaseNode(container, 'hypothesis_creation');
		expect(creationNode).toHaveAttribute('data-status', 'done');
		expect(creationNode).not.toBeDisabled();

		const verdictNode = getPhaseNode(container, 'verdict');
		expect(verdictNode).toHaveAttribute('data-status', 'pending');
		expect(verdictNode).toBeDisabled();
	});

	it('preserves click-to-drill into phase behavior', async () => {
		const onEnterPhase = vi.fn();
		const { container } = render(WorkflowCard, {
			props: {
				execution,
				phases,
				status: 'running',
				onEnterPhase
			}
		});

		// Clicking a completed phase drills in as readOnly: true
		const creationNode = getPhaseNode(container, 'hypothesis_creation');
		await fireEvent.click(creationNode);
		expect(onEnterPhase).toHaveBeenCalledWith('hypothesis_creation', { readOnly: true });

		// Clicking an active phase drills in as readOnly: false
		const trialsNode = getPhaseNode(container, 'falsification_trials');
		await fireEvent.click(trialsNode);
		expect(onEnterPhase).toHaveBeenCalledWith('falsification_trials', { readOnly: false });

		// Clicking the card body enters current active phase
		await fireEvent.click(screen.getByTestId('workflow-card-body'));
		expect(onEnterPhase).toHaveBeenCalledWith('falsification_trials', { readOnly: false });
	});

	it('displays attention banner and badges when attention needed', () => {
		const { container } = render(WorkflowCard, {
			props: {
				execution,
				phases,
				status: 'running',
				attentionPhases: ['hypothesis_creation']
			}
		});

		expect(screen.getByTestId('workflow-card-needs-input')).toHaveTextContent(
			'Input needed for hypothesis_creation'
		);
		const creationNode = getPhaseNode(container, 'hypothesis_creation');
		expect(creationNode).toHaveAttribute('data-attention', 'true');
	});
});
