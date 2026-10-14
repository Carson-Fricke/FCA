import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import WorkflowCard from '$lib/components/chat/WorkflowCard.svelte';
import type { WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';
import { getWorkflowDefinition } from '$lib/api';

vi.mock('$lib/api', () => ({ getWorkflowDefinition: vi.fn(async () => {
	throw new Error('Definition unavailable');
}) }));

describe('WorkflowCard phase transition graph', () => {
	const execution: WorkflowExecutionRecord = {
		execution_id: 'exec-123',
		chat_id: 'chat-1',
		trigger_message_id: 'msg-1',
		workflow_id: 'hypothesis-development',
		workflow_version: '1.0.0',
		requested_by: 'user-alex',
		state: 'running',
		control_sequence: 0, active_phases: ['falsification_trials'],
		intent_epoch: 1,
		steering_message_ids: [],
		created_at: '2026-01-01T00:00:00Z'
	};

	const phases: WorkflowPhase[] = [
		{ name: 'discovery', status: 'done', attempts: 1 },
		{ name: 'model_creation', status: 'done', attempts: 1 },
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

		// All 8 nodes rendered
		const stepNodes = screen.getAllByTestId('phase-step');
		expect(stepNodes).toHaveLength(8);

		const trialsNode = getPhaseNode(container, 'falsification_trials');
		expect(trialsNode).toHaveAttribute('data-status', 'active');
		expect(trialsNode).not.toBeDisabled();

		const discoveryNode = getPhaseNode(container, 'discovery');
		expect(discoveryNode).toHaveAttribute('data-status', 'done');
		expect(discoveryNode).not.toBeDisabled();

		const creationNode = getPhaseNode(container, 'model_creation');
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
		const creationNode = getPhaseNode(container, 'model_creation');
		await fireEvent.click(creationNode);
		expect(onEnterPhase).toHaveBeenCalledWith('model_creation', { readOnly: true });

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
				attentionPhases: ['model_creation']
			}
		});

		expect(screen.getByTestId('workflow-card-needs-input')).toHaveTextContent(
			'Input needed for model_creation'
		);
		const creationNode = getPhaseNode(container, 'model_creation');
		expect(creationNode).toHaveAttribute('data-attention', 'true');
	});

	it('fetches definition phases and title instead of inventing a hypothesis topology', async () => {
		vi.mocked(getWorkflowDefinition).mockResolvedValueOnce({
			workflow_id: execution.workflow_id, version: execution.workflow_version, title: 'Versioned backend workflow',
			splits: {},
			phases: ['discovery', 'new_backend_phase', 'completed'], terminal_phases: ['completed'],
			transitions: { discovery: ['new_backend_phase'], new_backend_phase: ['completed'], completed: [] }
		});
		render(WorkflowCard, { props: { execution, phases: [{ name: 'discovery', status: 'active' }] } });
		await screen.findByText('Versioned backend workflow');
		expect(screen.getAllByTestId('phase-step').map((node) => node.dataset.phase))
			.toEqual(['discovery', 'new_backend_phase', 'completed']);
		expect(getWorkflowDefinition).toHaveBeenCalledWith(execution.workflow_id, {
			workspaceId: '', version: execution.workflow_version
		});
	});

	it('keeps persisted phases readable but refuses current topology for an older version', async () => {
		vi.mocked(getWorkflowDefinition).mockResolvedValueOnce({ workflow_id: execution.workflow_id, version: '2.0.0',
			title: 'New workflow', phases: ['replacement'], terminal_phases: [], splits: {}, transitions: { replacement: [] } });
		render(WorkflowCard, { props: { execution, phases: [{ name: 'model_creation', status: 'done' }] } });
		await waitFor(() => expect(screen.getByTestId('workflow-definition-error')).toHaveTextContent('recorded version 1.0.0'));
		expect(screen.getAllByTestId('phase-step').map((node) => node.dataset.phase)).toEqual(['model_creation']);
	});

	it('does not query the API with an invented id when the execution has no workflow id', () => {
		vi.mocked(getWorkflowDefinition).mockClear();
		render(WorkflowCard, {
			props: { execution: { ...execution, workflow_id: '' }, phases }
		});

		expect(getWorkflowDefinition).not.toHaveBeenCalled();
		expect(screen.queryByTestId('workflow-definition-error')).toBeNull();
		expect(screen.getByText('Workflow execution')).toBeInTheDocument();
	});
});
