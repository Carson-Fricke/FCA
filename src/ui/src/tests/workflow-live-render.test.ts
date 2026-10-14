import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { aggregateDatums, aggregateEnvelope, aggregateMessage, eventFixture, timelineFixture } from './timeline-fixture';

const fixture = vi.hoisted(() => ({
	subscribers: [] as Array<(item: Record<string, unknown>) => void>,
	anchor: {
		message_id: 'msg-wf',
		chat_id: 'chat-1',
		conversation_id: 'chat-1',
		root_conversation_id: 'chat-1',
		parent_conversation_id: null,
		workspace_id: 'ws-1',
		actor_id: 'thesis-workflow',
		role: 'system',
		body: 'Hypothesis development',
		state: 'accepted',
		attachment_ids: [],
		created_at: '2026-01-01T00:00:01Z',
		updated_at: null,
		execution_id: 'exec-1',
		intent_epoch: 0,
		kind: 'message',
        tool_result: { datumType: 'workflow_execution', ok: true, summary: 'Hypothesis development', text: '', failure: null, structured: { executionId: 'exec-1', workflowId: 'hypothesis-development', version: '2.0.0', activePhases: ['ideation'], state: 'running' } },
		data: {
			currentPhase: 'ideation',
			executionId: 'exec-1',
			status: 'running',
			title: 'Hypothesis development',
			workflowId: 'hypothesis-development'
		}
	}
}));

function chatThread(chatId: string) {
	return {
		chat_id: chatId,
		conversation_id: chatId,
		root_conversation_id: chatId,
		parent_conversation_id: null,
		workspace_id: 'ws-1',
		title: chatId,
		state: 'active',
		kind: 'chat' as const,
		owner_actor_id: 'user-alex',
		created_at: '2026-01-01T00:00:00Z',
		updated_at: null,
		investigation_ids: [],
		strategy_ids: []
	};
}

vi.mock('$lib/api', () => ({
	apiBaseUrl: 'http://api.test',
	fixtureMode: false,
	conversationTitleFrom: (body: string) => body,
	createChat: vi.fn(),
	deleteChat: vi.fn(),
	getExecution: vi.fn(async () => null),
	getWorkflowDefinition: vi.fn(async () => { throw new Error('Unavailable'); }),
	listTimelinePage: vi.fn(async () => ({ items: [], nextBefore: null, after: 0 })),
	listChats: vi.fn(async () => [chatThread('chat-1'), chatThread('chat-2')]),
	listChatSubAgents: vi.fn(async () => []),
	postMessage: vi.fn(),
	postNodeMessage: vi.fn()
}));

vi.mock('$lib/sse', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/sse')>();
	class FakeTimelineStream {
		async start(onItem: (item: Record<string, unknown>) => void) {
			fixture.subscribers.push(onItem);
		}
		stop(): void {}
	}
	return {
		...actual,
		TimelineStream: FakeTimelineStream
	};
});

import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
import ChatSurface from '$lib/components/chat/ChatSurface.svelte';
import ChatComposer from '$lib/components/chat/ChatComposer.svelte';
import ScopeTranscript from '$lib/components/chat/ScopeTranscript.svelte';
import ToolResultCard from '$lib/components/chat/ToolResultCard.svelte';
import { buildNodeEntries, projectToolMessages } from '$lib/transcript';
import CopilotPanel from '$lib/components/shell/CopilotPanel.svelte';
import { postMessage } from '$lib/api';
import type { ChatMessage, ExecutionEvent } from '$lib/types';

const anchor = fixture.anchor as unknown as ChatMessage;

describe('durable tool activity', () => {
	it('retires pending from canonical neutral correlation and retains raw incoming prompt text', () => {
		const messages = [aggregateEnvelope, { ...aggregateMessage, role: 'system' }] as ChatMessage[];
		const projected = projectToolMessages(messages);
		expect(projected.filter((message) => message.tool_result).map((message) => message.tool_result?.callId)).toEqual([
			'aggregate-call', 'aggregate-call'
		]);
		expect(projected.some((message) => message.tool_result?.pending)).toBe(false);
		const prompt = { ...aggregateMessage, role: 'user', body: 'Raw operator prompt', data: {
			sessionMessage: { ...aggregateMessage.data.sessionMessage, content: { text: 'Raw operator prompt', source: 'operator',
				correlation: null, result: null, requests: [] } }
		} } as ChatMessage;
		expect(projectToolMessages([prompt])[0]).toEqual(prompt);
	});
	it('retires pending for any explicitly correlated result and suppresses aggregate datum event copies', () => {
		const result = { ...aggregateMessage, role: 'system', data: {}, tool_result: {
			...aggregateDatums[0], tool: 'validate_study', callId: 'aggregate-call'
		} } as ChatMessage;
		expect(projectToolMessages([aggregateEnvelope as ChatMessage, result]).some((message) => message.tool_result?.pending)).toBe(false);
		const event = eventFixture({ eventId: 'aggregate-copy', eventType: 'tool.result', cursor: 3,
			occurredAt: aggregateMessage.created_at, payload: { toolResult: result.tool_result } });
		const entries = buildNodeEntries({ messages: [aggregateEnvelope, aggregateMessage] as ChatMessage[], events: [event] });
		expect(entries.filter((entry) => entry.kind === 'tool')).toHaveLength(0);
		expect(entries.map((entry) => entry.id)).toEqual(['aggregate-envelope', 'aggregate-result:datum:0', 'aggregate-result:datum:1']);
	});
	it.each(['root', 'scoped'])('projects two cards from one aggregate durable result in %s', (scope) => {
		const messages = [aggregateEnvelope, aggregateMessage, aggregateMessage] as ChatMessage[];
		const before = JSON.stringify(messages);
		const entries = buildNodeEntries({ messages, events: [] });
		expect(entries.map((entry) => entry.id)).toEqual(['aggregate-envelope', 'aggregate-result:datum:0', 'aggregate-result:datum:1']);
		if (scope === 'root') render(ChatTranscript, { props: { workspaceId: 'ws-1', messages } });
		else render(ScopeTranscript, { props: { workspaceId: 'ws-1', entries } });
		expect(screen.getAllByTestId('tool-result-card')).toHaveLength(2);
		expect(screen.getAllByTestId('tool-result-card').map((card) => card.dataset.datumType)).toEqual(['study_evidence', 'validation_report']);
		expect(screen.getAllByTestId('tool-result-status').map((card) => card.textContent?.trim())).toEqual(['completed', 'failed']);
		expect(screen.getByText('Retained measurements')).toBeInTheDocument();
		expect(screen.getByText('Retained compiler diagnostics')).toBeInTheDocument();
		expect(screen.getAllByText('duration: 42 ms')).toHaveLength(2);
		expect(screen.getAllByText(/aggregate-study/)).toHaveLength(2);
		expect(JSON.stringify(messages)).toBe(before);
	});
	it('projects structured-only datums when the durable tool result carries no content', () => {
		const structuredOnly = { ...aggregateMessage, message_id: 'structured-only', body: 'read_sdk_reference result',
			data: { activityKind: 'delivery', sessionMessage: { message_id: 'structured-only', direction: 'incoming', position: 1,
				content: { text: 'read_sdk_reference result', source: 'tool_result', requests: [], correlation: null,
					result: { datums: [
						{ datumType: 'read_sdk_reference', ok: true, summary: null, text: '',
							structured: { topic: 'study', source: 'export_study_program!' }, failure: null },
						{ datumType: 'read_artifact', ok: true, summary: null, text: '',
							structured: { name: 'metrics.json', content: '{"sharpe":1.2}' }, failure: null }
					] } } } } } as ChatMessage;
		const projected = projectToolMessages([structuredOnly]);
		expect(projected.map((message) => message.tool_result?.structured)).toEqual([
			{ topic: 'study', source: 'export_study_program!' },
			{ name: 'metrics.json', content: '{"sharpe":1.2}' }
		]);
		expect(projected.map((message) => message.tool_result?.datumType)).toEqual([
			'read_sdk_reference', 'read_artifact'
		]);
	});
	const envelope: ChatMessage = { ...anchor, tool_result: null, kind: 'message', role: 'assistant',
		seq: 1, message_id: 'envelope', body: 'Checking the study now.', data: { startedAt: '2026-01-01T00:00:01Z',
			sessionMessage: { message_id: 'envelope', direction: 'outgoing', position: 0,
				content: { text: 'Checking the study now.', source: 'agent', requests: [
					{ id: 'validate-1', name: 'validate_study', arguments: '{"study":"one"}' },
					{ id: 'run-1', name: 'run_study', arguments: '{"study":"one"}' }
				], correlation: null, result: null } } } };
	const result: ChatMessage = { ...envelope, role: 'tool', seq: 2, message_id: 'result', body: 'validated',
		created_at: '2026-01-01T00:00:02Z',
		data: { sessionMessage: { message_id: 'result', direction: 'incoming', position: 1,
			content: { text: 'validated', source: 'tool_result', requests: [],
				correlation: { request_message_id: 'envelope', request_id: 'validate-1', tool_name: 'validate_study' },
				result: { durationMs: 42 } } } },
		tool_result: { datumType: 'validation_report', tool: 'validate_study', callId: 'validate-1', ok: true, summary: 'validated',
			structured: {}, text: '', failure: null } };

	it.each(['root', 'scoped'])('renders pending -> final once in the %s transcript, retaining assistant text', async (scope) => {
		const props = { workspaceId: 'ws-1', messages: [envelope] };
		const view = scope === 'root' ? render(ChatTranscript, { props }) : render(ScopeTranscript,
			{ props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages: [envelope], events: [] }) } });
		expect(screen.getAllByTestId('tool-result-status').map((card) => card.textContent?.trim())).toEqual(['pending', 'pending']);
		expect(screen.getByText('Checking the study now.')).toBeInTheDocument();
		expect(screen.getAllByTestId('tool-started-at')).toHaveLength(2);
		const messages = [envelope, result, { ...result }, envelope];
		await view.rerender(scope === 'root' ? { ...props, messages } :
			{ workspaceId: 'ws-1', entries: buildNodeEntries({ messages, events: [] }) });
		expect(screen.getAllByTestId('tool-result-card')).toHaveLength(2);
		expect(screen.getAllByTestId('tool-result-status').map((card) => card.textContent?.trim()).sort()).toEqual(['completed', 'pending']);
		expect(screen.getByText('duration: 42 ms')).toBeInTheDocument();
		expect(screen.getAllByText('arguments')).toHaveLength(2);
	});

	it.each(['root', 'scoped'])('normalizes persisted message metadata in the %s transcript', (scope) => {
		const persisted: ChatMessage = { ...result,
			data: { sessionMessage: { message_id: 'result', direction: 'incoming', position: 1,
				content: { text: 'validated', source: 'tool_result', requests: [],
					correlation: { request_message_id: 'envelope', request_id: 'validate-1', tool_name: 'validate_study' },
					result: { durationMs: 120, arguments: { study: 'persisted-study' } } } } },
			tool_result: { ...result.tool_result!, structured: { reused: true } } };
		const messages = [envelope, persisted];
		if (scope === 'root') render(ChatTranscript, { props: { workspaceId: 'ws-1', messages } });
		else render(ScopeTranscript, { props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages, events: [] }) } });
		expect(screen.getByText('duration: 120 ms')).toBeInTheDocument();
		expect(screen.getByText('reused: true')).toBeInTheDocument();
		expect(screen.getByText(/persisted-study/)).toBeInTheDocument();
	});

	it('does not duplicate a final result event explicitly referencing its durable message', () => {
		const event = eventFixture({ eventId: 'tool-event', eventType: 'tool.result', cursor: 1,
			occurredAt: result.created_at, payload: { toolResult: result.tool_result, messageId: result.message_id } });
		const entries = buildNodeEntries({ messages: [envelope, result], events: [event] });
		expect(entries.filter((entry) => entry.kind === 'tool')).toHaveLength(0);
		expect(entries.filter((entry) => entry.kind === 'message')).toHaveLength(3);
	});

	it('does not duplicate assistant output events linked to an existing durable reply', () => {
		const event = eventFixture({ eventId: 'output-copy', eventType: 'sub_agent.output', cursor: 1,
			occurredAt: envelope.created_at, payload: { messageId: envelope.message_id, text: envelope.body } });
		const entries = buildNodeEntries({ messages: [envelope], events: [event] });
		expect(entries.some((entry) => entry.kind === 'assistant')).toBe(false);
		expect(entries.filter((entry) => entry.kind === 'message' && entry.message.role === 'assistant')).toHaveLength(1);
	});

	it('replaces an envelope pending call with a final event result before its message arrives', () => {
		const event = eventFixture({ eventId: 'tool-event', eventType: 'tool.result', cursor: 1,
			occurredAt: result.created_at, payload: { toolResult: result.tool_result } });
		const entries = buildNodeEntries({ messages: [envelope], events: [event, { ...event }] });
		expect(entries.filter((entry) => entry.kind === 'tool')).toHaveLength(1);
		const pending = entries.filter((entry) => entry.kind === 'message' && entry.message.tool_result?.pending);
		expect(pending).toHaveLength(1);
		expect(pending[0].kind === 'message' && pending[0].message.tool_result?.tool).toBe('run_study');
	});

	it.each(['answered', 'resolved'])('renders historical operator requests as %s', async (state) => {
		const question = { ...result, tool_result: { ...result.tool_result!, datumType: 'operator_question',
			summary: 'Approve this study?', structured: { question: 'Approve this study?' } } };
		const messages = state === 'answered' ? [question, { ...envelope, role: 'user' as const,
			seq: 3, created_at: '2026-01-01T00:00:03Z', data: {} }] : [question];
		const projected = projectToolMessages(messages, state === 'resolved');
		render(ToolResultCard, { props: { workspaceId: 'ws-1', result: projected[0].tool_result! } });
		expect(screen.getByTestId('operator-question-waiting')).toHaveTextContent(state);
		expect(screen.getByTestId('operator-question-message')).toHaveTextContent('Approve this study?');
	});

	it.each(['root', 'scoped'])('keeps failure results and system runtime notices visible in the %s transcript', (scope) => {
		const failed = { ...result, tool_result: { ...result.tool_result!, ok: false,
			failure: { kind: 'transport', message: 'Study dispatch failed' } } };
		const notice: ChatMessage = { ...envelope, message_id: 'notice', seq: 3, role: 'system',
			body: 'Reply interrupted: provider unavailable', data: { status: 'interrupted',
				failure: { kind: 'provider', message: 'provider unavailable', certainty: 'not_started' } } };
		const messages = [envelope, failed, notice];
		if (scope === 'root') render(ChatTranscript, { props: { workspaceId: 'ws-1', messages } });
		else render(ScopeTranscript, { props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages, events: [] }) } });
		expect(screen.getByText('Reply interrupted: provider unavailable')).toBeInTheDocument();
		expect(screen.getAllByTestId('tool-failure').map((card) => card.textContent)).toEqual([
			expect.stringContaining('outcome is unknown'), expect.stringContaining('Study dispatch failed'), expect.stringContaining('provider unavailable')
		]);
		expect(screen.getAllByTestId('chat-message').some((entry) => entry.dataset.role === 'system')).toBe(true);
	});

	it.each(['root', 'scoped'])('preserves distinct datums of one call and deduplicates only message copies in %s', (scope) => {
		const second = { ...result, message_id: 'second-datum', tool_result: { ...result.tool_result!,
			datumType: 'study_evidence', summary: 'Second datum evidence', text: 'Additional measurements' } };
		const messages = [envelope, result, second, { ...result }, { ...second }];
		if (scope === 'root') render(ChatTranscript, { props: { workspaceId: 'ws-1', messages } });
		else render(ScopeTranscript, { props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages, events: [] }) } });
		expect(screen.getAllByTestId('tool-result-card')).toHaveLength(3);
		expect(screen.getByText('validated')).toBeInTheDocument();
		expect(screen.getByText('Second datum evidence')).toBeInTheDocument();
		expect(screen.getByText('Additional measurements')).toBeInTheDocument();
		expect(screen.getAllByTestId('tool-result-status').map((card) => card.textContent?.trim())).toEqual(['pending', 'completed', 'completed']);
	});

	it('preserves distinct event datums sharing a call ID', () => {
		const event = eventFixture({ eventId: 'first', eventType: 'tool.result', cursor: 1, occurredAt: result.created_at,
			payload: { toolResult: result.tool_result } });
		const second = { ...event, eventId: 'second', payload: { toolResult: { ...result.tool_result!, summary: 'Another datum' } } };
		const entries = buildNodeEntries({ messages: [envelope], events: [event, second, { ...event }] });
		expect(entries.filter((entry) => entry.kind === 'tool')).toHaveLength(2);
	});

	it.each(['root', 'scoped'])('shares seq-ordered event normalization and message/event datum deduplication in %s', (scope) => {
		const persisted = { ...result, seq: 30, created_at: '2025-01-01T00:00:00Z' };
		const events = [
			{ eventId: 'copy', seq: 31, eventType: 'tool.result', cursor: 999, occurredAt: persisted.created_at,
				payload: { toolResult: persisted.tool_result } },
			{ eventId: 'additional', seq: 32, eventType: 'tool.result', cursor: 1, occurredAt: '2024-01-01',
				payload: { toolResult: { ...persisted.tool_result!, datumType: 'study_evidence', summary: 'Distinct evidence datum' } } }
		].map(eventFixture);
		const messages = [{ ...envelope, seq: 20 }, persisted];
		if (scope === 'root') render(ChatTranscript, { props: { workspaceId: 'ws-1', messages, events } });
		else render(ScopeTranscript, { props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages, events }) } });
		expect(screen.getAllByTestId('tool-result-card')).toHaveLength(3);
		expect(screen.getAllByTestId('tool-result-status').map((node) => node.textContent?.trim())).toEqual(['pending', 'completed', 'completed']);
		expect(screen.getAllByText('validated')).toHaveLength(1);
		expect(screen.getByText('Distinct evidence datum')).toBeInTheDocument();
	});

	it('shows failures even when a failed result retains a standalone datum type', () => {
		render(ToolResultCard, { props: { workspaceId: 'ws-1', result: { ...result.tool_result!, ok: false,
			datumType: 'workflow_execution', failure: { kind: 'validation', message: 'Transition validation failed' } } } });
		expect(screen.getByTestId('tool-failure')).toHaveTextContent('Transition validation failed');
	});

	it.each(['tool_result', 'get_job_status', 'sub_agent_handle'])('shows shared metadata once for %s renderers', (datumType) => {
		render(ToolResultCard, { props: { workspaceId: 'ws-1', result: { ...result.tool_result!, datumType,
			startedAt: '2026-01-01T00:00:01Z', arguments: { study: 'one' }, durationMs: 42,
			structured: { subAgentId: 'child-1', job_id: 'job-1', state: 'completed', reused: false } } } });
		expect(screen.getAllByTestId('tool-metadata')).toHaveLength(1);
		expect(screen.getAllByTestId('tool-started-at')).toHaveLength(1);
		expect(screen.getAllByText('duration: 42 ms')).toHaveLength(1);
		expect(screen.getAllByText('reused: false')).toHaveLength(1);
		expect(screen.getAllByText('arguments')).toHaveLength(1);
	});

	it.each(['root', 'scoped'])('marks terminal unmatched calls unknown instead of pending in %s', (scope) => {
		if (scope === 'root') render(ChatTranscript, { props: { workspaceId: 'ws-1', messages: [envelope], terminal: true } });
		else render(ScopeTranscript, { props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages: [envelope], events: [], terminal: true }) } });
		expect(screen.getAllByTestId('tool-result-status').map((card) => card.textContent?.trim())).toEqual(['unknown_outcome', 'unknown_outcome']);
		expect(projectToolMessages([envelope], true).filter((message) => message.role === 'tool').every((message) => message.tool_result?.ok === false)).toBe(true);
	});

	it('interrupts only older unmatched calls and lets a subsequent envelope remain pending', () => {
		const notice: ChatMessage = { ...envelope, message_id: 'interruption', role: 'system',
			created_at: '2026-01-01T00:00:02Z', data: { status: 'interrupted' } };
		const later: ChatMessage = { ...envelope, message_id: 'later', created_at: '2026-01-01T00:00:03Z',
			data: { sessionMessage: { message_id: 'later', direction: 'outgoing', position: 0,
				content: { text: 'Checking the study now.', source: 'agent',
					requests: [{ id: 'later-call', name: 'run_study', arguments: '{}' }], correlation: null, result: null } } } };
		const tools = projectToolMessages([envelope, notice, later]).filter((message) => message.role === 'tool');
		expect(tools.map((message) => message.tool_result?.outcome ?? 'pending')).toEqual(['interrupted', 'interrupted', 'pending']);
		expect(tools.map((message) => message.tool_result?.ok)).toEqual([false, false, false]);
	});

	it.each(['root', 'scoped'])('replaces interrupted placeholders with late results without hiding remaining unknown outcomes in %s', async (scope) => {
		const notice: ChatMessage = { ...envelope, message_id: 'interruption', role: 'system',
			created_at: '2026-01-01T00:00:02Z', data: { status: 'interrupted' } };
		const props = { workspaceId: 'ws-1', messages: [envelope, notice] };
		const view = scope === 'root' ? render(ChatTranscript, { props }) : render(ScopeTranscript,
			{ props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages: props.messages, events: [] }) } });
		expect(screen.getAllByTestId('tool-result-status').map((card) => card.textContent?.trim())).toEqual(['interrupted', 'interrupted']);
		const messages = [...props.messages, result];
		await view.rerender(scope === 'root' ? { ...props, messages } :
			{ workspaceId: 'ws-1', entries: buildNodeEntries({ messages, events: [] }) });
		expect(screen.getAllByTestId('tool-result-status').map((card) => card.textContent?.trim())).toEqual(['interrupted', 'completed']);
		expect(screen.getAllByTestId('tool-result-card')).toHaveLength(2);
	});
});

describe('runtime validation activity', () => {
	const outer: ChatMessage = { ...anchor, seq: 1, tool_result: null, kind: 'message', role: 'assistant', message_id: 'outer-envelope',
		body: 'Requesting phase completion.', created_at: '2026-01-01T00:00:01Z',
		data: { sessionMessage: { message_id: 'outer-envelope', direction: 'outgoing', position: 0,
			content: { text: 'Requesting phase completion.', source: 'agent', requests: [
				{ id: 'outer-completion', name: 'request_phase_completion', arguments: '{}' }
			], correlation: null, result: null } } } };
	const start: ChatMessage = { ...anchor, seq: 2, tool_result: null, kind: 'message', role: 'tool', message_id: 'runtime-start',
		conversation_id: 'node-1', parent_conversation_id: 'chat-1',
		body: 'Internal study validation started', created_at: '2026-01-01T00:00:02Z', data: {
			toolActivity: 'started', tool: 'validate_study', toolCallId: 'runtime-validation-1',
			subAgentId: 'node-1', arguments: { study: 'internal-study' }, startedAt: '2026-01-01T00:00:02Z'
		} };

	it.each(['root', 'scoped'])('renders a runtime validate start without inventing an assistant envelope in %s', (scope) => {
		const messages = [start, { ...start }];
		if (scope === 'root') render(ChatTranscript, { props: { workspaceId: 'ws-1', messages } });
		else render(ScopeTranscript, { props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages, events: [] }) } });
		expect(screen.getAllByTestId('tool-result-card')).toHaveLength(1);
		expect(screen.getByTestId('tool-result-card')).toHaveAttribute('data-tool', 'validate_study');
		expect(screen.getByTestId('tool-result-status')).toHaveTextContent('pending');
		expect(screen.getByTestId('tool-started-at')).toHaveTextContent(start.data!.startedAt as string);
		expect(screen.getByText(/internal-study/)).toBeInTheDocument();
		expect(screen.getAllByTestId('chat-message').every((message) => message.dataset.role === 'tool')).toBe(true);
		expect(projectToolMessages(messages)[0].message_id).toBe(start.message_id);
	});

	it.each([
		['root', true], ['root', false], ['scoped', true], ['scoped', false]
	] as const
	)('reconciles runtime validation in %s (ok=%s) and preserves outer completion ordering', async (scope, ok) => {
		const initial = [outer, start];
		const view = scope === 'root' ? render(ChatTranscript, { props: { workspaceId: 'ws-1', messages: initial } }) :
			render(ScopeTranscript, { props: { workspaceId: 'ws-1', entries: buildNodeEntries({ messages: initial, events: [] }) } });
		expect(screen.getAllByTestId('tool-result-card').map((card) => card.dataset.tool)).toEqual(['request_phase_completion', 'validate_study']);
		const finished: ChatMessage = { ...start, seq: 3, message_id: 'runtime-finish', created_at: '2026-01-01T00:00:03Z',
			data: { subAgentId: 'node-1', activityKind: 'delivery',
				sessionMessage: { message_id: 'runtime-finish', direction: 'incoming', position: 1,
					content: { text: ok ? 'Validation reused' : 'Validation failed', source: 'runtime',
						requests: [], correlation: null,
						result: { durationMs: 12, toolCallId: 'runtime-validation-1' } } } },
			tool_result: { datumType: 'validation_report', tool: 'validate_study', callId: 'runtime-validation-1', ok: Boolean(ok),
				summary: ok ? 'Validation reused' : 'Validation failed', structured: { reused: ok }, text: '',
				failure: ok ? null : { kind: 'compile', message: 'Invalid study source' } } };
		const messages = [outer, start, finished, { ...start }, { ...finished }];
		await view.rerender(scope === 'root' ? { workspaceId: 'ws-1', messages } :
			{ workspaceId: 'ws-1', entries: buildNodeEntries({ messages, events: [] }) });
		expect(screen.getAllByTestId('tool-result-card').map((card) => card.dataset.tool)).toEqual(['request_phase_completion', 'validate_study']);
		expect(screen.getAllByTestId('tool-result-status').map((card) => card.textContent?.trim())).toEqual(['pending', ok ? 'completed' : 'failed']);
		expect(screen.getByText(`reused: ${ok}`)).toBeInTheDocument();
		expect(screen.getByText('duration: 12 ms')).toBeInTheDocument();
		expect(screen.getByText(/internal-study/)).toBeInTheDocument();
		if (!ok) expect(screen.getByTestId('tool-failure')).toHaveTextContent('Invalid study source');
		const completed: ChatMessage = { ...finished, message_id: 'outer-finish', created_at: '2026-01-01T00:00:04Z',
			conversation_id: anchor.conversation_id, parent_conversation_id: anchor.parent_conversation_id,
			tool_result: { ...finished.tool_result!,
				tool: 'request_phase_completion', callId: 'outer-completion' } };
		const projected = projectToolMessages([...messages, completed]);
		expect(projected.filter((message) => message.role === 'assistant').map((message) => message.message_id)).toEqual(['outer-envelope']);
		expect(projected.filter((message) => message.role === 'tool').map((message) => message.tool_result?.tool)).toEqual(['validate_study', 'request_phase_completion']);
	});

	it.each(['terminal', 'interrupted'])('marks unmatched runtime activity unknown when %s', (state) => {
		const notice: ChatMessage = { ...start, role: 'system', message_id: 'interrupted',
			created_at: '2026-01-01T00:00:03Z', data: { subAgentId: 'node-1', status: 'interrupted' } };
		const projected = projectToolMessages(state === 'terminal' ? [start] : [start, notice], state === 'terminal');
		const result = projected.find((message) => message.role === 'tool')!.tool_result!;
		expect(result.pending).toBe(false);
		expect(result.ok).toBe(false);
		expect(result.outcome).toBe(state === 'terminal' ? 'unknown_outcome' : 'interrupted');
		expect(result.failure?.message).toContain('outcome is unknown');
	});
});

describe('root mid-turn composer', () => {
	it.each(['prompt', 'streaming', 'tool'])('allows sending while root activity is %s, blocking only the POST', async (activity) => {
		let finish!: () => void;
		const onSend = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
		const message: ChatMessage = { ...anchor, kind: 'message', role: activity === 'prompt' ? 'user' : 'assistant',
			body: 'Agent is working', data: activity === 'tool' ? { sessionMessage: {
				message_id: anchor.message_id, direction: 'outgoing', position: 0,
				content: { text: 'Agent is working', source: 'agent',
					requests: [{ id: 'root-tool', name: 'run_study', arguments: '{}' }],
					correlation: null, result: null } } } : {} };
        const timeline = timelineFixture([message]);
        if (activity === 'streaming') timeline.append({ id: 'streaming-root', seq: 100, kind: 'output',
            chatId: 'chat-1', workspaceId: 'ws-1', threadId: 'root', ts: anchor.created_at,
            payload: { state: 'started', invocationId: 'model', messageId: 'model-output', actorId: 'agent', startedAt: anchor.created_at } });
        render(ChatSurface, { props: { workspaceId: 'ws-1', chatId: 'chat-1', timeline, onSend } });
		const input = screen.getByTestId('composer-input');
		await fireEvent.input(input, { target: { value: 'feedback mid-turn' } });
		const send = screen.getByTestId('composer-send');
		expect(input).not.toBeDisabled();
		expect(send).not.toBeDisabled();
		await fireEvent.click(send);
		expect(send).toBeDisabled();
		expect(input).not.toBeDisabled();
		await fireEvent.click(send);
		expect(onSend).toHaveBeenCalledTimes(1);
		await fireEvent.input(input, { target: { value: 'next draft' } });
		finish();
		await waitFor(() => expect(send).not.toBeDisabled());
		expect(input).toHaveValue('next draft');
	});

	it('keeps a rejected root draft and visibly surfaces the send error', async () => {
		render(ChatComposer, { props: { onSubmit: vi.fn(async () => { throw new Error('POST rejected'); }) } });
		const input = screen.getByTestId('composer-input');
		await fireEvent.input(input, { target: { value: 'keep feedback' } });
		await fireEvent.click(screen.getByTestId('composer-send'));
		expect(await screen.findByTestId('composer-error')).toHaveTextContent('POST rejected');
		expect(input).toHaveValue('keep feedback');
	});
});

function timelineItem(seq: number) {
	return {
		id: `message:${fixture.anchor.message_id}`,
		chatId: 'chat-1',
		workspaceId: 'ws-1',
		kind: 'message',
		seq,
		ts: '2026-01-01T00:00:01Z',
		payload: fixture.anchor
	};
}

function timelineMessageItem(message: ChatMessage, seq: number) {
	return {
		id: `message:${message.message_id}`,
		chatId: message.chat_id,
		workspaceId: message.workspace_id,
		kind: 'message',
		seq,
		ts: message.created_at,
		payload: message
	};
}

beforeEach(() => {
	fixture.subscribers.length = 0;
});

describe('workflow anchor rendering', () => {
	it('renders a kind=workflow item as the WorkflowCard', async () => {
		render(ChatTranscript, { props: { messages: [anchor], workspaceId: 'ws-1' } });

		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-card-status')).toHaveTextContent('running');
		expect(screen.getByTestId('workflow-card-body')).toBeInTheDocument();
	});

});

describe('Copilot live timeline rendering', () => {
	it('keeps root sends available during a pending tool and gates only the message POST', async () => {
		let finish!: (value: import('$lib/types').PostMessageResult) => void;
		vi.mocked(postMessage).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
		render(CopilotPanel, { props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() } });
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));
		const envelope: ChatMessage = { ...anchor, tool_result: null, kind: 'message', role: 'assistant', data: { sessionMessage: {
			message_id: anchor.message_id, direction: 'outgoing', position: 0,
			content: { text: '', source: 'agent',
				requests: [{ id: 'working', name: 'run_study', arguments: '{}' }], correlation: null, result: null } } } };
		fixture.subscribers.at(-1)!(timelineMessageItem(envelope, 1));
		await screen.findByTestId('tool-result-status');
		const input = screen.getByTestId('composer-input');
		await fireEvent.input(input, { target: { value: 'please focus on quality' } });
		const send = screen.getByTestId('composer-send');
		expect(send).not.toBeDisabled();
		await fireEvent.click(send);
		expect(send).toBeDisabled();
		expect(input).not.toBeDisabled();
		finish({ message: envelope, percolated: [] });
		await waitFor(() => expect(input).toHaveValue(''));
		await fireEvent.input(input, { target: { value: 'another thought' } });
		await waitFor(() => expect(send).not.toBeDisabled());
		expect(screen.getByTestId('tool-result-status')).toHaveTextContent('pending');
	});

	it('surfaces a rejected copilot POST and retains its draft', async () => {
		vi.mocked(postMessage).mockRejectedValueOnce(new Error('service unavailable'));
		render(CopilotPanel, { props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() } });
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));
		const input = screen.getByTestId('composer-input');
		await fireEvent.input(input, { target: { value: 'retry this' } });
		await fireEvent.click(screen.getByTestId('composer-send'));
		expect(await screen.findByTestId('composer-error')).toHaveTextContent('service unavailable');
		expect(input).toHaveValue('retry this');
	});
	it('renders a root assistant envelope immediately, then one final tool result on replay', async () => {
		render(CopilotPanel, { props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() } });
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));
		const envelope: ChatMessage = { ...anchor, tool_result: null, message_id: 'root-envelope', kind: 'message', role: 'assistant',
			body: '', data: { sessionMessage: { message_id: 'root-envelope', direction: 'outgoing', position: 0,
				content: { text: '', source: 'agent',
					requests: [{ id: 'root-call', name: 'run_study', arguments: '{}' }], correlation: null, result: null } } } };
		const deliver = fixture.subscribers.at(-1)!;
		deliver(timelineMessageItem(envelope, 1));
		expect(await screen.findByTestId('tool-result-status')).toHaveTextContent('pending');
		const result: ChatMessage = { ...envelope, message_id: 'root-result', role: 'tool',
			data: { sessionMessage: { message_id: 'root-result', direction: 'incoming', position: 1,
				content: { text: 'Study complete', source: 'tool_result', requests: [],
					correlation: { request_message_id: 'root-envelope', request_id: 'root-call', tool_name: 'run_study' },
					result: {} } } },
			tool_result: { datumType: 'study_evidence', tool: 'run_study', callId: 'root-call', ok: true, summary: 'Study complete',
				structured: {}, text: '', failure: null } };
		deliver(timelineMessageItem(result, 2));
		deliver(timelineMessageItem(result, 2));
		await waitFor(() => expect(screen.getByTestId('tool-result-status')).toHaveTextContent('completed'));
		expect(screen.getAllByTestId('tool-result-card')).toHaveLength(1);
	});
	it('renders a workflow card pushed over the unified timeline stream', async () => {
		render(CopilotPanel, {
			props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() }
		});
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));

		fixture.subscribers.at(-1)?.(timelineItem(1));

		expect(await screen.findByTestId('workflow-card')).toBeInTheDocument();
		expect(screen.getByTestId('workflow-card-body')).toBeInTheDocument();
	});

	it('clears the active timeline and shows a frontend-only draft on new conversation', async () => {
		render(CopilotPanel, {
			props: { workspaceId: 'ws-1', onExpand: vi.fn(), onClose: vi.fn() }
		});
		await waitFor(() => expect(fixture.subscribers.length).toBeGreaterThan(0));

		const previous = {
			...anchor,
			message_id: 'old-message',
            tool_result: null,
			kind: 'message' as const,
			role: 'assistant' as const,
			body: 'old conversation text'
		};
		fixture.subscribers.at(-1)?.(timelineMessageItem(previous, 1));
		expect(await screen.findByText('old conversation text')).toBeInTheDocument();

		await fireEvent.click(screen.getByTestId('copilot-new-conversation'));

		expect(screen.queryByText('old conversation text')).toBeNull();
		await fireEvent.click(screen.getByTestId('copilot-history'));
		expect(screen.getAllByTestId('copilot-history-item')[0]).toHaveTextContent(
			'New conversation'
		);
	});
});
