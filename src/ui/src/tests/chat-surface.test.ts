import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import type { ExecutionEvent } from '$lib/types';

const knownNode = {
	subAgentId: 'sub-1',
	parentSubAgentId: null,
	phase: 'research',
	role: 'Researcher',
	status: 'needs_input',
	acceptsInput: true,
	context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } }
};

const toolResult = {
	tool: 'run_backtest',
	callId: 'call-1',
	ok: true,
	summary: 'done',
	structured: {},
	text: '',
	failure: null
};

function event(
	eventType: ExecutionEvent['eventType'],
	payload: Record<string, unknown>,
	cursor: number
): ExecutionEvent {
	return {
		actorId: 'thesis-agent',
		actorScope: 'workflow',
		correlationId: `corr-${cursor}`,
		cursor,
		eventId: `evt-${cursor}`,
		eventType,
		executionId: 'exec-1',
		intentEpoch: 0,
		lateResult: false,
		occurredAt: `2026-09-08T17:14:0${cursor}Z`,
		payload,
		workspaceId: 'alpha-lab'
	};
}

const postNodeMessage = vi.hoisted(() => vi.fn());
const listExecutionSubAgents = vi.hoisted(() => vi.fn());
const listNodeEvents = vi.hoisted(() => vi.fn());

vi.mock('$lib/api', () => ({
	apiBaseUrl: 'http://api.test',
	fixtureMode: false,
	listExecutionSubAgents,
	listNodeEvents,
	postNodeMessage
}));

vi.mock('$lib/sse', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/sse')>();
	class FakeExecutionEventStream {
		async start(): Promise<void> {}
		stop(): void {}
	}
	return { ...actual, ExecutionEventStream: FakeExecutionEventStream };
});

import ChatSurface from '$lib/components/chat/ChatSurface.svelte';
import { WorkflowScopeController } from '$lib/workflow-scope.svelte';
import type { ChatMessage, WorkflowExecutionRecord } from '$lib/types';

const execution: WorkflowExecutionRecord = {
	execution_id: 'exec-1',
	chat_id: 'chat-1',
	trigger_message_id: 'msg-1',
	workflow_id: 'hypothesis-development',
	workflow_version: '1.0.0',
	requested_by: 'user-alex',
	state: 'running',
	phase: 'research',
	intent_epoch: 0,
	steering_message_ids: [],
	created_at: '2026-09-08T17:14:01Z'
};

const anchor: ChatMessage = {
	message_id: 'msg-wf',
	chat_id: 'chat-1',
	workspace_id: 'alpha-lab',
	actor_id: 'thesis-workflow',
	role: 'system',
	body: 'Hypothesis development',
	state: 'accepted',
	attachment_ids: [],
	created_at: '2026-09-08T17:14:01Z',
	updated_at: null,
	execution_id: 'exec-1',
	intent_epoch: 0,
	kind: 'workflow',
	data: { executionId: 'exec-1', currentPhase: 'research', status: 'running', workflowId: 'hypothesis-development' }
};

function renderSurface() {
	return render(ChatSurface, {
		props: {
			workspaceId: 'alpha-lab',
			chatId: 'chat-1',
			messages: [anchor],
			execution,
			onSend: vi.fn(),
			hostId: 'test-surface'
		}
	});
}

describe('ChatSurface drill-in', () => {
	it('enters a phase sub-agent, renders its transcript, posts to it, then backs out', async () => {
		listExecutionSubAgents.mockImplementation(
			async (
				_ws: string,
				_exec: string,
				options: { parentSubAgentId?: string | null } = {}
			) => (options.parentSubAgentId ? [] : [knownNode])
		);
		listNodeEvents.mockImplementation(
			async (_ws: string, _exec: string, node: { kind: string; id: string }) =>
				node.kind === 'execution'
					? [event('workflow.started', { phase: 'research' }, 1)]
					: [event('tool.result', { subAgentId: 'sub-1', toolResult }, 2)]
		);
		postNodeMessage.mockResolvedValue({ ...anchor, message_id: 'msg-node-1' });
		window.localStorage.clear();

		renderSurface();
		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());
		await waitFor(() => expect(listExecutionSubAgents).toHaveBeenCalled());
		await new Promise((resolve) => setTimeout(resolve, 0));

		await fireEvent.click(screen.getByTestId('workflow-card-body'));

		await waitFor(() => expect(screen.getByTestId('scope-transcript')).toBeInTheDocument());
		expect(screen.getAllByTestId('scope-crumb')).toHaveLength(2);
		await waitFor(() => expect(screen.getByTestId('tool-result-card')).toBeInTheDocument());
		expect(screen.getByTestId('sub-agent-composer')).toBeInTheDocument();

		const input = screen.getByTestId('sub-agent-composer-input');
		await fireEvent.input(input, { target: { value: 'please continue' } });
		await fireEvent.click(screen.getByTestId('sub-agent-composer-send'));
		await waitFor(() =>
			expect(postNodeMessage).toHaveBeenCalledWith(
				'alpha-lab',
				'exec-1',
				expect.objectContaining({ kind: 'sub_agent', id: 'sub-1' }),
				{ body: 'please continue' }
			)
		);

		await fireEvent.click(screen.getByTestId('scope-back'));
		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());
		expect(screen.getByTestId('chat-composer')).toBeInTheDocument();

		// Re-entering the same phase must render the same history.
		await fireEvent.click(screen.getByTestId('workflow-card-body'));
		await waitFor(() => expect(screen.getByTestId('scope-transcript')).toBeInTheDocument());
		expect(await screen.findByTestId('tool-result-card')).toBeInTheDocument();
		expect(screen.getByTestId('sub-agent-composer')).toBeInTheDocument();
	});

	it('keeps the root composer available while a workflow scope is active', async () => {
		listExecutionSubAgents.mockResolvedValue([]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		renderSurface();
		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());
		expect(screen.getByTestId('chat-composer')).toBeInTheDocument();
	});

	it('does not leak an active execution across separately updating conversations', async () => {
		listExecutionSubAgents.mockResolvedValue([]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();
		const newerAnchor = {
			...anchor,
			message_id: 'msg-wf-2',
			chat_id: 'chat-2',
			execution_id: 'exec-2',
			created_at: '2026-09-08T17:15:01Z',
			data: { ...anchor.data, executionId: 'exec-2', currentPhase: 'hypothesis_creation' }
		};
		const completed = {
			...event('workflow.completed', {}, 3),
			executionId: 'exec-2'
		};

		render(ChatSurface, {
			props: {
				workspaceId: 'alpha-lab',
				chatId: 'chat-2',
				messages: [newerAnchor],
				events: [completed],
				// Simulate the previous conversation's fetch resolving during a switch.
				execution,
				onSend: vi.fn(),
				hostId: 'separate-workflows'
			}
		});

		await waitFor(() =>
			expect(listExecutionSubAgents).toHaveBeenCalledWith('alpha-lab', 'exec-2')
		);
		const cards = await screen.findAllByTestId('workflow-card');
		expect(cards).toHaveLength(1);
		expect(cards[0]).toHaveTextContent('completed');
		expect(cards[0]).toHaveTextContent('exec-2');
		expect(cards[0]).not.toHaveTextContent('exec-1');
	});

	it('discards a delayed sub-agent listing from the previous execution', async () => {
		let resolveOld: (nodes: typeof knownNode[]) => void = () => {};
		const oldNodes = new Promise<typeof knownNode[]>((resolve) => {
			resolveOld = resolve;
		});
		listExecutionSubAgents.mockImplementation(
			async (_workspaceId: string, executionId: string) =>
				executionId === 'exec-1' ? oldNodes : []
		);
		const controller = new WorkflowScopeController('alpha-lab', 'async-switch');
		controller.attach('exec-1', execution, { externalEvents: true });
		controller.attach('exec-2', { ...execution, execution_id: 'exec-2', chat_id: 'chat-2' }, {
			externalEvents: true
		});

		resolveOld([knownNode]);
		await controller.ensureLoaded();
		await oldNodes;

		expect(controller.knownNodes).toEqual([]);
		controller.detach();
	});

	it('does not surface sub-agent replies in the root transcript', async () => {
		listExecutionSubAgents.mockResolvedValue([]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		render(ChatSurface, {
			props: {
				workspaceId: 'alpha-lab',
				chatId: 'chat-1',
				messages: [
					anchor,
					{
						...anchor,
						message_id: 'thread-message',
						role: 'user',
						body: 'thread-only reply',
						kind: 'sub_agent',
						data: { subAgentId: 'sub-1' }
					}
				],
				execution,
				onSend: vi.fn(),
				hostId: 'root-filter'
			}
		});

		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());
		expect(screen.queryByText('thread-only reply')).toBeNull();
	});

	it('drills in from a live workflow anchor before the execution record is fetched', async () => {
		listExecutionSubAgents.mockImplementation(
			async (
				_ws: string,
				_exec: string,
				options: { parentSubAgentId?: string | null } = {}
			) => (options.parentSubAgentId ? [] : [knownNode])
		);
		listNodeEvents.mockResolvedValue([]);
		postNodeMessage.mockResolvedValue({ ...anchor, message_id: 'msg-node-2' });
		window.localStorage.clear();

		render(ChatSurface, {
			props: {
				workspaceId: 'alpha-lab',
				chatId: 'chat-1',
				messages: [anchor],
				execution: null,
				onSend: vi.fn(),
				hostId: 'anchor-only'
			}
		});

		await waitFor(() => expect(listExecutionSubAgents).toHaveBeenCalled());
		await new Promise((resolve) => setTimeout(resolve, 0));
		await fireEvent.click(screen.getByTestId('workflow-card-body'));

		await waitFor(() => expect(screen.getByTestId('scope-transcript')).toBeInTheDocument());
		expect(screen.getAllByTestId('scope-crumb')).toHaveLength(2);
		expect(screen.getByTestId('sub-agent-composer')).toBeInTheDocument();
	});

	it('lists every orchestrator phase and enters a completed one read-only', async () => {
		const nodeA = {
			subAgentId: 'sub-a',
			parentSubAgentId: null,
			phase: 'ideation',
			role: 'Ideation',
			status: 'completed',
			acceptsInput: false,
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } }
		};
		const nodeB = {
			subAgentId: 'sub-b',
			parentSubAgentId: null,
			phase: 'hypothesis',
			role: 'Hypothesis',
			status: 'running',
			acceptsInput: false,
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } }
		};
		listExecutionSubAgents.mockResolvedValue([nodeA, nodeB]);
		listNodeEvents.mockImplementation(
			async (_ws: string, _exec: string, node: { kind: string }) =>
				node.kind === 'execution'
					? [
							event('workflow.launched', { workflow: { phases: ['requested', 'ideation', 'hypothesis', 'report'] } }, 1),
							event('workflow.started', {}, 2),
							event('sub_agent.spawned', { subAgentId: 'sub-a', parentSubAgentId: null, phase: 'ideation', role: 'Ideation' }, 3),
							event('sub_agent.completed', { subAgentId: 'sub-a', parentSubAgentId: null, phase: 'ideation', role: 'Ideation', frozen: true, status: 'completed' }, 4),
							event('workflow.phase', { phase: 'hypothesis' }, 5),
							event('sub_agent.spawned', { subAgentId: 'sub-b', parentSubAgentId: null, phase: 'hypothesis', role: 'Hypothesis' }, 6)
						]
					: []
		);
		window.localStorage.clear();

		renderSurface();
		await waitFor(() => expect(screen.getAllByTestId('phase-step')).toHaveLength(3));
		expect(screen.getByText('ideation')).toHaveAttribute('data-status', 'done');
		expect(screen.getByText('hypothesis')).toHaveAttribute('data-status', 'active');
		expect(screen.getByText('report')).toHaveAttribute('data-status', 'pending');
		expect(screen.getByText('report')).toBeDisabled();

		await fireEvent.click(screen.getByText('ideation'));
		await waitFor(() => expect(screen.getByTestId('scope-readonly')).toBeInTheDocument());
		expect(screen.queryByTestId('sub-agent-composer')).toBeNull();
	});

	it('drills into a phase from externally-ingested timeline events', async () => {
		listExecutionSubAgents.mockResolvedValue([knownNode]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();
		const spawn = event(
			'sub_agent.spawned',
			{
				subAgentId: 'sub-1',
				parentSubAgentId: null,
				phase: 'research',
				role: 'Researcher',
				acceptsInput: true
			},
			1
		);

		render(ChatSurface, {
			props: {
				workspaceId: 'alpha-lab',
				chatId: 'chat-1',
				messages: [anchor],
				execution,
				events: [spawn],
				onSend: vi.fn(),
				hostId: 'external'
			}
		});

		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());
		await waitFor(() => expect(screen.getAllByTestId('phase-step')).toHaveLength(1));
		await fireEvent.click(screen.getByTestId('workflow-card-body'));

		await waitFor(() => expect(screen.getByTestId('scope-transcript')).toBeInTheDocument());
		expect(screen.getAllByTestId('scope-crumb')).toHaveLength(2);
	});
});
