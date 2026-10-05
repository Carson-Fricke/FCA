import { TimelineController } from '$lib/timeline.svelte';
import type { ChatMessage, ExecutionEvent, TimelineItem } from '$lib/types';
import type { DatumWire } from '$lib/generated';
import backendMessage from './backend-message.json';

export const aggregateDatums: DatumWire[] = [
	{ datumType: 'study_evidence', ok: true, summary: 'Measured study evidence', text: 'Retained measurements',
		structured: { observations: [12, 34] }, failure: null },
	{ datumType: 'validation_report', ok: false, summary: 'Study validation details', text: 'Retained compiler diagnostics',
		structured: { diagnostic: 'missing series' }, failure: { kind: 'validation', message: 'Series is required' } }
];
export const aggregateMessage = { ...backendMessage, message_id: 'aggregate-result', role: 'tool', tool_result: null,
	body: 'Study results', data: { activityKind: 'delivery', toolCallId: 'aggregate-call', durationMs: 42, reused: false,
		sessionMessage: { message_id: 'aggregate-result', direction: 'incoming', position: 2,
			content: { text: 'Study results', source: 'tool_result', requests: [],
				correlation: { request_message_id: 'aggregate-envelope', request_id: 'aggregate-call', tool_name: 'validate_study' },
				result: { datums: aggregateDatums } } }, datums: aggregateDatums } };
export const aggregateEnvelope = { ...backendMessage, message_id: 'aggregate-envelope', role: 'assistant', tool_result: null,
	body: 'Checking aggregate results.', data: { activityKind: 'model_output', startedAt: '2026-01-01T00:00:01Z',
		toolCalls: [{ id: 'aggregate-call', function: { name: 'validate_study', arguments: '{"study":"aggregate-study"}' } }] } };

export function timelineFixture(messages: ChatMessage[] = [], events: ExecutionEvent[] = []): TimelineController {
	const timeline = new TimelineController();
	const items: TimelineItem[] = [
		...messages.map((message, index) => ({ id: message.message_id, kind: 'message', seq: message.seq ?? index + 1,
			chatId: message.chat_id, workspaceId: message.workspace_id, ts: message.created_at,
			threadId: String(message.data?.subAgentId ?? 'root'), payload: message as unknown as Record<string, unknown> })),
		...events.map((event, index) => ({ id: event.eventId, kind: 'event', seq: event.seq ?? messages.length + index + 1,
			chatId: 'chat-1', workspaceId: event.workspaceId, ts: event.occurredAt,
			threadId: String(event.payload.subAgentId ?? 'root'), payload: event as unknown as Record<string, unknown> }))
	];
	timeline.merge(items);
	return timeline;
}
