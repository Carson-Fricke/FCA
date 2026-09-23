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
	context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
	workflowExecutionId: 'exec-1'
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
const listChatSubAgents = vi.hoisted(() => vi.fn());
const listExecutionEvents = vi.hoisted(() => vi.fn());
const listNodeEvents = vi.hoisted(() => vi.fn());

vi.mock('$lib/api', () => ({
	apiBaseUrl: 'http://api.test',
	fixtureMode: false,
	listChatSubAgents,
	listExecutionEvents,
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

function renderSurfaceWith(
	messages: ChatMessage[],
	overrides: Record<string, unknown> = {}
) {
	return render(ChatSurface, {
		props: {
			workspaceId: 'alpha-lab',
			chatId: 'chat-1',
			messages,
			execution,
			onSend: vi.fn(),
			hostId: 'test-surface',
			...overrides
		}
	});
}

describe('ChatSurface deterministic phase run view', () => {
	const trialsEvents = [
		event('workflow.launched', { workflow: { phases: ['hypothesis_creation', 'falsification_trials', 'verdict'] } }, 1),
		event('workflow.started', {}, 2),
		event('sub_agent.spawned', { subAgentId: 'sub-1', parentSubAgentId: null, phase: 'hypothesis_creation', role: 'Researcher', acceptsInput: true }, 3),
		event('workflow.phase', { phase: 'falsification_trials' }, 4),
		event('workflow.progress', { phase: 'falsification_trials', kind: 'tool', text: 'Running the falsification model.' }, 5),
		event('workflow.progress', { phase: 'falsification_trials', kind: 'tool', text: 'Falsification execution completed in state completed.' }, 6),
		event(
			'workflow.phase_result',
			{
				phase: 'falsification_trials',
				summary: 'Ran 1 falsification trial(s): 1 survived, 0 refuted.',
				artifact: {
					type: 'falsification_report',
					content: 'Ran 1 falsification trial(s): 1 survived, 0 refuted.',
					passedCount: 1,
					falsifiedCount: 0,
					totalCount: 1,
					survived: true,
					hypothesis: { claim: 'Five-day momentum in SPY continues.' },
					trials: [
						{
							testId: 't1',
							falsifierId: 'f1',
							runId: 'study-1',
							runState: 'completed',
							detail: null,
							observed: 1.0,
							falsified: false,
							observable: 'sharpe',
							condition: 'lt',
							threshold: 0.0,
							description: 'Run the momentum probe and require a negative Sharpe.'
						}
					],
					controls: [],
					metrics: { sharpe: 1.0 }
				}
			},
			7
		)
	];

	it('opens the run view from the deterministic phase pill and renders live progress and the result', async () => {
		listChatSubAgents.mockResolvedValue([]);
		listExecutionEvents.mockResolvedValue(trialsEvents);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		renderSurface();
		await waitFor(() => expect(screen.getAllByTestId('phase-step')).toHaveLength(3));
		expect(screen.getByText('falsification trials')).toHaveAttribute('data-status', 'active');

		await fireEvent.click(screen.getByText('falsification trials'));
		await waitFor(() => expect(screen.getByTestId('phase-run-view')).toBeInTheDocument());
		// Live progress lines render from the ingested events.
		const lines = screen.getAllByTestId('phase-progress-line');
		expect(lines).toHaveLength(2);
		expect(screen.getByTestId('phase-run-status')).toHaveTextContent('Completed');
		// The result card renders from the phase_result event's artifact.
		await waitFor(() =>
			expect(screen.getByTestId('falsification-report-card')).toBeInTheDocument()
		);
		// A deterministic phase never offers a composer.
		expect(screen.queryByTestId('sub-agent-composer')).toBeNull();
		expect(screen.getByTestId('scope-readonly')).toBeInTheDocument();
		// The scope deep link round-trips through the phase grammar.
		expect(screen.getAllByTestId('scope-crumb')[1]).toHaveTextContent('falsification_trials');
	});

	it('renders the error variant for a failed computation', async () => {
		listChatSubAgents.mockResolvedValue([]);
		listExecutionEvents.mockResolvedValue([
			event('workflow.phase', { phase: 'falsification_trials' }, 1),
			event('workflow.started', {}, 2),
			event(
				'workflow.phase_result',
				{
					phase: 'falsification_trials',
					summary: 'Falsification trials failed: harness exploded',
					artifact: { type: 'error', message: 'harness exploded' }
				},
				3
			)
		]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		renderSurface();
		await waitFor(() =>
			expect(screen.getByText('falsification trials')).toHaveAttribute('data-status', 'active')
		);
		await fireEvent.click(screen.getByText('falsification trials'));

		await waitFor(() => expect(screen.getByTestId('phase-run-error')).toBeInTheDocument());
		expect(screen.getByTestId('phase-run-error')).toHaveTextContent('harness exploded');
		expect(screen.getByTestId('phase-run-status')).toHaveTextContent('Failed');
		expect(screen.queryByTestId('falsification-report-card')).toBeNull();
	});

	it('renders the run view from a phase deep link before any click', async () => {
		listChatSubAgents.mockResolvedValue([]);
		listExecutionEvents.mockResolvedValue(trialsEvents);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		renderSurfaceWith([anchor], {
			initialScope: 'conversation:chat-1/phase:falsification_trials'
		});

		await waitFor(() => expect(screen.getByTestId('phase-run-view')).toBeInTheDocument());
		await waitFor(() =>
			expect(screen.getByTestId('falsification-report-card')).toBeInTheDocument()
		);
	});

	it('does not replay a scope from a previous conversation onto a new chat', async () => {
		// The regression: the page drops its scope grant on conversation
		// switches, and the controller enforces conversation identity - so a
		// scope whose root names another chat can never replay onto this one,
		// even when the stale phase name is legal for the new run.
		listChatSubAgents.mockResolvedValue([]);
		listExecutionEvents.mockResolvedValue([
			event('workflow.launched', { workflow: { phases: ['hypothesis_creation', 'falsification_trials', 'verdict'] } }, 1),
			event('workflow.started', {}, 2)
		]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		// The NEW chat runs the same workflow (so the stale phase name is
		// legal), which is exactly the cross-chat confusion the fix prevents.
		const otherChatExecution = {
			...execution,
			execution_id: 'exec-2',
			chat_id: 'chat-2',
			phase: 'hypothesis_creation'
		};
		const otherChatAnchor: ChatMessage = {
			...anchor,
			chat_id: 'chat-2',
			execution_id: 'exec-2',
			created_at: '2026-09-09T17:14:01Z',
			data: { executionId: 'exec-2', currentPhase: 'hypothesis_creation', status: 'running', workflowId: 'hypothesis-development' }
		};

		renderSurfaceWith([otherChatAnchor], {
			chatId: 'chat-2',
			execution: otherChatExecution,
			hostId: 'stale-scope-switch',
			// The mount-time residue the page keeps passing: the conversation
			// root is the OLD chat. Identity enforcement drops it and
			// everything after it.
			initialScope: 'conversation:chat-1/phase:falsification_trials'
		});

		// The foreign scope was dropped: the root transcript renders.
		await waitFor(() => expect(screen.getByTestId('chat-composer')).toBeInTheDocument());
		expect(screen.queryByTestId('phase-run-view')).toBeNull();
		expect(screen.queryByTestId('scope-stack')).toBeNull();
	});

	it('still opens agent phases as conversations', async () => {
		listChatSubAgents.mockResolvedValue([knownNode]);
		listExecutionEvents.mockResolvedValue([
			event('workflow.phase', { phase: 'research' }, 1)
		]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		renderSurface();
		await waitFor(() => expect(screen.getAllByTestId('phase-step')).toHaveLength(1));
		await fireEvent.click(screen.getByTestId('phase-step'));

		await waitFor(() => expect(screen.getByTestId('scope-transcript')).toBeInTheDocument());
		expect(screen.queryByTestId('phase-run-view')).toBeNull();
	});
});

describe('ChatSurface drill-in', () => {
	it('enters a phase sub-agent, renders its transcript, posts to it, then backs out', async () => {
		listChatSubAgents.mockResolvedValue([knownNode]);
		listExecutionEvents.mockResolvedValue([
			event('workflow.started', { phase: 'research' }, 1)
		]);
		listNodeEvents.mockImplementation(
			async (
				_ws: string,
				_chat: string,
				node: { kind: string; id: string }
			) => (node.kind === 'sub_agent' ? [event('tool.result', { subAgentId: 'sub-1', toolResult }, 2)] : [])
		);
		postNodeMessage.mockResolvedValue({ ...anchor, message_id: 'msg-node-1' });
		window.localStorage.clear();

		renderSurface();
		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalled());
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
				'chat-1',
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
		listChatSubAgents.mockResolvedValue([]);
		listExecutionEvents.mockResolvedValue([]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		renderSurface();
		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());
		expect(screen.getByTestId('chat-composer')).toBeInTheDocument();
	});

	it('does not leak an active execution across separately updating conversations', async () => {
		listChatSubAgents.mockResolvedValue([]);
		listExecutionEvents.mockResolvedValue([]);
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
			expect(listChatSubAgents).toHaveBeenCalledWith('alpha-lab', 'chat-2')
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
		listChatSubAgents.mockImplementation(
			async (_workspaceId: string, chatId: string) =>
				chatId === 'chat-1' ? oldNodes : []
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
		listChatSubAgents.mockResolvedValue([]);
		listExecutionEvents.mockResolvedValue([]);
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
		listChatSubAgents.mockResolvedValue([knownNode]);
		listExecutionEvents.mockResolvedValue([]);
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

		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalled());
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
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			workflowExecutionId: 'exec-1'
		};
		const nodeB = {
			subAgentId: 'sub-b',
			parentSubAgentId: null,
			phase: 'hypothesis',
			role: 'Hypothesis',
			status: 'running',
			acceptsInput: false,
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			workflowExecutionId: 'exec-1'
		};
		listChatSubAgents.mockResolvedValue([nodeA, nodeB]);
		listExecutionEvents.mockResolvedValue([
			event('workflow.launched', { workflow: { phases: ['requested', 'ideation', 'hypothesis', 'report'] } }, 1),
			event('workflow.started', {}, 2),
			event('sub_agent.spawned', { subAgentId: 'sub-a', parentSubAgentId: null, phase: 'ideation', role: 'Ideation' }, 3),
			event('sub_agent.completed', { subAgentId: 'sub-a', parentSubAgentId: null, phase: 'ideation', role: 'Ideation', frozen: true, status: 'completed' }, 4),
			event('workflow.phase', { phase: 'hypothesis' }, 5),
			event('sub_agent.spawned', { subAgentId: 'sub-b', parentSubAgentId: null, phase: 'hypothesis', role: 'Hypothesis' }, 6)
		]);
		listNodeEvents.mockResolvedValue([]);
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
		listChatSubAgents.mockResolvedValue([knownNode]);
		listExecutionEvents.mockResolvedValue([]);
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

	it('drills into a chat-spawned sub-agent from its handle card', async () => {
		const helper = {
			...knownNode,
			subAgentId: 'helper-1',
			phase: '',
			role: 'Research helper',
			status: 'running',
			acceptsInput: true,
			workflowExecutionId: null
		};
		listChatSubAgents.mockResolvedValue([helper]);
		listExecutionEvents.mockResolvedValue([]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		const spawnResult = {
			tool: 'spawn_sub_agent',
			callId: 'call-spawn-1',
			ok: true,
			summary: 'Sub-agent spawned; it runs asynchronously.',
			datumType: 'sub_agent_handle',
			structured: {
				sub_agent_id: 'helper-1',
				status: 'spawned',
				kind: 'conversation',
				role: 'research',
				task: 'Check the regime'
			},
			text: 'Sub-agent spawned; it runs asynchronously.',
			failure: null
		};
		const base = {
			chat_id: 'chat-1',
			workspace_id: 'alpha-lab',
			state: 'accepted' as const,
			attachment_ids: [] as string[],
			updated_at: null,
			execution_id: null,
			intent_epoch: 0
		};

		render(ChatSurface, {
			props: {
				workspaceId: 'alpha-lab',
				chatId: 'chat-1',
				messages: [
					{
						...base,
						message_id: 'm1',
						actor_id: 'operator',
						role: 'user',
						body: 'please research',
						created_at: '2026-09-08T17:14:01Z'
					},
					{
						...base,
						message_id: 'm2',
						actor_id: 'thesis-agent',
						role: 'tool',
						body: spawnResult.summary,
						created_at: '2026-09-08T17:14:02Z',
						tool_result: spawnResult
					},
					{
						...base,
						message_id: 'm3',
						actor_id: 'operator',
						role: 'user',
						body: 'Initial task prompt',
						created_at: '2026-09-08T17:14:03Z',
						kind: 'sub_agent' as const,
						data: { subAgentId: 'helper-1' }
					},
					{
						...base,
						message_id: 'm4',
						actor_id: 'helper-1',
						role: 'assistant',
						body: 'Working on it now.',
						created_at: '2026-09-08T17:14:04Z',
						kind: 'sub_agent' as const,
						data: { subAgentId: 'helper-1' }
					}
				],
				execution: null,
				onSend: vi.fn(),
				hostId: 'chat-spawn'
			}
		});

		const card = await waitFor(() => screen.getByTestId('sub-agent-card'));
		expect(card).toHaveAttribute('data-sub-agent', 'helper-1');
		expect(screen.getByTestId('sub-agent-card-task')).toHaveTextContent('Check the regime');
		// The datum upgrades to the durable node's live status.
		await waitFor(() => expect(card).toHaveAttribute('data-status', 'running'));

		await fireEvent.click(card);
		await waitFor(() => expect(screen.getByTestId('scope-transcript')).toBeInTheDocument());
		expect(screen.getAllByTestId('scope-crumb')).toHaveLength(2);

		// The nested surface opens on the initial prompt as the first user
		// message, with the agent's reply after it.
		const entries = screen.getAllByTestId('chat-message');
		expect(entries[0]).toHaveAttribute('data-role', 'user');
		expect(entries[0]).toHaveTextContent('Initial task prompt');
		expect(entries[1]).toHaveAttribute('data-role', 'assistant');
		expect(entries[1]).toHaveTextContent('Working on it now.');
		expect(screen.getByTestId('sub-agent-composer')).toBeInTheDocument();
	});

	it('hides the workflow node and enters the phase agent, then backs out to the anchor', async () => {
		const anchorNode = {
			subAgentId: 'anchor-1',
			parentSubAgentId: null,
			phase: '',
			role: 'Researcher',
			status: 'running',
			acceptsInput: true,
			context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			workflowExecutionId: 'exec-1'
		};
		const workflowNode = {
			subAgentId: 'wf-1',
			parentSubAgentId: 'anchor-1',
			phase: 'requested',
			role: 'workflow-node',
			status: 'spawned',
			acceptsInput: false,
			context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			workflowExecutionId: 'exec-1'
		};
		const phaseAgent = {
			subAgentId: 'phase-1',
			parentSubAgentId: 'wf-1',
			phase: 'research',
			role: 'Phase researcher',
			status: 'running',
			acceptsInput: false,
			context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			workflowExecutionId: 'exec-1'
		};
		listChatSubAgents.mockResolvedValue([anchorNode, workflowNode, phaseAgent]);
		listExecutionEvents.mockResolvedValue([]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		renderSurface();
		await waitFor(() => expect(screen.getByTestId('workflow-card')).toBeInTheDocument());
		await waitFor(() => expect(screen.getAllByTestId('phase-step')).toHaveLength(1));
		expect(screen.getByTestId('phase-step')).toHaveAttribute('data-phase', 'research');
		expect(screen.queryByText('requested')).toBeNull();
		expect(screen.queryByText('workflow-node')).toBeNull();

		await fireEvent.click(screen.getByTestId('phase-step'));
		await waitFor(() => expect(screen.getByTestId('scope-transcript')).toBeInTheDocument());
		expect(screen.getAllByTestId('scope-crumb')).toHaveLength(3);
		expect(listNodeEvents).toHaveBeenCalledWith(
			'alpha-lab',
			'chat-1',
			{ kind: 'sub_agent', id: 'phase-1' },
			{ includeDescendants: false }
		);
		expect(listNodeEvents).not.toHaveBeenCalledWith(
			'alpha-lab',
			'chat-1',
			expect.objectContaining({ id: 'wf-1' }),
			expect.anything()
		);

		await fireEvent.click(screen.getByTestId('scope-back'));
		await waitFor(() => expect(screen.getAllByTestId('scope-crumb')).toHaveLength(2));
		expect(screen.getAllByTestId('scope-crumb')[1]).toHaveTextContent('Researcher');
		expect(screen.getByTestId('chat-surface')).not.toHaveTextContent('workflow-node');
	});

	it('does not change the selection when a workflow launch arrives', async () => {
		const anchorNode = {
			subAgentId: 'anchor-1',
			parentSubAgentId: null,
			phase: '',
			role: 'Researcher',
			status: 'running',
			acceptsInput: true,
			context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			workflowExecutionId: 'exec-1'
		};
		const workflowNode = {
			subAgentId: 'wf-1',
			parentSubAgentId: 'anchor-1',
			phase: 'requested',
			role: 'workflow-node',
			status: 'spawned',
			acceptsInput: false,
			context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			workflowExecutionId: 'exec-1'
		};
		const phaseAgent = {
			subAgentId: 'phase-1',
			parentSubAgentId: 'wf-1',
			phase: 'research',
			role: 'Phase researcher',
			status: 'running',
			acceptsInput: false,
			context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			workflowExecutionId: 'exec-1'
		};
		listChatSubAgents.mockResolvedValue([anchorNode, workflowNode, phaseAgent]);
		listExecutionEvents.mockResolvedValue([]);
		listNodeEvents.mockResolvedValue([]);
		window.localStorage.clear();

		const view = render(ChatSurface, {
			props: {
				workspaceId: 'alpha-lab',
				chatId: 'chat-1',
				messages: [],
				execution: null,
				onSend: vi.fn(),
				hostId: 'launch-selection'
			}
		});
		expect(screen.queryByTestId('scope-stack')).toBeNull();

		await view.rerender({
			messages: [anchor],
			initialScope: 'conversation:chat-1/sub_agent:anchor-1'
		});

		await waitFor(() => expect(screen.getAllByTestId('scope-crumb')).toHaveLength(2));
		expect(screen.getByTestId('scope-stack')).toHaveAttribute('data-depth', '2');
		expect(screen.queryByText('workflow-node')).toBeNull();
		expect(listNodeEvents).not.toHaveBeenCalledWith(
			'alpha-lab',
			'chat-1',
			expect.objectContaining({ id: 'wf-1' }),
			expect.anything()
		);
	});
});
