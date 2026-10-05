import { describe, expect, it, vi } from 'vitest';
import {
	parseSseChunk,
	TimelineStream,
	chatTimelineStreamUrl
} from '$lib/sse';
import type { TimelineItem } from '$lib/types';
import { fixtures } from '$lib/fixtures';
import { WireDecodeError } from '$lib/wire';

const WORKSPACE = 'alpha-lab';

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

describe('SSE chunk parsing', () => {
	it('parses complete frames and buffers partial frames', () => {
		const text = timelineFrame(1) + timelineFrame(2);
		const head = text.slice(0, 40);
		const tail = text.slice(40);
		const first = parseSseChunk('', head);
		expect(first.frames).toHaveLength(0);
		const second = parseSseChunk(first.buffer, tail);
		expect(second.frames).toHaveLength(2);
		expect(second.frames[0].id).toBe('1');
		expect(second.frames[1].id).toBe('2');
	});

	it('handles split CRLF delimiters, comments and multiline data', () => {
		const first = parseSseChunk('', ': heartbeat\r\n\r\nid: 3\r\nevent: timeline\r\ndata: {\r\ndata: "seq": 3}\r\n\r');
		expect(first.frames).toEqual([]);
		const second = parseSseChunk(first.buffer, '\n');
		expect(second.frames).toEqual([{ id: '3', event: 'timeline', data: { seq: 3 } }]);
		expect(second.buffer).toBe('');
	});
});

function timelineFrame(seq: number, body = fixtures.chatMessages['chat-1'].items[0].body): string {
	const item: TimelineItem = {
		id: `message-${seq}`, seq, threadId: 'root', kind: 'message',
		workspaceId: WORKSPACE, chatId: 'chat-1', ts: '2026-09-08T17:14:12Z',
		payload: { ...fixtures.chatMessages['chat-1'].items[0], message_id: `message-${seq}`, body }
	};
	return `id: ${seq}\nevent: timeline.item\ndata: ${JSON.stringify(item)}\n\n`;
}

describe('TimelineStream cursor-only delivery', () => {
	it('includes zero and positive snapshot watermarks without a backfill limit', () => {
		expect(chatTimelineStreamUrl('/api/', 'ws /', 'chat /', { after: 0 })).toBe(
			'/api/v1/workspaces/ws%20%2F/chats/chat%20%2F/timeline/stream?after=0'
		);
		expect(chatTimelineStreamUrl('/api', 'ws', 'chat', { after: 41, thread: 'node /' })).toBe(
			'/api/v1/workspaces/ws/chats/chat/timeline/stream?after=41&thread=node+%2F'
		);
	});
	it.each([-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])('rejects invalid initial watermark %s', (after) => {
		expect(() => new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after })).toThrow(RangeError);
	});
	it('tails from the REST watermark, then resumes with Last-Event-ID across drops', async () => {
		const requests: { url: string; headers: Headers }[] = [];
		const responses = [sseResponse([timelineFrame(8)]), sseResponse([timelineFrame(8), timelineFrame(9)], true)];
		const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			requests.push({ url: String(input), headers: new Headers(init?.headers) });
			return responses.shift()!;
		});
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1',
			after: 7, fetchImpl, reconnectDelayMs: 1 });
		const items: TimelineItem[] = [];
		const done = stream.start((item) => items.push(item));
		try {
			await vi.waitFor(() => expect(items.map((item) => item.seq)).toEqual([8, 9]));
			expect(requests[0].url).toContain('?after=7');
			expect(requests[0].url).not.toContain('limit');
			expect(requests[0].headers.get('Last-Event-ID')).toBeNull();
			expect(requests[1].url).toBe(requests[0].url);
			expect(requests[1].headers.get('Last-Event-ID')).toBe('8');
		} finally {
			stream.stop();
			await done;
		}
	});
	it('retries failed connects from the same snapshot watermark before receiving an item', async () => {
		const requests: { url: string; headers: Headers }[] = [];
		const responses = [new Response(null, { status: 503 }), sseResponse([timelineFrame(1)], true)];
		const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			requests.push({ url: String(input), headers: new Headers(init?.headers) });
			return responses.shift()!;
		});
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1',
			after: 0, fetchImpl, reconnectDelayMs: 1 });
		const items: TimelineItem[] = [];
		const done = stream.start((item) => items.push(item));
		try {
			await vi.waitFor(() => expect(items).toHaveLength(1));
			expect(requests.map((request) => request.url)).toEqual([requests[0].url, requests[0].url]);
			expect(requests[0].url).toContain('?after=0');
			expect(requests.map((request) => request.headers.get('Last-Event-ID'))).toEqual([null, null]);
		} finally {
			stream.stop();
			await done;
		}
	});
	it('suppresses stale and duplicate sequences without requiring contiguous thread sequences', async () => {
		const fetchImpl = vi.fn(async () => sseResponse([
			timelineFrame(7) + timelineFrame(6) + timelineFrame(8) + timelineFrame(8) + timelineFrame(7) + timelineFrame(12)
		]));
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 7, thread: 'root', fetchImpl });
		const items: TimelineItem[] = [];
		await stream.start((item) => { items.push(item); if (item.seq === 12) stream.stop(); });
		expect(items.map((item) => item.seq)).toEqual([8, 12]);
	});
	it('decodes UTF-8 and frames split across byte chunks without losing text', async () => {
		const text = 'hello \u00e9 \ud83d\ude80';
		const bytes = new TextEncoder().encode(timelineFrame(1, text).replace(/\n/g, '\r\n'));
		let index = 0;
		const body = new ReadableStream<Uint8Array>({ pull(controller) {
			if (index < bytes.length) controller.enqueue(bytes.slice(index, ++index));
			else controller.close();
		} });
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 0,
			fetchImpl: vi.fn(async () => new Response(body)) });
		const items: TimelineItem[] = [];
		await stream.start((item) => { items.push(item); stream.stop(); });
		expect(items).toHaveLength(1);
		expect(items[0]).toMatchObject({ kind: 'message', payload: { body: text } });
	});
	it('replays an incomplete frame after a dropped connection using the payload sequence, not SSE id', async () => {
		const requests: Headers[] = [];
		const responses = [
			sseResponse([timelineFrame(8).replace('id: 8', 'id: 999'), timelineFrame(9).slice(0, 50)]),
			sseResponse([timelineFrame(8) + timelineFrame(9)])
		];
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 7, reconnectDelayMs: 1,
			fetchImpl: vi.fn(async (_input, init) => { requests.push(new Headers(init?.headers)); return responses.shift()!; }) });
		const items: TimelineItem[] = [];
		await stream.start((item) => { items.push(item); if (item.seq === 9) stream.stop(); });
		expect(items.map((item) => item.seq)).toEqual([8, 9]);
		expect(requests.map((headers) => headers.get('Last-Event-ID'))).toEqual([null, '8']);
	});
	it('retains its delivered cursor after network and transient HTTP failures', async () => {
		const headers: Headers[] = [];
		const responses: (Response | Error)[] = [sseResponse([timelineFrame(8)]), new TypeError('network'),
			new Response(null, { status: 503 }), sseResponse([timelineFrame(9)])];
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 7, reconnectDelayMs: 1,
			fetchImpl: vi.fn(async (_input, init) => {
				headers.push(new Headers(init?.headers));
				const response = responses.shift()!;
				if (response instanceof Error) throw response;
				return response;
			}) });
		const items: TimelineItem[] = [];
		await stream.start((item) => { items.push(item); if (item.seq === 9) stream.stop(); });
		expect(items.map((item) => item.seq)).toEqual([8, 9]);
		expect(headers.map((header) => header.get('Last-Event-ID'))).toEqual([null, '8', '8', '8']);
		expect(headers.every((header) => header.get('Accept') === 'text/event-stream')).toBe(true);
	});
	it.each([400, 401, 403, 404, 409])('surfaces terminal HTTP %s rather than silently retrying', async (status) => {
		const fetchImpl = vi.fn(async () => new Response(null, { status }));
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 0, fetchImpl });
		await expect(stream.start(() => {})).rejects.toMatchObject({ name: 'ThesisApiError', status });
		expect(fetchImpl).toHaveBeenCalledTimes(1);
		stream.stop();
	});
	it.each([408, 429])('retries transient HTTP %s', async (status) => {
		const fetchImpl = vi.fn().mockResolvedValueOnce(new Response(null, { status })).mockResolvedValueOnce(sseResponse([timelineFrame(1)]));
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 0, fetchImpl, reconnectDelayMs: 1 });
		await stream.start(() => stream.stop());
		expect(fetchImpl).toHaveBeenCalledTimes(2);
	});
	it.each([
		'event: timeline\ndata: {broken}\n\n',
		'event: timeline\ndata: {"seq":1}\n\n',
		timelineFrame(Number.MAX_SAFE_INTEGER + 1),
		timelineFrame(1.5)
	])('rejects invalid wire data without retry or delivery', async (frame) => {
		const cancel = vi.fn();
		const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new TextEncoder().encode(frame)); }, cancel });
		const fetchImpl = vi.fn(async () => new Response(body));
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 0, fetchImpl });
		const receive = vi.fn();
		await expect(stream.start(receive)).rejects.toBeInstanceOf(WireDecodeError);
		expect(receive).not.toHaveBeenCalled();
		expect(fetchImpl).toHaveBeenCalledTimes(1);
		expect(cancel).toHaveBeenCalledTimes(1);
		stream.stop();
	});
	it('does not advance the cursor until the consumer accepts an item', async () => {
		const headers: Headers[] = [];
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 0, reconnectDelayMs: 1,
			fetchImpl: vi.fn(async (_input, init) => { headers.push(new Headers(init?.headers)); return sseResponse([timelineFrame(1)]); }) });
		let attempts = 0;
		await stream.start(() => { if (++attempts === 1) throw new Error('consumer failure'); stream.stop(); });
		expect(attempts).toBe(2);
		expect(headers.map((header) => header.get('Last-Event-ID'))).toEqual([null, null]);
	});
	it('stops promptly during reconnect backoff', async () => {
		vi.useFakeTimers();
		try {
			const fetchImpl = vi.fn(async () => new Response(null, { status: 503 }));
			const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 0,
				fetchImpl, reconnectDelayMs: 60_000 });
			const done = stream.start(() => {});
			await Promise.resolve();
			expect(vi.getTimerCount()).toBe(1);
			stream.stop();
			await done;
			expect(fetchImpl).toHaveBeenCalledTimes(1);
			expect(vi.getTimerCount()).toBe(0);
		} finally { vi.useRealTimers(); }
	});
	it('aborts a pending connect and ignores frames remaining in a chunk after stop', async () => {
		let signal: AbortSignal | undefined;
		const pending = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 0,
			fetchImpl: vi.fn((_input, init) => {
				signal = init?.signal ?? undefined;
				return new Promise<Response>((_resolve, reject) => signal?.addEventListener('abort', () => reject(signal?.reason)));
			}) });
		const pendingDone = pending.start(() => { throw new Error('Unexpected arrival'); });
		pending.stop();
		await pendingDone;
		expect(signal?.aborted).toBe(true);
		const stream = new TimelineStream({ baseUrl: '/api', workspaceId: WORKSPACE, chatId: 'chat-1', after: 0,
			fetchImpl: vi.fn(async () => sseResponse([timelineFrame(1) + timelineFrame(2)])) });
		const items: TimelineItem[] = [];
		await stream.start((item) => { items.push(item); stream.stop(); });
		expect(items.map((item) => item.seq)).toEqual([1]);
	});
});
