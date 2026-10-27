import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { flushSync, tick } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage, ExecutionEvent, SubAgentNode, TimelineItem, WorkflowExecutionRecord } from '$lib/types';
import { aggregateMessage, timelineFixture } from './timeline-fixture';

const streams = vi.hoisted(() => [] as Array<(item: TimelineItem) => void>);
vi.mock('$lib/api', async (original) => ({
	...await original<typeof import('$lib/api')>(), fixtureMode: false,
	listChats: vi.fn(async () => [{ chat_id: 'chat-1', title: 'Research' }]),
	listWorkflowMetadata: vi.fn(async () => ({ items: [], after: 0 })),
	listTimelinePage: vi.fn(async () => ({ items: [], nextBefore: null, after: 0 })),
	listChatSubAgents: vi.fn(async () => []),
	getExecution: vi.fn(async () => { throw new Error('Forbidden latest-execution fetch'); }),
	getWorkflowDefinition: vi.fn(async () => { throw new Error('Unavailable'); })
}));
vi.mock('$lib/sse', () => ({ TimelineStream: class {
	async start(receive: (item: TimelineItem) => void) { streams.push(receive); }
	stop() {}
} }));

import ChatSurface from '$lib/components/chat/ChatSurface.svelte';
import { ChatHostController } from '$lib/chat-host.svelte';
import { WorkflowScopeController } from '$lib/workflow-scope.svelte';
import { getExecution, listChatSubAgents } from '$lib/api';
import { phaseScopeId, parseScope, serializeScope } from '$lib/scope.svelte';
import { withWorkflowNodes } from '$lib/workflow-live';
import { deriveSubAgentTree, projectExecution } from '$lib/subagent';

const phase = 'discovery';
function launch(id: string, seq: number, workflowId = 'hypothesis-development'): ChatMessage {
	const datum = { datumType: 'workflow_execution', ok: true, summary: `Launched ${id}`, text: '', failure: null,
		structured: { executionId: id, workflowId, version: '5.0.0', activePhases: [phase] } };
	return { ...aggregateMessage, chat_id: 'chat-1', workspace_id: 'ws-1', seq, message_id: `launch-${id}`, kind: 'message',
		data: { activityKind: 'delivery', datums: [datum], sessionMessage: { ...aggregateMessage.data.sessionMessage,
			content: { ...aggregateMessage.data.sessionMessage.content, result: { datums: [datum] } } } } } as ChatMessage;
}
function event(id: string, seq: number, type: ExecutionEvent['eventType'], payload: Record<string, unknown>): ExecutionEvent {
	if (type.startsWith('workflow.') && !['workflow.progress', 'workflow.phase_result'].includes(type) && typeof payload.phase === 'string') payload = { activePhases: [payload.phase], controlSequence: seq, ...payload };
	return { eventId: `event-${seq}`, executionId: id, eventType: type, cursor: seq, seq,
		occurredAt: `2026-01-01T00:00:${String(seq).padStart(2, '0')}Z`, payload, workspaceId: 'ws-1',
		actorId: 'runtime', actorScope: 'workflow', correlationId: id, intentEpoch: 0, lateResult: false };
}
function spawn(id: string, seq: number): ExecutionEvent {
	return event(id, seq, 'sub_agent.spawned', { subAgentId: `node-${id}`, phase, role: `Agent ${id}`, acceptsInput: true, phaseExecutor: true });
}
const card = (id: string) => screen.getAllByTestId('workflow-card').find((element) => element.dataset.execution === id)!;

beforeEach(() => { streams.length = 0; vi.clearAllMocks(); localStorage.clear(); });

describe('multiple workflows on one timeline', () => {
	it('keeps parallel critics active, shows the blocked join, and ignores a delayed earlier frontier', async () => {
		const graph = (seq: number, controlSequence: number, activePhases: string[], arrived: string[]) => event('one', seq, 'workflow.phase', {
			phase: 'join_reviews', controlSequence, activePhases,
			pendingInputs: { join_reviews: { required: ['strategy', 'correctness'], arrived, activationId: 'round-one' } }
		});
		const reviewer = (name: string, seq: number) => event('one', seq, 'sub_agent.spawned', {
			subAgentId: name, phase: name, role: 'reviewer', phaseExecutor: true, acceptsInput: true, phaseAttemptId: `attempt-${name}`
		});
		const timeline = timelineFixture([launch('one', 1, 'parallel-review')], [graph(2, 1, ['strategy_review', 'correctness_review'], []),
			reviewer('strategy_review', 3), reviewer('correctness_review', 4)]);
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn() } });
		const step = (name: string) => card('one').querySelector(`[data-phase="${name}"]`)!;
		expect(step('strategy_review')).toHaveAttribute('data-status', 'active');
		expect(step('correctness_review')).toHaveAttribute('data-status', 'active');
		expect(step('join_reviews')).toHaveAttribute('title', 'Waiting for strategy, correctness');
		timeline.merge(timelineFixture([], [graph(5, 2, ['correctness_review'], ['strategy']),
			event('one', 6, 'sub_agent.completed', { subAgentId: 'strategy_review', phase: 'strategy_review', phaseExecutor: true, phaseAttemptId: 'attempt-strategy_review' }),
			graph(7, 1, ['strategy_review', 'correctness_review'], [])]).items);
		await tick();
		expect(step('strategy_review')).toHaveAttribute('data-status', 'done');
		expect(step('correctness_review')).toHaveAttribute('data-status', 'active');
		expect(step('join_reviews')).toHaveAttribute('title', 'Waiting for correctness');
		await fireEvent.click(step('correctness_review'));
		expect(screen.getByTestId('sub-agent-composer-input')).toBeInTheDocument();
		await fireEvent.click(screen.getByTestId('scope-back'));
		await fireEvent.click(step('join_reviews'));
		expect(screen.getByTestId('scope-readonly')).toBeInTheDocument();
	});

	it('renders aggregate launch handles, isolates failure/attention and navigates the matching agent without resetting on launch', async () => {
		const timeline = timelineFixture([launch('one', 1)], [spawn('one', 2)]);
		const onScopeChange = vi.fn();
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn(), onScopeChange } });
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalledTimes(1));
		await fireEvent.click(within(card('one')).getByTitle('Enter active discovery orchestrator'));
		expect(await screen.findByTestId('sub-agent-composer-input')).toBeInTheDocument();
		expect(onScopeChange.mock.calls.at(-1)?.[0]).toContain('sub_agent:node-one');
		timeline.merge(timelineFixture([launch('two', 3)], [spawn('two', 4)]).items);
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalledTimes(2));
		expect(screen.getByTestId('sub-agent-composer-input')).toBeInTheDocument();
		expect(onScopeChange.mock.calls.at(-1)?.[0]).toContain('sub_agent:node-one');
		await fireEvent.click(screen.getByTestId('scope-back'));
		expect(screen.getAllByTestId('workflow-card')).toHaveLength(2);
		expect(within(card('one')).getByTestId('workflow-card-status')).toHaveTextContent('running');
		expect(within(card('two')).getByTestId('workflow-card-status')).toHaveTextContent('running');
		expect(screen.getByTestId('composer-input')).toHaveAttribute('placeholder', expect.stringContaining('execution is active'));
		timeline.merge(timelineFixture([], [event('two', 5, 'sub_agent.needs_input', { subAgentId: 'node-two', phase }),
			event('one', 6, 'workflow.failed', { phase })]).items);
		await waitFor(() => expect(within(card('one')).getByTestId('workflow-card-status')).toHaveTextContent('failed'));
		expect(within(card('two')).getByTestId('workflow-card-status')).toHaveTextContent('running');
		expect(card('two')).toHaveTextContent('Input needed');
		expect(card('one')).not.toHaveTextContent('Input needed');
		await fireEvent.click(within(card('two')).getByText(/Input needed for discovery/));
		expect(onScopeChange.mock.calls.at(-1)?.[0]).toContain('sub_agent:node-two');
		expect(getExecution).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByTestId('scope-back'));
		timeline.merge(timelineFixture([], [event('two', 7, 'workflow.completed', { phase })]).items);
		await waitFor(() => expect(screen.getByTestId('composer-input')).not.toHaveAttribute('placeholder', expect.stringContaining('execution is active')));
	});

	it('opens an event-projected agent immediately while metadata is unresolved', async () => {
		vi.mocked(listChatSubAgents).mockImplementationOnce(() => new Promise(() => {}));
		const timeline = timelineFixture([launch('one', 1)], [spawn('one', 2)]);
		const controller = new WorkflowScopeController('ws-1', 'immediate', timeline);
		controller.attachChat('chat-1');
		await controller.enterPhase('one', phase);
		expect(controller.activeSegment?.id).toBe('node-one');
		controller.detach();
	});

	it('opens a phase before spawn without waiting for REST, then resolves only its matching live executor within a tick', async () => {
		vi.mocked(listChatSubAgents).mockImplementationOnce(() => new Promise(() => {}));
		const timeline = timelineFixture([launch('one', 1), launch('two', 2)]);
		const onScopeChange = vi.fn();
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn(), onScopeChange } });
		await fireEvent.click(within(card('one')).getByTestId('workflow-card-body'));
		expect(screen.getByTestId('scope-readonly')).toBeInTheDocument();
		expect(parseScope(onScopeChange.mock.calls.at(-1)![0]).at(-1)).toMatchObject({ kind: 'phase', executionId: 'one', phase });
		timeline.merge(timelineFixture([], [spawn('two', 3)]).items);
		await tick();
		expect(screen.queryByTestId('sub-agent-composer-input')).toBeNull();
		expect(parseScope(onScopeChange.mock.calls.at(-1)![0]).at(-1)?.executionId).toBe('one');
		timeline.merge(timelineFixture([], [spawn('one', 4)]).items);
		await tick();
		expect(screen.getByTestId('sub-agent-composer-input')).toBeInTheDocument();
		expect(onScopeChange.mock.calls.at(-1)![0]).toContain('sub_agent:node-one');
		expect(listChatSubAgents).toHaveBeenCalledTimes(1);
	});

	it('does not overwrite a later phase click or back navigation when the earlier executor arrives', async () => {
		vi.mocked(listChatSubAgents).mockImplementationOnce(() => new Promise(() => {}));
		const timeline = timelineFixture([launch('one', 1), launch('two', 2)]);
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn() } });
		await fireEvent.click(within(card('one')).getByTestId('workflow-card-body'));
		await fireEvent.click(screen.getByTestId('scope-back'));
		await fireEvent.click(within(card('two')).getByTestId('workflow-card-body'));
		timeline.merge(timelineFixture([], [spawn('one', 3)]).items);
		await tick();
		expect(screen.queryByTestId('sub-agent-composer-input')).toBeNull();
		await fireEvent.click(screen.getByTestId('scope-back'));
		timeline.merge(timelineFixture([], [spawn('two', 4)]).items);
		await tick();
		expect(screen.getByTestId('composer-input')).toBeInTheDocument();
		expect(screen.queryByTestId('sub-agent-composer-input')).toBeNull();
	});

	it('resolves a pending phase from metadata and discards it on chat reattachment', async () => {
		let finish!: (nodes: SubAgentNode[]) => void;
		vi.mocked(listChatSubAgents).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		const timeline = timelineFixture([launch('one', 1)]);
		const controller = new WorkflowScopeController('ws-1', 'pending-metadata', timeline);
		controller.attachChat('chat-1');
		await controller.enterPhase('one', phase);
		expect(controller.activeSegment?.kind).toBe('phase');
		finish([{ subAgentId: 'executor', parentSubAgentId: null, phase, role: 'executor',
			workflowExecutionId: 'one', phaseExecutor: true, status: 'running', acceptsInput: true, context: null }]);
		await controller.ensureLoaded();
		expect(controller.activeSegment?.id).toBe('executor');
		await controller.enterPhase('two', phase);
		controller.attachChat('chat-2');
		timeline.merge(timelineFixture([], [spawn('two', 3)]).items);
		controller.resolvePendingPhase();
		expect(controller.activeSegment).toMatchObject({ kind: 'conversation', id: 'chat-2' });
		controller.detach();
	});

	it('replays earlier lifecycle evidence over a delayed initial snapshot without resurrecting failures', async () => {
		const timeline = timelineFixture([launch('one', 1), launch('two', 2)], [event('one', 3, 'workflow.failed', { phase })]);
		const snapshot = { execution_id: 'one', chat_id: 'chat-1', trigger_message_id: '', workflow_id: '', workflow_version: '',
			requested_by: '', state: 'running', control_sequence: 0, active_phases: ['trials'], intent_epoch: 0, steering_message_ids: [], created_at: '' } as WorkflowExecutionRecord;
		timeline.merge([{ kind: 'execution', id: 'snapshot-one', seq: 4, payload: snapshot, chatId: 'chat-1', workspaceId: 'ws-1', ts: '' } as TimelineItem]);
		expect(timeline.workflows.get('one')?.execution.state).toBe('failed');
		expect(timeline.workflows.get('one')?.execution.phase).toBe(phase);
		timeline.merge(timelineFixture([], [event('two', 5, 'workflow.failed', { phase })]).items);
		expect(timeline.workflows.get('one')?.execution.state).toBe('failed');
		expect(timeline.workflows.get('two')?.execution.state).toBe('failed');
		timeline.merge([{ kind: 'execution', id: 'later-version', seq: 7, payload: { ...snapshot, active_phases: ['newer-snapshot'] },
			chatId: 'chat-1', workspaceId: 'ws-1', ts: '' } as TimelineItem]);
		expect(timeline.workflows.get('one')?.execution.state).toBe('failed');
		expect(timeline.workflows.get('one')?.execution.phase).toBe(phase);
		const fallback = { ...snapshot, execution_id: 'three' };
		timeline.merge([{ kind: 'execution', id: 'snapshot-three', seq: 6, payload: fallback, chatId: 'chat-1', workspaceId: 'ws-1', ts: '' } as TimelineItem]);
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn() } });
		expect(await screen.findAllByTestId('workflow-card')).toHaveLength(3);
	});

	it('does not let hidden workflow records, helpers or text/result events regress the current phase', () => {
		const execution = timelineFixture([launch('one', 1)]).workflows.get('one')!.execution;
		const events = [event('one', 2, 'workflow.phase', { phase: 'trials' }),
			event('one', 3, 'sub_agent.completed', { phase, role: 'workflow-node' }),
			event('one', 4, 'sub_agent.spawned', { phase: 'helper-phase', phaseExecutor: false }),
			event('one', 5, 'sub_agent.output', { phase: 'old-phase' }),
			event('one', 6, 'workflow.progress', { phase: 'old-phase' }),
			event('one', 7, 'workflow.phase_result', { phase: 'old-phase' })];
		expect(projectExecution(execution, events).phase).toBe('trials');
		expect(projectExecution({ ...execution, active_phases: [] }, [spawn('one', 2)]).phase).toBe(phase);
		expect(projectExecution({ ...execution, active_phases: [] }, [events[2]]).phase).toBeNull();
	});

	it('uses explicit REST executors over earlier helper spawns and requires explicit executor provenance', async () => {
		const executor: SubAgentNode = { subAgentId: 'executor', parentSubAgentId: null, phase, role: 'executor',
			workflowExecutionId: 'one', phaseExecutor: true, status: 'completed', acceptsInput: false, context: null };
		const helper = event('one', 2, 'sub_agent.spawned', { subAgentId: 'helper', phase,
			parentSubAgentId: 'executor', phaseExecutor: false, acceptsInput: true });
		const timeline = timelineFixture([launch('one', 1)], [helper]);
		vi.mocked(listChatSubAgents).mockResolvedValueOnce([executor]);
		const controller = new WorkflowScopeController('ws-1', 'executors', timeline);
		controller.attachChat('chat-1');
		await controller.ensureLoaded();
		const live = withWorkflowNodes(timeline.workflows, controller.tree).get('one')!;
		expect(live.phases.find((item) => item.name === phase)?.status).toBe('done');
		await controller.enterPhase('one', phase);
		expect(controller.activeSegment?.id).toBe('executor');
        const tree = deriveSubAgentTree([
            event('one', 3, 'sub_agent.spawned', { subAgentId: 'record', role: 'workflow-node', phase }),
            event('one', 4, 'sub_agent.spawned', { subAgentId: 'executor', parentSubAgentId: 'record', phase, phaseExecutor: true }),
            event('one', 5, 'sub_agent.spawned', { subAgentId: 'helper', parentSubAgentId: 'executor', phase, phaseExecutor: false })
        ]);
        expect(tree[0].node.phaseExecutor).toBe(true);
        expect(tree[0].children[0].node.phaseExecutor).toBe(false);
		controller.detach();
	});

	it.each(['failed', 'completed', 'cancelled', 'stopped'] as const)('opens %s workflow cards read-only even when the node still accepts input', async (state) => {
		const timeline = timelineFixture([launch('one', 1)], [spawn('one', 2), event('one', 3, `workflow.${state}`, { phase })]);
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn() } });
		await fireEvent.click(within(card('one')).getByTestId('workflow-card-body'));
		expect(await screen.findByTestId('scope-readonly')).toBeInTheDocument();
		expect(screen.queryByTestId('sub-agent-composer-input')).toBeNull();
		const controller = new WorkflowScopeController('ws-1', 'terminal', timeline);
		controller.attachChat('chat-1');
		await controller.enterPhase('one', phase, false);
		expect(controller.activeSegment?.readOnly).toBe(true);
		controller.detach();
	});

	it('closes only the terminal workflow composer while its scope is already open', async () => {
		const timeline = timelineFixture([launch('one', 1), launch('two', 2)], [spawn('one', 3), spawn('two', 4)]);
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn() } });
		await fireEvent.click(within(card('one')).getByTitle('Enter active discovery orchestrator'));
		expect(await screen.findByTestId('sub-agent-composer-input')).toBeInTheDocument();
		timeline.merge(timelineFixture([], [event('one', 5, 'workflow.failed', { phase })]).items);
		expect(await screen.findByTestId('scope-readonly')).toBeInTheDocument();
		await fireEvent.click(screen.getByTestId('scope-back'));
		await fireEvent.click(within(card('two')).getByTitle('Enter active discovery orchestrator'));
		expect(await screen.findByTestId('sub-agent-composer-input')).toBeInTheDocument();
	});

	it('preserves read-only scope restrictions when delayed metadata rebuilds ancestor paths', async () => {
		let finish!: (nodes: SubAgentNode[]) => void;
		vi.mocked(listChatSubAgents).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		const timeline = timelineFixture([launch('one', 1)]);
		const controller = new WorkflowScopeController('ws-1', 'restricted', timeline);
		controller.attachChat('chat-1', { scope: 'conversation:chat-1/sub_agent:child' });
		controller.scope.updateActive({ readOnly: true });
		const parent: SubAgentNode = { subAgentId: 'parent', parentSubAgentId: null, role: 'parent', phase,
			workflowExecutionId: 'one', phaseExecutor: true, status: 'running', acceptsInput: true, context: null };
		finish([parent, { ...parent, subAgentId: 'child', parentSubAgentId: 'parent', phaseExecutor: false }]);
		await controller.ensureLoaded();
		expect(controller.scope.segments.map((segment) => segment.id)).toEqual(['chat-1', 'parent', 'child']);
		expect(controller.activeSegment?.readOnly).toBe(true);
		const persisted = controller.scope.toQuery();
		expect(parseScope(persisted).at(-1)).toMatchObject({ id: 'child', readOnly: true });
		vi.mocked(listChatSubAgents).mockResolvedValueOnce([parent, { ...parent, subAgentId: 'child', parentSubAgentId: 'parent', phaseExecutor: false }]);
		controller.attachChat('chat-1', { scope: persisted });
		await controller.ensureLoaded();
		expect(controller.activeSegment?.readOnly).toBe(true);
		controller.detach();
	});

	it('indexes canonical-only aggregate results and renders multiple handles from one receipt without a separate workflow message format', async () => {
		const one = launch('one', 1);
		const two = launch('two', 1);
		const datums = [...one.data!.datums!, ...two.data!.datums!];
		const receipt = { ...one, data: { ...one.data, datums, sessionMessage: { ...one.data!.sessionMessage!,
			content: { ...one.data!.sessionMessage!.content, result: { datums } } } } };
		const canonicalOnly = { ...receipt, data: { sessionMessage: receipt.data.sessionMessage } };
		expect(timelineFixture([canonicalOnly]).workflows.size).toBe(2);
		const timeline = timelineFixture([receipt]);
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn() } });
		expect(await screen.findAllByTestId('workflow-card')).toHaveLength(2);
		expect(timeline.messages).toHaveLength(1);
		expect(screen.getAllByTestId('workflow-card').map((item) => item.dataset.execution)).toEqual(['one', 'two']);
	});

	it('keeps same-named deterministic phase results and failures execution-specific', async () => {
		const timeline = timelineFixture([launch('one', 1), launch('two', 2)], [
			event('one', 3, 'workflow.phase', { phase }), event('two', 4, 'workflow.phase', { phase }),
			event('one', 5, 'workflow.phase_result', { phase, artifact: { type: 'error', message: 'Only one failed' } }),
			event('two', 6, 'workflow.phase_result', { phase, summary: 'Two succeeded', artifact: { type: 'note' } })
		]);
		const controller = new WorkflowScopeController('ws-1', 'deterministic', timeline);
		controller.attachChat('chat-1');
		await controller.enterPhase('one', phase);
		expect(controller.eventsForPhase('one', phase).map((item) => item.executionId)).toEqual(['one', 'one']);
		const one = controller.activeSegment?.id;
		await controller.enterPhase('two', phase);
		expect(controller.activeSegment?.id).not.toBe(one);
		expect(controller.eventsForPhase('two', phase).at(-1)?.payload.summary).toBe('Two succeeded');
		controller.detach();
	});

	it('rejects ambiguous old phase links and retains collision-free execution-specific phase scopes', async () => {
		const timeline = timelineFixture([launch('one', 1), launch('two', 2)]);
		const controller = new WorkflowScopeController('ws-1', 'links', timeline);
		controller.attachChat('chat-1', { scope: 'conversation:chat-1/phase:discovery' });
		await controller.ensureLoaded();
		expect(controller.error).toContain('is outdated');
		expect(controller.activeSegment?.executionId).toBeUndefined();
		await controller.enterPhase('one', phase);
		const first = controller.scope.toQuery();
		await controller.enterPhase('two', phase);
		expect(controller.scope.toQuery()).not.toBe(first);
		expect(parseScope(controller.scope.toQuery()).at(-1)).toMatchObject({ executionId: 'two', phase });
		expect(phaseScopeId('a:b', 'c')).not.toBe(phaseScopeId('a', 'b:c'));
		const partial = timelineFixture([launch('one', 1)]);
		partial.history.root = { complete: false, loading: true, loaded: false, nextBefore: null, error: '' };
		const partialController = new WorkflowScopeController('ws-1', 'partial-links', partial);
		partialController.attachChat('chat-1', { scope: 'conversation:chat-1/phase:discovery' });
		expect(partialController.activeSegment?.executionId).toBeUndefined();
		expect(partialController.error).toContain('is outdated');
		partialController.detach();
		controller.attachChat('chat-1', { scope: serializeScope([{ kind: 'conversation', id: 'chat-1', label: '' },
			{ kind: 'phase', id: phaseScopeId('one', phase), label: '' }]) });
		expect(controller.activeSegment?.executionId).toBe('one');
		expect(controller.error).toBe('');
		controller.detach();
	});

	it('opens one stream and never requests a latest execution, including conversation switches', async () => {
		const host = new ChatHostController();
		await host.open('ws-1');
		await vi.waitFor(() => expect(streams).toHaveLength(1));
		for (const item of timelineFixture([launch('one', 1), launch('two', 2)]).items) streams[0](item);
		expect(host.timeline.workflows.size).toBe(2);
		host.select('chat-2');
		await vi.waitFor(() => expect(streams).toHaveLength(2));
		streams[0](timelineFixture([launch('stale', 3)]).items[0]);
		expect(host.timeline.workflows.size).toBe(0);
		expect(getExecution).not.toHaveBeenCalled();
		host.stop();
	});

	it('does not refresh metadata for text deltas, and coalesces lifecycle refreshes', async () => {
		const timeline = timelineFixture([launch('one', 1)]);
		render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend: vi.fn() } });
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalledTimes(1));
		await tick();
		for (let seq = 2; seq < 30; seq++) {
			timeline.merge(timelineFixture([], [event('one', seq, 'sub_agent.output', { subAgentId: 'node-one', text: 'delta' })]).items);
			flushSync();
		}
		expect(listChatSubAgents).toHaveBeenCalledTimes(1);
		let finish!: (nodes: []) => void;
		vi.mocked(listChatSubAgents).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		timeline.merge(timelineFixture([], [spawn('one', 30)]).items);
		await tick();
		timeline.merge(timelineFixture([], [event('one', 31, 'sub_agent.needs_input', { subAgentId: 'node-one', phase })]).items);
		await tick();
		timeline.merge(timelineFixture([], [spawn('two', 32)]).items);
		await tick();
		expect(listChatSubAgents).toHaveBeenCalledTimes(2);
		finish([]);
		await waitFor(() => expect(listChatSubAgents).toHaveBeenCalledTimes(3));
	});

	it('loads and updates a 100-workflow/10,000-event indexed fixture under one second', () => {
		const messages = Array.from({ length: 100 }, (_, index) => launch(`run-${index}`, index + 1));
		const events = Array.from({ length: 10_000 }, (_, index) => event(`run-${index % 100}`, index + 101,
			'workflow.progress', { phase, text: 'measurement' }));
		const start = performance.now();
		const timeline = timelineFixture(messages, events);
		const controller = new WorkflowScopeController('ws-1', 'perf', timeline);
		expect(withWorkflowNodes(timeline.workflows, controller.tree).size).toBe(100);
		const loaded = performance.now() - start;
		const updateStart = performance.now();
		timeline.merge(timelineFixture([], [event('run-0', 10_101, 'workflow.failed', { phase })]).items);
		expect(withWorkflowNodes(timeline.workflows, controller.tree).get('run-0')?.execution.state).toBe('failed');
		expect(timeline.workflows.get('run-1')?.execution.state).toBe('running');
		const updated = performance.now() - updateStart;
		console.info(`Multi-workflow fixture: load ${loaded.toFixed(1)} ms, update ${updated.toFixed(1)} ms`);
		expect(loaded).toBeLessThan(1000);
		expect(updated).toBeLessThan(1000);
	});
});
