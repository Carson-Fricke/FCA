import { TimelineController } from '$lib/timeline.svelte';
import type { ChatMessage, ExecutionEvent, TimelineItem } from '$lib/types';
import type { DatumWire } from '$lib/generated';
import backendMessage from './backend-message.json';
import { fixtures } from '$lib/fixtures';
import { decodeWire } from '$lib/wire';

export function eventFixture(overrides: Partial<ExecutionEvent>): ExecutionEvent {
    const {seq, threadId, ...wire} = overrides;
    if (wire.eventType?.startsWith('workflow.') && !['workflow.progress', 'workflow.phase_result'].includes(wire.eventType) && typeof wire.payload?.phase === 'string') {
        wire.payload = { activePhases: [wire.payload.phase], controlSequence: wire.cursor ?? seq ?? 0, ...wire.payload };
    }
    return { ...decodeWire('ExecutionEventWire', { ...fixtures.executionEvents['exec-1'].items[0], ...wire }, 'test/event'), seq: seq ?? overrides.cursor ?? 1, threadId };
}

export const aggregateDatums: DatumWire[] = [
	{ datumType: 'study_evidence', ok: true, summary: 'Measured study evidence', text: 'Retained measurements',
		structured: { observations: [12, 34] }, failure: null },
	{ datumType: 'validation_report', ok: false, summary: 'Study validation details', text: 'Retained compiler diagnostics',
		structured: { diagnostic: 'missing series' }, failure: { kind: 'validation', message: 'Series is required' } }
];
export const aggregateMessage = { ...backendMessage, message_id: 'aggregate-result', role: 'tool', tool_result: null,
	body: 'Study results', data: { activityKind: 'delivery',
		sessionMessage: { message_id: 'aggregate-result', direction: 'incoming', position: 2,
			content: { text: 'Study results', source: 'tool_result', requests: [],
				correlation: { request_message_id: 'aggregate-envelope', request_id: 'aggregate-call', tool_name: 'validate_study' },
				result: { datums: aggregateDatums, durationMs: 42 } } } } };
export const aggregateEnvelope = { ...backendMessage, message_id: 'aggregate-envelope', role: 'assistant', tool_result: null,
	body: 'Checking aggregate results.', data: { activityKind: 'model_output', startedAt: '2026-01-01T00:00:01Z',
		sessionMessage: { message_id: 'aggregate-envelope', direction: 'outgoing', position: 0,
			content: { text: 'Checking aggregate results.', source: 'agent',
				requests: [{ id: 'aggregate-call', name: 'validate_study', arguments: '{"study":"aggregate-study"}' }],
				correlation: null, result: null } } } };

export function timelineFixture(messages: ChatMessage[] = [], events: ExecutionEvent[] = []): TimelineController {
	const timeline = new TimelineController();
	const items: TimelineItem[] = [
		...messages.map((message, index) => {
            const {seq, threadId, ...wire} = message;
            return { id: message.message_id, kind: 'message' as const, seq: seq ?? index + 1,
			chatId: message.chat_id, workspaceId: message.workspace_id, ts: message.created_at,
			threadId: threadId ?? String(message.data?.subAgentId ?? 'root'), payload: decodeWire('ChatMessageWire', { ...backendMessage, tool_result: null, ...wire }, 'test/message') };
        }),
		...events.map((event, index) => ({ id: event.eventId, kind: 'event' as const, seq: event.seq ?? messages.length + index + 1,
			chatId: 'chat-1', workspaceId: event.workspaceId, ts: event.occurredAt,
			threadId: String(event.payload.subAgentId ?? 'root'), payload: event }))
	];
	timeline.merge(items);
	return timeline;
}
