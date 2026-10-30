/** Fetch-based SSE consumption of the unified chat timeline. */
import type { TimelineItemWire } from './generated';
import { decodeWire, WireDecodeError } from './wire';
import { ThesisApiError } from './transport';

export interface SseFrame {
	id: string | null;
	event: string;
	data: Record<string, unknown>;
}

/** Parse complete SSE frames, retaining partial lines for the next chunk. */
export function parseSseChunk(buffer: string, chunk: string): { frames: SseFrame[]; buffer: string } {
	let text = buffer + chunk;
	const frames: SseFrame[] = [];
	let separator: number;
	while ((separator = findFrameEnd(text)) !== -1) {
		const block = text.slice(0, separator);
		text = text.slice(separator + (text.startsWith('\r\n\r\n', separator) ? 4 : 2));
		const frame = parseFrame(block);
		if (frame) frames.push(frame);
	}
	return { frames, buffer: text };
}

function findFrameEnd(text: string): number {
	const lf = text.indexOf('\n\n');
	const crlf = text.indexOf('\r\n\r\n');
	if (lf === -1) return crlf;
	if (crlf === -1) return lf;
	return Math.min(lf, crlf);
}

function parseFrame(block: string): SseFrame | null {
	let id: string | null = null;
	let event = 'message';
	const dataLines: string[] = [];
	for (const rawLine of block.split(/\r?\n/)) {
		if (rawLine.startsWith(':')) continue;
		const colon = rawLine.indexOf(':');
		const field = colon === -1 ? rawLine : rawLine.slice(0, colon);
		let value = colon === -1 ? '' : rawLine.slice(colon + 1);
		if (value.startsWith(' ')) value = value.slice(1);
		if (field === 'id') id = value;
		else if (field === 'event') event = value;
		else if (field === 'data') dataLines.push(value);
	}
	if (!dataLines.length) return null;
	try {
		return { id, event, data: JSON.parse(dataLines.join('\n')) as Record<string, unknown> };
	} catch {
		throw new WireDecodeError('JSON', 'timeline/stream', 'SSE data is not valid JSON');
	}
}

export interface TimelineStreamOptions {
	baseUrl: string;
	workspaceId: string;
	chatId: string;
	/** Chat-wide snapshot watermark from the first REST history page. */
	after: number;
	/** Narrow the stream to one sub-agent thread. */
	thread?: string | null;
	fetchImpl?: typeof fetch;
	reconnectDelayMs?: number;
}

export function chatTimelineStreamUrl(
	baseUrl: string,
	workspaceId: string,
	chatId: string,
	options: { after: number; thread?: string | null }
): string {
	const base = baseUrl.replace(/\/+$/, '');
	const params = new URLSearchParams();
	params.set('after', String(options.after));
	if (options.thread) params.set('thread', options.thread);
	return `${base}/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/timeline/stream?${params}`;
}

/** Tail after the REST watermark; reconnect after the last delivered sequence. */
export class TimelineStream {
	private readonly fetchImpl: typeof fetch;
	private readonly reconnectDelayMs: number;
	private closed = false;
	private cursor: number | null = null;
	private activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
	private readonly request = new AbortController();

	constructor(private readonly options: TimelineStreamOptions) {
		if (!Number.isSafeInteger(options.after) || options.after < 0) throw new RangeError('Timeline after must be a non-negative safe integer');
		this.fetchImpl = options.fetchImpl ?? fetch;
		this.reconnectDelayMs = options.reconnectDelayMs ?? 1000;
	}

	stop(): void {
		this.closed = true;
		this.request.abort();
		void this.activeReader?.cancel().catch(() => undefined);
	}

	async start(onItem: (item: TimelineItemWire) => void): Promise<void> {
		const url = chatTimelineStreamUrl(
			this.options.baseUrl,
			this.options.workspaceId,
			this.options.chatId,
			{ after: this.options.after, thread: this.options.thread }
		);
		while (!this.closed) {
			try {
				const headers: Record<string, string> = { Accept: 'text/event-stream' };
				if (this.cursor !== null) headers['Last-Event-ID'] = String(this.cursor);
				const response = await this.fetchImpl(url, { headers, signal: this.request.signal });
				if (this.closed) return;
				if (response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429) {
					throw new ThesisApiError(response.status, 'timeline/stream');
				}
				if (response.ok && response.body) await this.pump(response.body, onItem);
			} catch (error) {
				if (this.closed) return;
				if (error instanceof WireDecodeError || error instanceof ThesisApiError) throw error;
			}
			if (!this.closed) {
				await new Promise<void>((resolve) => {
					const finish = () => {
						clearTimeout(timer);
						this.request.signal.removeEventListener('abort', finish);
						resolve();
					};
					const timer = setTimeout(finish, this.reconnectDelayMs);
					this.request.signal.addEventListener('abort', finish, { once: true });
				});
			}
		}
	}

	private async pump(body: ReadableStream<Uint8Array>, onItem: (item: TimelineItemWire) => void): Promise<void> {
		const reader = body.getReader();
		this.activeReader = reader;
		const decoder = new TextDecoder();
		let buffer = '';
		try {
			for (;;) {
				const { done, value } = await reader.read();
				if (this.closed) return;
				if (done) return;
				const parsed = parseSseChunk(buffer, decoder.decode(value, { stream: true }));
				buffer = parsed.buffer;
				for (const frame of parsed.frames) {
					if (this.closed) return;
					const item = decodeWire('TimelineItemWire', frame.data, 'timeline/stream');
					if (!Number.isSafeInteger(item.seq)) throw new WireDecodeError('TimelineItemWire', 'timeline/stream', 'Unsafe sequence');
					if (item.seq <= (this.cursor ?? this.options.after)) continue;
					onItem(item);
					this.cursor = item.seq;
				}
			}
		} finally {
			if (this.activeReader === reader) this.activeReader = null;
			await reader.cancel().catch(() => undefined);
			reader.releaseLock();
		}
	}
}
