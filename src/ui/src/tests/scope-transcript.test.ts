import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import ScopeTranscript from '$lib/components/chat/ScopeTranscript.svelte';
import { buildNodeEntries, outputText } from '$lib/transcript';
import type { SubAgentTreeNode } from '$lib/subagent';
import type { ChatMessage, ExecutionEvent, SubAgentNode, ToolResult } from '$lib/types';

const toolResult: ToolResult = {
	tool: 'run_backtest',
	callId: 'call-1',
	ok: true,
	summary: 'done',
	structured: { run_id: 'run-1' },
	text: '',
	failure: null
};

function event(
	eventType: ExecutionEvent['eventType'],
	payload: Record<string, unknown>,
	cursor: number,
	occurredAt = '2026-09-08T17:14:01Z'
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
		occurredAt,
		payload,
		workspaceId: 'alpha-lab'
	};
}

function message(
	id: string,
	createdAt: string,
	data: Record<string, unknown> = { subAgentId: 'sub-1' }
): ChatMessage {
	return {
		message_id: id,
		chat_id: 'chat-1',
		workspace_id: 'alpha-lab',
		actor_id: 'user-alex',
		role: 'user',
		body: id,
		state: 'accepted',
		attachment_ids: [],
		created_at: createdAt,
		updated_at: null,
		execution_id: 'exec-1',
		intent_epoch: 0,
		kind: 'sub_agent',
		data
	};
}

function node(overrides: Partial<SubAgentNode> = {}): SubAgentNode {
	return {
		subAgentId: 'sub-2',
		parentSubAgentId: 'sub-1',
		phase: 'research',
		role: 'Coder',
		status: 'running',
		context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
		acceptsInput: false,
		workflowExecutionId: 'exec-1',
		...overrides
	};
}

describe('buildNodeEntries', () => {
	it('interleaves user messages, tools, and outputs chronologically', () => {
		const entries = buildNodeEntries({
			messages: [
				message('m1', '2026-09-08T17:14:00Z'),
				message('m3', '2026-09-08T17:14:03Z')
			],
			events: [
				event('tool.result', { subAgentId: 'sub-1', toolResult }, 2, '2026-09-08T17:14:02Z'),
				event('sub_agent.output', { subAgentId: 'sub-1', text: 'the answer' }, 3, '2026-09-08T17:14:04Z')
			]
		});

		expect(entries.map((entry) => entry.kind)).toEqual([
			'message',
			'tool',
			'message',
			'assistant'
		]);
		expect(entries.map((entry) => entry.at)).toEqual([
			'2026-09-08T17:14:00Z',
			'2026-09-08T17:14:02Z',
			'2026-09-08T17:14:03Z',
			'2026-09-08T17:14:04Z'
		]);
		const assistant = entries.at(-1);
		expect(assistant?.kind === 'assistant' && assistant.text).toBe('the answer');
	});

	it('never renders engine activity labels as conversation content', () => {
		const entries = buildNodeEntries({
			messages: [],
			events: [
				event(
					'sub_agent.progress',
					{ subAgentId: 'sub-1', kind: 'activity', text: 'Preparing trials.' },
					1,
					'2026-09-08T17:14:01Z'
				),
				event(
					'sub_agent.progress',
					{ subAgentId: 'sub-1', kind: 'thinking', text: 'weighing' },
					2,
					'2026-09-08T17:14:02Z'
				)
			]
		});

		expect(entries).toEqual([]);
	});

	it('parses structured falsification report artifacts into kind=falsification', () => {
		const entries = buildNodeEntries({
			messages: [],
			events: [
				event(
					'sub_agent.output',
					{
						subAgentId: 'sub-1',
						artifact: {
							type: 'falsification_report',
							content: 'Ran 3 falsification trial(s).',
							passedCount: 3,
							falsifiedCount: 0,
							totalCount: 3,
							survived: true,
							trials: [
								{
									testId: 'T01',
									falsifierId: 'f1',
									observable: 'alpha',
									condition: 'gte',
									threshold: 0,
									observed: -0.05,
									falsified: false
								}
							]
						}
					},
					1,
					'2026-09-08T17:14:04Z'
				)
			]
		});

		expect(entries).toHaveLength(1);
		expect(entries[0].kind).toBe('falsification');
		if (entries[0].kind === 'falsification') {
			expect(entries[0].data.survived).toBe(true);
			expect(entries[0].data.trials).toHaveLength(1);
		}
	});

	it('represents a child spawn as one drill-in item, not a raw event', () => {
		const child: SubAgentTreeNode = {
			node: node({ subAgentId: 'sub-2', role: 'Coder' }),
			events: [event('sub_agent.spawned', { subAgentId: 'sub-2' }, 5, '2026-09-08T17:14:05Z')],
			children: []
		};
		const entries = buildNodeEntries({
			messages: [],
			events: [],
			children: [child]
		});

		expect(entries).toHaveLength(1);
		expect(entries[0].kind).toBe('sub-agent');
		expect(entries[0].kind === 'sub-agent' && entries[0].node.subAgentId).toBe('sub-2');
	});

	it('renders a node\'s own spawn as its initial context', () => {
		const entries = buildNodeEntries({
			messages: [],
			events: [
				event(
					'sub_agent.spawned',
					{
						subAgentId: 'sub-1',
						role: 'Researcher',
						phase: 'research',
						context: { isolated: true, knowledge_cutoff: '2024-01-01', mcp: { enabled: true, tools: ['search'] } }
					},
					1
				)
			]
		});

		expect(entries).toHaveLength(1);
		const spawn = entries[0];
		expect(spawn.kind).toBe('spawn');
		expect(spawn.kind === 'spawn' && spawn.spawn.role).toBe('Researcher');
		expect(spawn.kind === 'spawn' && spawn.spawn.isolated).toBe(true);
		expect(spawn.kind === 'spawn' && spawn.spawn.knowledgeCutoff).toBe('2024-01-01');
		expect(spawn.kind === 'spawn' && spawn.spawn.mcpTools).toEqual(['search']);
	});

	it('renders a non-text artifact output as fenced JSON', () => {
		const value = outputText(
			event('sub_agent.output', { subAgentId: 'sub-1', artifact: { content: { verdict: 'reject' } } }, 1)
		);
		expect(value).toContain('```json');
		expect(value).toContain('reject');
	});

	it('never renders needs-input events: the event log is liveness wiring', () => {
		// The pause signal flips node status (and the root attention line);
		// the real question renders from its own tool-result message. The
		// event itself must never become conversational content.
		const entries = buildNodeEntries({
			messages: [],
			events: [
				event('sub_agent.output', { subAgentId: 'sub-1', text: 'Please approve.' }, 1),
				event('sub_agent.needs_input', { subAgentId: 'sub-1', question: 'Please approve.' }, 2),
				event('sub_agent.needs_input', { subAgentId: 'sub-1' }, 3)
			]
		});

		expect(entries.filter((entry) => entry.kind === 'assistant')).toHaveLength(1);
		expect(entries.every((entry) => entry.kind !== 'event')).toBe(true);
	});
});

describe('ScopeTranscript workflow rendering', () => {
	it('renders a node-scope workflow_execution datum as the workflow card', () => {
		const workflowResult: ToolResult = {
			tool: 'start_workflow',
			datumType: 'workflow_execution',
			callId: 'call-workflow',
			ok: true,
			summary: 'Hypothesis development started.',
			structured: {
				executionId: 'exec-42',
				workflowId: 'hypothesis-development',
				phase: 'requested',
				state: 'running'
			},
			text: '',
			failure: null
		};
		const entries = buildNodeEntries({
			messages: [],
			events: [event('tool.result', { subAgentId: 'sub-1', toolResult: workflowResult }, 1)]
		});

		render(ScopeTranscript, { props: { entries, workspaceId: 'alpha-lab' } });

		expect(screen.getByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByText('hypothesis-development')).toBeInTheDocument();
		expect(screen.queryByTestId('generic-tool-card')).not.toBeInTheDocument();
	});
});
