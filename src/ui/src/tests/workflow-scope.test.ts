import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ExecutionEvent, SubAgentNode, WorkflowExecutionRecord } from '$lib/types';

const listChatSubAgents = vi.hoisted(() => vi.fn());
const listExecutionEvents = vi.hoisted(() => vi.fn());
const listNodeEvents = vi.hoisted(() => vi.fn());
const postNodeMessage = vi.hoisted(() => vi.fn());

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

import { WorkflowScopeController } from '$lib/workflow-scope.svelte';
import type { SubAgentTreeNode } from '$lib/subagent';

function execution(executionId: string): WorkflowExecutionRecord {
	return {
		execution_id: executionId,
		chat_id: 'chat-1',
		trigger_message_id: 'msg-1',
		workflow_id: 'hypothesis-development',
		workflow_version: '1.0.0',
		requested_by: 'user-alex',
		state: 'running',
		phase: 'ideation',
		intent_epoch: 0,
		steering_message_ids: [],
		created_at: '2026-09-08T17:14:01Z'
	};
}

function node(overrides: Partial<SubAgentNode> = {}): SubAgentNode {
	return {
		subAgentId: 'node-1',
		parentSubAgentId: null,
		phase: '',
		role: 'sub-agent',
		status: 'running',
		context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
		acceptsInput: false,
		workflowExecutionId: 'exec-1',
		...overrides
	};
}

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
		workspaceId: 'ws-1'
	};
}

function flatIds(nodes: SubAgentTreeNode[]): string[] {
	const ids: string[] = [];
	const visit = (list: SubAgentTreeNode[]): void => {
		for (const item of list) {
			ids.push(item.node.subAgentId);
			visit(item.children);
		}
	};
	visit(nodes);
	return ids;
}

const anchor = node({
	subAgentId: 'anchor-1',
	role: 'Researcher',
	acceptsInput: true
});
const workflowNode = node({
	subAgentId: 'wf-1',
	parentSubAgentId: 'anchor-1',
	phase: 'requested',
	role: 'workflow-node'
});
const phaseNode = node({
	subAgentId: 'phase-1',
	parentSubAgentId: 'wf-1',
	phase: 'ideation',
	role: 'Ideation'
});

beforeEach(() => {
	vi.clearAllMocks();
	window.localStorage.clear();
	listExecutionEvents.mockResolvedValue([]);
	listNodeEvents.mockResolvedValue([]);
});

describe('WorkflowScopeController workflow-node handling', () => {
	it('never exposes or enters the workflow-node record and never requests its events', async () => {
		listChatSubAgents.mockResolvedValue([anchor, workflowNode, phaseNode]);
		const controller = new WorkflowScopeController('ws-1', 'hide-node');
		controller.attach('exec-1', execution('exec-1'), { externalEvents: true });
		await controller.ensureLoaded();

		expect(flatIds(controller.tree)).toEqual(['anchor-1', 'phase-1']);
		expect(controller.subAgentById('wf-1')).toBeNull();

		controller.enterSubAgent(workflowNode);
		expect(controller.activeSegment?.kind).toBe('conversation');
		expect(listNodeEvents).not.toHaveBeenCalled();
		controller.detach();
	});

	it('opens the phase agent and backs out to the launching anchor', async () => {
		listChatSubAgents.mockResolvedValue([anchor, workflowNode, phaseNode]);
		const controller = new WorkflowScopeController('ws-1', 'nested-phase');
		controller.attach('exec-1', execution('exec-1'), { externalEvents: true });
		await controller.ensureLoaded();

		await controller.enterPhase('ideation');
		expect(controller.activeSegment).toMatchObject({ kind: 'sub_agent', id: 'phase-1' });
		expect(controller.scope.segments.map((segment) => segment.id)).toEqual([
			'chat-1',
			'anchor-1',
			'phase-1'
		]);
		expect(listNodeEvents).toHaveBeenCalledWith(
			'ws-1',
			'chat-1',
			{ kind: 'sub_agent', id: 'phase-1' },
			{ includeDescendants: false }
		);
		expect(listNodeEvents).not.toHaveBeenCalledWith(
			'ws-1',
			'chat-1',
			expect.objectContaining({ id: 'wf-1' }),
			expect.anything()
		);

		controller.back();
		expect(controller.activeSegment).toMatchObject({ kind: 'sub_agent', id: 'anchor-1' });
		controller.detach();
	});

	it('backs out to the conversation root for a root-launched workflow', async () => {
		const rootWorkflowNode = node({
			subAgentId: 'wf-2',
			parentSubAgentId: null,
			phase: 'requested',
			role: 'workflow-node'
		});
		const rootPhaseNode = node({
			subAgentId: 'phase-2',
			parentSubAgentId: 'wf-2',
			phase: 'ideation',
			role: 'Ideation'
		});
		listChatSubAgents.mockResolvedValue([rootWorkflowNode, rootPhaseNode]);
		const controller = new WorkflowScopeController('ws-1', 'root-phase');
		controller.attach('exec-1', execution('exec-1'), { externalEvents: true });
		await controller.ensureLoaded();

		await controller.enterPhase('ideation');
		expect(controller.scope.segments.map((segment) => segment.id)).toEqual([
			'chat-1',
			'phase-2'
		]);

		controller.back();
		expect(controller.activeSegment?.kind).toBe('conversation');
		controller.detach();
	});

	it('never requests events for a node without a workflow execution or outside the tree', async () => {
		const helper = node({
			subAgentId: 'helper-1',
			role: 'Helper',
			acceptsInput: true,
			workflowExecutionId: null
		});
		listChatSubAgents.mockResolvedValue([helper]);
		const controller = new WorkflowScopeController('ws-1', 'no-execution');
		controller.attach('exec-1', execution('exec-1'), { externalEvents: true });
		await controller.ensureLoaded();

		controller.enterSubAgent(helper);
		expect(controller.activeSegment).toMatchObject({ id: 'helper-1' });
		controller.loadNode('ghost-node');
		expect(listNodeEvents).not.toHaveBeenCalled();
		controller.detach();
	});

	it('drops a persisted scope that names the workflow node', async () => {
		listChatSubAgents.mockResolvedValue([anchor, workflowNode, phaseNode]);
		const controller = new WorkflowScopeController('ws-1', 'stale-scope');
		controller.attach('exec-1', execution('exec-1'), {
			externalEvents: true,
			scope: 'conversation:chat-1/sub_agent:wf-1/sub_agent:phase-1'
		});
		await controller.ensureLoaded();

		expect(controller.scope.segments.map((segment) => segment.id)).toEqual(['chat-1']);
		expect(controller.activeSegment?.kind).toBe('conversation');
		expect(listNodeEvents).not.toHaveBeenCalled();
		controller.detach();
	});
});

describe('WorkflowScopeController launch stability', () => {
	it('does not change the current selection when a workflow launches', async () => {
		listChatSubAgents.mockResolvedValue([anchor]);
		const controller = new WorkflowScopeController('ws-1', 'launch-selection');
		controller.attach('exec-1', execution('exec-1'), { externalEvents: true });
		await controller.ensureLoaded();
		controller.enterSubAgent(anchor);
		expect(controller.activeSegment).toMatchObject({ id: 'anchor-1' });

		// The launch adds the workflow node and its phase agent to the tree and
		// attaches the new execution; the anchor stays selected.
		const launchedAnchor = {
			...anchor,
			subAgentId: 'anchor-2'
		};
		const launchedWorkflowNode = node({
			subAgentId: 'wf-2',
			parentSubAgentId: 'anchor-2',
			phase: 'requested',
			role: 'workflow-node',
			workflowExecutionId: 'exec-2'
		});
		const launchedPhaseNode = node({
			subAgentId: 'phase-2',
			parentSubAgentId: 'wf-2',
			phase: 'ideation',
			role: 'Ideation',
			workflowExecutionId: 'exec-2'
		});
		listChatSubAgents.mockResolvedValue([
			anchor,
			launchedAnchor,
			launchedWorkflowNode,
			launchedPhaseNode
		]);
		listExecutionEvents.mockResolvedValue([
			event(
				'sub_agent.spawned',
				{ subAgentId: 'wf-2', parentSubAgentId: 'anchor-2', phase: 'requested', role: 'workflow-node' },
				1
			),
			event(
				'sub_agent.spawned',
				{ subAgentId: 'phase-2', parentSubAgentId: 'wf-2', phase: 'ideation', role: 'Ideation' },
				2
			)
		]);

		const scopeBeforeLaunch = controller.scope.toQuery();
		controller.attach('exec-2', execution('exec-2'), {
			chatId: 'chat-1',
			externalEvents: false,
			scope: scopeBeforeLaunch
		});
		await controller.ensureLoaded();

		expect(controller.activeSegment).toMatchObject({ kind: 'sub_agent', id: 'anchor-1' });
		expect(controller.scope.segments.map((segment) => segment.id)).toEqual(['chat-1', 'anchor-1']);
		controller.detach();
	});
});