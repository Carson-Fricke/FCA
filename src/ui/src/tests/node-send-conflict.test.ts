import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage, ExecutionEvent, SubAgentNode, TimelineItem, WorkflowExecutionRecord } from '$lib/types';
import { eventFixture, timelineFixture, transcriptFixture } from './timeline-fixture';
import type { TimelinePage } from '$lib/api';

const fixture = vi.hoisted(() => ({ terminal: false, cancelled: false, backfill: [] as TimelineItem[] }));
vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, fixtureMode: false,
		listWorkflowMetadata: vi.fn(async () => ({ items: [], after: 0 })),
		getWorkflowDefinition: vi.fn(async () => { throw new Error('Unavailable'); }),
		listChatSubAgents: vi.fn(async () => [{ subAgentId: 'subagent-a', parentSubAgentId: null,
			phase: 'model_creation', role: 'researcher', phaseExecutor: true,
			status: fixture.cancelled ? 'cancelled' : fixture.terminal ? 'completed' : 'running',
			acceptsInput: !fixture.terminal, workflowExecutionId: 'exec-1',
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: true, tools: ['validate_study'] } } }]),
		listTimelinePage: vi.fn(async (_ws, _chat, thread) => ({ items: thread === 'subagent-a' ? transcriptFixture(fixture.backfill) : [], nextBefore: null,
			after: Math.max(0, ...fixture.backfill.map((item) => item.seq)) })),
		postNodeMessage: vi.fn(async () => { throw new actual.ThesisApiError(409, '/messages'); }) };
});
vi.mock('$lib/sse', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/sse')>(),
	TimelineStream: class { async start() {} stop() {} }
}));

import ChatSurface from '$lib/components/chat/ChatSurface.svelte';
import { listChatSubAgents, listTimelinePage, postNodeMessage } from '$lib/api';
import { TimelineController } from '$lib/timeline.svelte';
import { WorkflowScopeController } from '$lib/workflow-scope.svelte';

const execution: WorkflowExecutionRecord = {
	execution_id: 'exec-1', chat_id: 'chat-1', trigger_message_id: 'msg-1', workflow_id: 'hypothesis-development',
	conversation_id: 'chat-1', root_conversation_id: 'chat-1', parent_conversation_id: null,
	workflow_version: 'v1', requested_by: 'operator', state: 'running', control_sequence: 0, active_phases: ['model_creation'], intent_epoch: 0,
	steering_message_ids: [], created_at: '2026-01-01T00:00:00Z'
};
function message(id: string, role: ChatMessage['role'] = 'user'): ChatMessage {
	return { message_id: id, chat_id: 'chat-1', workspace_id: 'ws-1', actor_id: 'operator', role, body: id,
		conversation_id: 'subagent-a', root_conversation_id: 'chat-1', parent_conversation_id: 'chat-1',
		state: 'accepted', attachment_ids: [], created_at: '2026-01-01T00:00:01Z', updated_at: null,
		execution_id: null, intent_epoch: 0, kind: 'sub_agent', data: { subAgentId: 'subagent-a' } };
}
function surface(messages: ChatMessage[] = []) {
	const timeline = new TimelineController();
	timeline.start('ws-1', 'chat-1');
	timeline.merge(timelineFixture(messages).items);
	timeline.merge([{ id: 'execution', kind: 'execution', seq: 0, chatId: 'chat-1', workspaceId: 'ws-1',
		ts: execution.created_at, payload: execution } as TimelineItem]);
	const view = render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline,
		onSend: vi.fn(), initialScope: 'conversation:chat-1/sub_agent:subagent-a' } });
	return { view, timeline };
}
beforeEach(() => { fixture.terminal = false; fixture.cancelled = false; fixture.backfill = []; vi.clearAllMocks(); });

describe('node input and canonical history', () => {
	it('coalesces loopback triggers during a stale REST request and opens the composer without a new spawn', async () => {
		const completed: SubAgentNode = { subAgentId: 'subagent-a', parentSubAgentId: null, phase: 'model_creation',
			role: 'researcher', status: 'completed', acceptsInput: false, workflowExecutionId: 'exec-1',
			createdAt: '2026-01-01T00:00:01Z', updatedAt: '2026-01-01T00:00:03Z', phaseAttemptId: 'a',
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } } };
		const running = { ...completed, status: 'running' as const, acceptsInput: true, phaseAttemptId: 'b',
			createdAt: '2026-01-01T00:00:05Z', updatedAt: '2026-01-01T00:00:07Z' };
		let release!: (nodes: SubAgentNode[]) => void;
		vi.mocked(listChatSubAgents).mockResolvedValueOnce([completed])
			.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }))
			.mockResolvedValueOnce([running]);
		const { timeline } = surface();
		await screen.findByTestId('scope-readonly');
		const prior = eventFixture({ eventId: 'prior-completed', eventType: 'sub_agent.completed', cursor: 3, seq: 3,
			occurredAt: completed.updatedAt ?? '2026-01-01T00:00:03Z', executionId: 'exec-1',
			payload: { subAgentId: completed.subAgentId, phaseAttemptId: 'a' } });
		timeline.merge(timelineFixture([], [prior]).items);
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalledTimes(2));
		const loopback = { ...prior, eventId: 'loopback', eventType: 'workflow.phase' as const, cursor: 5, seq: 5,
			occurredAt: running.createdAt, payload: { phase: 'model_creation', phaseAttemptId: 'b' } };
		timeline.merge(timelineFixture([], [loopback]).items);
		await waitFor(() => expect(screen.getByTestId('scope-readonly')).toBeInTheDocument());
		const inputRequest = { ...prior, eventId: 'new-input', eventType: 'sub_agent.needs_input' as const, cursor: 6, seq: 6,
			occurredAt: '2026-01-01T00:00:06Z', payload: { subAgentId: completed.subAgentId, phaseAttemptId: 'b', acceptsInput: true } };
		timeline.merge(timelineFixture([], [inputRequest]).items);
		await waitFor(() => expect(screen.getByTestId('scope-readonly')).toBeInTheDocument());
		expect(listChatSubAgents).toHaveBeenCalledTimes(2);
		release([completed]);
		const input = await screen.findByTestId('sub-agent-composer-input');
		expect(listChatSubAgents).toHaveBeenCalledTimes(3);
		await fireEvent.input(input, { target: { value: 'feedback on the reopened attempt' } });
		expect(input).not.toBeDisabled();
		expect(screen.getByTestId('sub-agent-composer-send')).not.toBeDisabled();
	});
	it('runs a trailing refresh when reentry arrives during the initial listing', async () => {
		let release!: (nodes: SubAgentNode[]) => void;
		const completed: SubAgentNode = { subAgentId: 'subagent-a', parentSubAgentId: null, phase: 'model_creation',
			role: 'researcher', status: 'completed', acceptsInput: false, workflowExecutionId: 'exec-1',
			createdAt: '2026-01-01T00:00:01Z', updatedAt: '2026-01-01T00:00:03Z',
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } } };
		vi.mocked(listChatSubAgents).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }))
			.mockResolvedValueOnce([{ ...completed, status: 'running', acceptsInput: true,
				createdAt: '2026-01-01T00:00:05Z', updatedAt: '2026-01-01T00:00:06Z' }]);
		const { timeline } = surface();
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalledTimes(1));
		const loopback = eventFixture({ eventId: 'loopback', eventType: 'workflow.phase', cursor: 5, seq: 5,
			occurredAt: '2026-01-01T00:00:05Z', executionId: 'exec-1', payload: { phase: 'model_creation' } });
		timeline.merge(timelineFixture([], [loopback]).items);
		await waitFor(() => expect(screen.getByTestId('scope-readonly')).toBeInTheDocument());
		expect(listChatSubAgents).toHaveBeenCalledTimes(1);
		release([completed]);
		await screen.findByTestId('sub-agent-composer-input');
		expect(listChatSubAgents).toHaveBeenCalledTimes(2);
	});
	it('clears queued refreshes on detach and ignores the previous request completion', async () => {
		const timeline = new TimelineController();
		const controller = new WorkflowScopeController('ws-1', 'refresh-detach', timeline);
		controller.attachChat('chat-1');
		await controller.ensureLoaded();
		let release!: (nodes: SubAgentNode[]) => void;
		vi.mocked(listChatSubAgents).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
		controller.refreshNodes();
		controller.refreshNodes();
		controller.refreshNodes();
		expect(listChatSubAgents).toHaveBeenCalledTimes(2);
		controller.detach();
		controller.attachChat('chat-2');
		await controller.ensureLoaded();
		release([]);
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(listChatSubAgents).toHaveBeenCalledTimes(3);
		expect(controller.knownNodes).toHaveLength(1);
		controller.detach();
	});
	it.each(['spawned', 'running', 'needs_input'] as const)('keeps the reopened %s attempt composer writable after completed history', async (status) => {
		const snapshot: SubAgentNode = { subAgentId: 'subagent-a', parentSubAgentId: null, phase: 'model_creation',
			role: 'researcher', status, acceptsInput: true, workflowExecutionId: 'exec-1',
			createdAt: '2026-01-01T00:00:05Z', updatedAt: '2026-01-01T00:00:06Z',
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } } };
		vi.mocked(listChatSubAgents).mockResolvedValueOnce([snapshot]);
		const completed = eventFixture({ eventId: 'old-completed', eventType: 'sub_agent.completed', cursor: 3, seq: 3,
			occurredAt: '2026-01-01T00:00:03Z', executionId: 'exec-1', payload: { subAgentId: 'subagent-a' } });
		fixture.backfill = timelineFixture([], [completed]).items;
		surface([message('new prompt')]);
		const input = await screen.findByTestId('sub-agent-composer-input');
		await screen.findByTestId('scope-pending');
		await fireEvent.input(input, { target: { value: 'midturn feedback' } });
		expect(input).not.toBeDisabled();
		expect(screen.getByTestId('sub-agent-composer-send')).not.toBeDisabled();
	});
	it('reopens the composer from newer spawn events despite a stale terminal REST refresh', async () => {
		const snapshot: SubAgentNode = { subAgentId: 'subagent-a', parentSubAgentId: null, phase: 'model_creation',
			role: 'researcher', status: 'completed', acceptsInput: false, workflowExecutionId: 'exec-1',
			createdAt: '2026-01-01T00:00:01Z', updatedAt: '2026-01-01T00:00:03Z',
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } } };
		vi.mocked(listChatSubAgents).mockResolvedValueOnce([snapshot]).mockResolvedValueOnce([snapshot]);
		const { timeline } = surface();
		await screen.findByTestId('scope-readonly');
		const spawn = eventFixture({ eventId: 'new-spawn', eventType: 'sub_agent.spawned', cursor: 5, seq: 5,
			occurredAt: '2026-01-01T00:00:05Z', executionId: 'exec-1',
			payload: { subAgentId: 'subagent-a', acceptsInput: true, phase: 'model_creation' } });
		timeline.merge(timelineFixture([], [spawn]).items);
		const input = await screen.findByTestId('sub-agent-composer-input');
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalledTimes(2));
		await fireEvent.input(input, { target: { value: 'reentry feedback' } });
		expect(input).not.toBeDisabled();
		expect(screen.getByTestId('sub-agent-composer-send')).not.toBeDisabled();
	});
	it('keeps the composer closed after cancellation and later same-attempt input events', async () => {
		const snapshot: SubAgentNode = { subAgentId: 'subagent-a', parentSubAgentId: null, phase: 'model_creation',
			role: 'researcher', status: 'running', acceptsInput: true, workflowExecutionId: 'exec-1',
			createdAt: '2026-01-01T00:00:01Z', updatedAt: '2026-01-01T00:00:02Z', phaseAttemptId: 'a',
			context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } } };
		vi.mocked(listChatSubAgents).mockResolvedValueOnce([snapshot]).mockResolvedValueOnce([snapshot]);
		const { timeline } = surface();
		await screen.findByTestId('sub-agent-composer-input');
		const cancel = eventFixture({ eventId: 'cancel', eventType: 'sub_agent.cancelled', cursor: 3, seq: 3,
			occurredAt: '2026-01-01T00:00:03Z', executionId: 'exec-1',
			payload: { subAgentId: 'subagent-a', phaseAttemptId: 'a', acceptsInput: false } });
		const late = { ...cancel, eventId: 'late-input', eventType: 'sub_agent.needs_input' as const, cursor: 4, seq: 4,
			occurredAt: '2026-01-01T00:00:04Z', payload: { ...cancel.payload, acceptsInput: true } };
		timeline.merge(timelineFixture([], [cancel, late]).items);
		await screen.findByTestId('scope-readonly');
		expect(screen.queryByTestId('sub-agent-composer-input')).toBeNull();
	});
	it('surfaces a 409, retains the draft across navigation and refreshes capabilities', async () => {
		surface();
		const input = await screen.findByTestId('sub-agent-composer-input');
		await fireEvent.input(input, { target: { value: 'revise the model' } });
		await fireEvent.click(screen.getByTestId('sub-agent-composer-send'));
		expect(await screen.findByTestId('node-send-error')).toHaveTextContent('no longer accepts input');
		await waitFor(() => expect(input).toHaveValue('revise the model'));
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalledTimes(2));
		await fireEvent.click(screen.getByTestId('scope-back'));
		await fireEvent.click(screen.getByTitle('Enter active model_creation orchestrator'));
		expect(await screen.findByTestId('sub-agent-composer-input')).toHaveValue('revise the model');
	});
	it('keeps mid-turn input writable, blocks only duplicate POSTs and clears queued feedback by sequence', async () => {
		let finish!: (value: ChatMessage) => void;
		vi.mocked(postNodeMessage).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		const { timeline } = surface([message('prompt')]);
		const input = await screen.findByTestId('sub-agent-composer-input');
		expect(screen.getByTestId('scope-pending')).toBeInTheDocument();
		await fireEvent.input(input, { target: { value: 'steer now' } });
		const send = screen.getByTestId('sub-agent-composer-send');
		await fireEvent.click(send);
		expect(send).toBeDisabled();
		expect(input).not.toBeDisabled();
		await fireEvent.click(send);
		expect(postNodeMessage).toHaveBeenCalledTimes(1);
		await fireEvent.input(input, { target: { value: 'next thought' } });
		finish(message('accepted'));
		await waitFor(() => expect(send).not.toBeDisabled());
		expect(input).toHaveValue('next thought');
		expect(screen.getByTestId('node-feedback-queued')).toHaveTextContent('queued for delivery');
		timeline.merge(timelineFixture([message('prompt'), message('accepted'),
			{ ...message('reply', 'assistant'), created_at: '2025-01-01T00:00:00Z' }]).items);
		await waitFor(() => expect(screen.queryByTestId('node-feedback-queued')).toBeNull());
	});
	it('reconciles a terminal capability after rejection', async () => {
		surface();
		const input = await screen.findByTestId('sub-agent-composer-input');
		await fireEvent.input(input, { target: { value: 'keep this draft' } });
		fixture.terminal = true;
		await fireEvent.click(screen.getByTestId('sub-agent-composer-send'));
		await screen.findByTestId('scope-readonly');
		expect(screen.queryByTestId('sub-agent-composer-input')).toBeNull();
	});
	it.each(['completed', 'cancelled'])('renders %s history read-only and unmatched tools as unknown', async (status) => {
		fixture.terminal = status === 'completed'; fixture.cancelled = status === 'cancelled';
		surface([{ ...message('envelope', 'assistant'), data: { subAgentId: 'subagent-a',
			sessionMessage: { message_id: 'envelope', direction: 'outgoing', position: 0,
				content: { text: 'envelope', source: 'agent',
					requests: [{ id: 'call', name: 'run_study', arguments: '{}' }], correlation: null, result: null } } } }]);
		await waitFor(() => expect(screen.getByTestId('tool-result-status')).toHaveTextContent('unknown_outcome'));
		expect(screen.queryByTestId('sub-agent-composer-input')).toBeNull();
	});
	it('uses loaded history for pending state and deduplicates live/history by timeline identity', async () => {
		const messages = Array.from({ length: 200 }, (_, index) => message(`history-${index}`, 'assistant'));
		fixture.backfill = timelineFixture(messages).items;
		surface([messages[199]]);
		await screen.findByText('history-0');
		expect(screen.getAllByTestId('chat-message')).toHaveLength(200);
		expect(screen.queryByTestId('scope-pending')).toBeNull();
		expect(listTimelinePage).toHaveBeenCalledWith('ws-1', 'chat-1', 'subagent-a', null, expect.any(AbortSignal));
	}, 20_000);
	it('cancels scope history and ignores late replies, including same-chat reattachment', async () => {
		let finish!: (page: TimelinePage) => void;
		const timeline = new TimelineController();
		timeline.start('ws-1', 'chat-1');
		await timeline.loadHistory();
		vi.mocked(listTimelinePage).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		const controller = new WorkflowScopeController('ws-1', 'history', timeline);
		const options = { scope: 'conversation:chat-1/sub_agent:subagent-a' };
		controller.attachChat('chat-1', options);
		await controller.ensureLoaded();
		const signal = vi.mocked(listTimelinePage).mock.calls.at(-1)![4]!;
		controller.back();
		expect(signal.aborted).toBe(true);
		controller.attachChat('chat-1', options);
		await controller.ensureLoaded();
		finish({ items: transcriptFixture(timelineFixture([message('stale')]).items), nextBefore: null, after: 1 });
		await Promise.resolve();
		expect(timeline.messagesFor('subagent-a')).toEqual([]);
		controller.detach(); timeline.stop();
	});
	it('retains a partial page on timeout and retries through the canonical history owner', async () => {
		const first = transcriptFixture(timelineFixture([message('first page visible', 'assistant')]).items)[0];
		const { timeline } = surface();
		await screen.findByTestId('sub-agent-composer-input');
		await waitFor(() => expect(timeline.history['subagent-a']?.complete).toBe(true));
		timeline.history['subagent-a'].complete = false;
		vi.mocked(listTimelinePage).mockResolvedValueOnce({ items: [first], nextBefore: 1, after: first.seq })
			.mockRejectedValueOnce(new Error('Scoped history page timed out after 30 seconds.'));
		await timeline.loadHistory('subagent-a', true);
		await screen.findByText('first page visible');
		// A short viewport requests the next page automatically; failures retain retry.
		await screen.findByText(/timed out after 30 seconds/);
		vi.mocked(listTimelinePage).mockResolvedValueOnce({ items: [first], nextBefore: null, after: first.seq });
		await fireEvent.click(screen.getByTestId('history-retry'));
		await waitFor(() => expect(screen.queryByTestId('history-retry')).toBeNull());
		expect(screen.getAllByTestId('chat-message')).toHaveLength(1);
	});
	it('cancels old-node history on sibling navigation while keeping received pages in the canonical timeline', async () => {
		let finish!: (page: TimelinePage) => void;
		const timeline = new TimelineController(); timeline.start('ws-1', 'chat-1');
		await vi.waitFor(() => expect(timeline.history.root?.complete).toBe(true));
		vi.mocked(listTimelinePage).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		const controller = new WorkflowScopeController('ws-1', 'siblings', timeline);
		controller.attachChat('chat-1', { scope: 'conversation:chat-1/sub_agent:subagent-a' });
		await controller.ensureLoaded();
		const signal = vi.mocked(listTimelinePage).mock.calls.at(-1)![4]!;
		timeline.merge(timelineFixture([message('partial', 'assistant')]).items);
		const sibling = { ...controller.subAgentById('subagent-a')!, subAgentId: 'subagent-b' };
		controller.knownNodes = [...controller.knownNodes, sibling];
		controller.enterSubAgent(sibling);
		expect(signal.aborted).toBe(true);
		expect(controller.activeSegment?.id).toBe('subagent-b');
		finish({ items: transcriptFixture(timelineFixture([message('late')]).items), nextBefore: null, after: 1 });
		await Promise.resolve();
		expect(timeline.messagesFor('subagent-a').map((message) => message.body)).toEqual(['partial']);
		controller.detach(); timeline.stop();
	});
});
