import { describe, expect, it } from 'vitest';
import {
	CHAT_MESSAGE_EVENT_KIND,
	ExecutionEventStream,
	executionEventOf,
	parseSseChunk,
	type CursorStore,
	type SseFrame
} from '$lib/sse';
import type { ChatMessage } from '$lib/types';

const WORKSPACE = 'alpha-lab';
const EXECUTION = 'exec-1';

function durableEvent(id: number): string {
	return (
		`id: ${id}\nevent: workflow.started\n` +
		`data: ${JSON.stringify({
			kind: 'execution.event',
			eventId: `evt-${id}`,
			eventType: 'workflow.started',
			cursor: id,
			executionId: EXECUTION,
			workspaceId: WORKSPACE,
			correlationId: 'corr',
			actorId: 'agent',
			actorScope: 'workflow',
			intentEpoch: 0,
			lateResult: false,
			occurredAt: '2026-09-08T17:14:01Z',
			payload: {}
		})}\n\n`
	);
}

function chatMessageFrame(messageId: string, withId: boolean): string {
	const message: ChatMessage = {
		message_id: messageId,
		chat_id: 'chat-1',
		workspace_id: WORKSPACE,
		actor_id: 'agent',
		role: 'assistant',
		body: 'hello',
		state: 'accepted',
		attachment_ids: [],
		created_at: '2026-09-08T17:14:12Z',
		updated_at: null,
		execution_id: EXECUTION,
		intent_epoch: 0
	};
	const idLine = withId ? `id: 999\n` : '';
	return `${idLine}event: ${CHAT_MESSAGE_EVENT_KIND}\ndata: ${JSON.stringify({
		kind: CHAT_MESSAGE_EVENT_KIND,
		message
	})}\n\n`;
}

class RecordingCursorStore implements CursorStore {
	calls: { executionId: string; cursor: number }[] = [];
	private cursors = new Map<string, number>();

	get(executionId: string): number {
		return this.cursors.get(executionId) ?? 0;
	}

	set(executionId: string, cursor: number): void {
		this.calls.push({ executionId, cursor });
		this.cursors.set(executionId, cursor);
	}
}

/** Build a fake SSE response; `hang` keeps the stream open (no reconnect). */
function sseResponse(chunks: string[], hang = false): Response {
	const encoder = new TextEncoder();
	let index = 0;
	const body = new ReadableStream<Uint8Array>({
		pull(controller) {
			if (index < chunks.length) {
				controller.enqueue(encoder.encode(chunks[index]));
				index += 1;
				if (hang && index === chunks.length) return new Promise<void>(() => {});
				return undefined;
			}
			if (hang) return new Promise<void>(() => {});
			controller.close();
			return undefined;
		}
	});
	return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function makeFetch(responses: Response[], requests: { headers: Headers }[]): typeof fetch {
	return (async (_input: unknown, init?: RequestInit) => {
		requests.push({ headers: new Headers(init?.headers) });
		const next = responses.shift();
		if (!next) throw new Error('no more fake responses');
		return next;
	}) as unknown as typeof fetch;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 5));

describe('SSE chunk parsing', () => {
	it('parses complete frames and buffers partial frames', () => {
		const text = durableEvent(1) + chatMessageFrame('msg-1', false);
		const head = text.slice(0, 40);
		const tail = text.slice(40);
		const first = parseSseChunk('', head);
		expect(first.frames).toHaveLength(0);
		const second = parseSseChunk(first.buffer, tail);
		expect(second.frames).toHaveLength(2);
		expect(second.frames[0].id).toBe('1');
		expect(second.frames[1].id).toBeNull();
	});

	it('extracts durable execution events and rejects ephemeral frames', () => {
		const frames = parseSseChunk('', durableEvent(3) + chatMessageFrame('msg-x', false)).frames;
		expect(executionEventOf(frames[0])?.cursor).toBe(3);
		expect(executionEventOf(frames[1])).toBeNull();
	});
});

describe('ExecutionEventStream resume + dedup', () => {
	it('resumes with persisted Last-Event-ID and dedupes ephemeral messages', async () => {
		const store = new RecordingCursorStore();
		const requests: { headers: Headers }[] = [];
		const fetchImpl = makeFetch(
			[
				// First connection: two durable events, msg-1 delivered twice
				// (echoed snapshot), then the connection drops.
				sseResponse([durableEvent(1), durableEvent(2), chatMessageFrame('msg-1', false)]),
				// Reconnect: server re-snapshots msg-1 (must be suppressed) and
				// delivers cursor 3 plus a fresh msg-2.
				sseResponse(
					[chatMessageFrame('msg-1', false), durableEvent(3), chatMessageFrame('msg-2', false)],
					true
				)
			],
			requests
		);

		const delivered: SseFrame[] = [];
		const stream = new ExecutionEventStream({
			baseUrl: 'http://thesis.local',
			workspaceId: WORKSPACE,
			executionId: EXECUTION,
			cursorStore: store,
			fetchImpl,
			reconnectDelayMs: 1
		});

		const done = stream.start((frame) => delivered.push(frame));
		await flush();
		await flush();
		stream.stop();
		await done.catch(() => undefined);

		// First request carries no Last-Event-ID; the reconnect does.
		expect(requests.length).toBeGreaterThanOrEqual(2);
		expect(requests[0].headers.get('Last-Event-ID')).toBeNull();
		expect(requests[1].headers.get('Last-Event-ID')).toBe('2');

		// Durable frames delivered: cursors 1, 2, 3 (cursor 999 on a chat frame
		// is ignored as a cursor since ephemeral frames carry no id).
		const cursors = delivered
			.map((f) => executionEventOf(f)?.cursor ?? null)
			.filter((c): c is number => c !== null);
		expect(cursors).toEqual([1, 2, 3]);

		// Ephemeral dedup by message id across reconnects.
		const messageIds = delivered
			.filter((f) => f.event === CHAT_MESSAGE_EVENT_KIND)
			.map((f) => (f.data['message'] as ChatMessage).message_id);
		expect(messageIds).toEqual(['msg-1', 'msg-2']);
		expect(stream.hasSeenMessage('msg-1')).toBe(true);

		// Cursor persisted per execution.
		expect(store.calls).toEqual([
			{ executionId: EXECUTION, cursor: 1 },
			{ executionId: EXECUTION, cursor: 2 },
			{ executionId: EXECUTION, cursor: 3 }
		]);
		expect(stream.lastEventId).toBe(3);
	});

	it('starts from the persisted cursor for a previously subscribed execution', async () => {
		const store = new RecordingCursorStore();
		store.set(EXECUTION, 7);
		const requests: { headers: Headers }[] = [];
		const fetchImpl = makeFetch([sseResponse([durableEvent(8)], true)], requests);

		const stream = new ExecutionEventStream({
			baseUrl: '',
			workspaceId: WORKSPACE,
			executionId: EXECUTION,
			cursorStore: store,
			fetchImpl,
			reconnectDelayMs: 1
		});

		const delivered: SseFrame[] = [];
		const done = stream.start((frame) => delivered.push(frame));
		await flush();
		stream.stop();
		await done.catch(() => undefined);

		expect(requests[0].headers.get('Last-Event-ID')).toBe('7');
		expect(executionEventOf(delivered[0])?.cursor).toBe(8);
	});
});
