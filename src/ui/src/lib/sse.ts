/**
 * SSE consumption of GET /v1/workspaces/{ws}/executions/{id}/events/stream.
 *
 * Contract (fca-thesis.md): durable execution events carry the replay
 * cursor as the SSE `id`, so the client stores the last `id` and reconnects
 * with `Last-Event-ID`. Ephemeral frames (`chat.message`, `provider.delta`)
 * carry no `id`, are re-snapshotted on (re)connect, and are deduplicated by
 * message ID; they are never replayed after reconnect.
 *
 * The client is fetch-stream based (not bare EventSource) so the reconnect
 * request can carry the persisted cursor as a header, and so tests can
 * inject a fake stream implementation.
 */
import type { ChatMessage, ExecutionEvent } from './types';

export const CHAT_MESSAGE_EVENT_KIND = 'chat.message';
export const PROVIDER_DELTA_EVENT_KIND = 'provider.delta';

export interface SseFrame {
	/** Replay cursor; null for ephemeral frames. */
	id: string | null;
	event: string;
	data: Record<string, unknown>;
}

export interface CursorStore {
	get(executionId: string): number;
	set(executionId: string, cursor: number): void;
}

export class LocalCursorStore implements CursorStore {
	constructor(private readonly storage: Storage | null = typeof localStorage !== 'undefined' ? localStorage : null) {}

	get(executionId: string): number {
		if (!this.storage) return 0;
		const raw = this.storage.getItem(cursorKey(executionId));
		const value = raw === null ? Number.NaN : Number(raw);
		return Number.isInteger(value) && value >= 0 ? value : 0;
	}

	set(executionId: string, cursor: number): void {
		if (!this.storage) return;
		this.storage.setItem(cursorKey(executionId), String(cursor));
	}
}

function cursorKey(executionId: string): string {
	return `thesis.sse.cursor.${executionId}`;
}

export interface ExecutionStreamOptions {
	baseUrl: string;
	workspaceId: string;
	executionId: string;
	cursorStore?: CursorStore;
	fetchImpl?: typeof fetch;
	reconnectDelayMs?: number;
	/** Override the stream path (used by chat-scoped streams). */
	streamPath?: (workspaceId: string, id: string) => string;
	/** Subscribe to one sub-agent node instead of the root execution. */
	subAgentId?: string | null;
	/** Include descendant nodes' events (default: own events only). */
	includeDescendants?: boolean;
	/** Cursor persistence key; defaults to the execution id. */
	cursorKey?: string;
}

export type FrameHandler = (frame: SseFrame) => void;

/** Parse one SSE text chunk stream into frames, buffering partial lines. */
export function parseSseChunk(buffer: string, chunk: string): { frames: SseFrame[]; buffer: string } {
	let text = buffer + chunk;
	const frames: SseFrame[] = [];
	let separator: number;
	while ((separator = findFrameEnd(text)) !== -1) {
		const block = text.slice(0, separator);
		text = text.slice(separator + frameEndLength(text, separator));
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

function frameEndLength(text: string, separator: number): number {
	return text.startsWith('\r\n\r\n', separator) ? 4 : 2;
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
		return null;
	}
}

export class ExecutionEventStream {
	readonly cursorStore: CursorStore;
	private readonly fetchImpl: typeof fetch;
	private readonly reconnectDelayMs: number;
	private closed = false;
	private cursor: number;
	private activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
	private readonly seenMessages = new Set<string>();
	private readonly cursorKey: string;

	constructor(private readonly options: ExecutionStreamOptions) {
		this.cursorStore = options.cursorStore ?? new LocalCursorStore();
		this.fetchImpl = options.fetchImpl ?? fetch;
		this.reconnectDelayMs = options.reconnectDelayMs ?? 1000;
		this.cursorKey = options.cursorKey ?? options.executionId;
		this.cursor = this.cursorStore.get(this.cursorKey);
	}

	/** Current durable cursor (persisted per execution subscription). */
	get lastEventId(): number {
		return this.cursor;
	}

	/** Messages already delivered; ephemeral resnapshots are suppressed. */
	hasSeenMessage(messageId: string): boolean {
		return this.seenMessages.has(messageId);
	}

	stop(): void {
		this.closed = true;
		// Unblock a pending read so `start()` resolves promptly.
		void this.activeReader?.cancel().catch(() => undefined);
	}

	async start(onFrame: FrameHandler): Promise<void> {
		const url = this.options.streamPath
			? this.options.streamPath(this.options.workspaceId, this.options.executionId)
			: nodeStreamUrl(this.options.baseUrl, this.options.workspaceId, this.options.executionId, {
					subAgentId: this.options.subAgentId,
					includeDescendants: this.options.includeDescendants
				});
		while (!this.closed) {
			try {
				const headers: Record<string, string> = { Accept: 'text/event-stream' };
				if (this.cursor > 0) headers['Last-Event-ID'] = String(this.cursor);
				const response = await this.fetchImpl(url, { headers });
				if (this.closed) return;
				if (!response.ok || !response.body) {
					await delay(this.reconnectDelayMs);
					continue;
				}
				await this.pump(response.body, onFrame);
			} catch {
				if (this.closed) return;
			}
			if (!this.closed) await delay(this.reconnectDelayMs);
		}
	}

	private async pump(body: ReadableStream<Uint8Array>, onFrame: FrameHandler): Promise<void> {
		const reader = body.getReader();
		this.activeReader = reader;
		const decoder = new TextDecoder();
		let buffer = '';
		try {
			for (;;) {
				const { done, value } = await reader.read();
				if (this.closed) {
					await reader.cancel().catch(() => undefined);
					return;
				}
				if (done) return;
				const parsed = parseSseChunk(buffer, decoder.decode(value, { stream: true }));
				buffer = parsed.buffer;
				for (const frame of parsed.frames) this.dispatch(frame, onFrame);
			}
		} finally {
			if (this.activeReader === reader) this.activeReader = null;
			reader.releaseLock();
		}
	}

	private dispatch(frame: SseFrame, onFrame: FrameHandler): void {
		if (frame.id !== null) {
			const cursor = Number(frame.id);
			if (Number.isInteger(cursor) && cursor >= 0) {
				this.cursor = cursor;
				this.cursorStore.set(this.cursorKey, cursor);
			}
			onFrame(frame);
			return;
		}
		// Ephemeral frames: deduplicate by message ID. They are never
		// replayed by the server, but a reconnect re-snapshots the chat,
		// so suppress anything already delivered.
		const key = ephemeralKey(frame);
		if (key === null) {
			onFrame(frame);
			return;
		}
		if (this.seenMessages.has(key)) return;
		this.seenMessages.add(key);
		onFrame(frame);
	}
}

function ephemeralKey(frame: SseFrame): string | null {
	if (frame.event === CHAT_MESSAGE_EVENT_KIND) {
		const message = frame.data['message'] as ChatMessage | undefined;
		return message?.message_id ?? null;
	}
	// Provider deltas are an ordered token stream: every chunk, including the
	// `start` and `done` markers, must reach the client, so they are never
	// deduplicated. A replayed `start` frame resets the visible text before the
	// chunks re-append on reconnect.
	return null;
}

export function streamUrl(baseUrl: string, workspaceId: string, executionId: string): string {
	const base = baseUrl.replace(/\/+$/, '');
	return `${base}/v1/workspaces/${encodeURIComponent(workspaceId)}/executions/${encodeURIComponent(executionId)}/events/stream`;
}

export interface NodeStreamOptions {
	/** Subscribe to this sub-agent node instead of the root execution. */
	subAgentId?: string | null;
	/** Include descendant nodes' events (default: own events only). */
	includeDescendants?: boolean;
}

/**
 * URL for the execution or sub-agent node event stream. The root execution
 * stream is `/executions/{exec}/events/stream`; a sub-agent node is
 * `/executions/{exec}/sub_agents/{a}/events/stream`. `include_descendants`
 * is only appended when true, so the own-events default keeps the base URL
 * stable.
 */
export function nodeStreamUrl(
	baseUrl: string,
	workspaceId: string,
	executionId: string,
	options: NodeStreamOptions = {}
): string {
	const base = baseUrl.replace(/\/+$/, '');
	const node = options.subAgentId
		? `/sub_agents/${encodeURIComponent(options.subAgentId)}`
		: '';
	const query = options.includeDescendants ? '?include_descendants=true' : '';
	return `${base}/v1/workspaces/${encodeURIComponent(workspaceId)}/executions/${encodeURIComponent(executionId)}${node}/events/stream${query}`;
}

export function chatStreamUrl(baseUrl: string, workspaceId: string, chatId: string): string {
	const base = baseUrl.replace(/\/+$/, '');
	return `${base}/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/events/stream`;
}

export interface ChatStreamOptions {
	baseUrl: string;
	workspaceId: string;
	chatId: string;
	cursorStore?: CursorStore;
	fetchImpl?: typeof fetch;
	reconnectDelayMs?: number;
}

/** SSE subscription to one chat's streamed assistant reply. */
export class ChatEventStream extends ExecutionEventStream {
	constructor(options: ChatStreamOptions) {
		const { chatId, ...rest } = options;
		super({
			...rest,
			executionId: chatId,
			streamPath: (workspaceId, id) => chatStreamUrl(options.baseUrl, workspaceId, id)
		});
	}
}

export interface ProviderDelta {
	messageId?: string;
	messageIdFinal?: string;
	role?: string;
	text?: string;
	/** Chunk channel: `"reasoning"` for a model thought, absent for answer text. */
	kind?: string;
	start?: boolean;
	done?: boolean;
	error?: string;
}

/** Decode an ephemeral provider-delta frame into its delta payload. */
export function providerDeltaOf(frame: SseFrame): ProviderDelta | null {
	if (frame.event !== PROVIDER_DELTA_EVENT_KIND) return null;
	return (frame.data['delta'] as ProviderDelta | undefined) ?? null;
}

/** Decode a durable execution event frame into its record. */
export function executionEventOf(frame: SseFrame): ExecutionEvent | null {
	if (frame.id === null) return null;
	const kind = frame.data['kind'];
	if (kind !== 'execution.event') return null;
	return frame.data as unknown as ExecutionEvent;
}

/** Decode an ephemeral chat-message frame into its message record. */
export function chatMessageOf(frame: SseFrame): ChatMessage | null {
	if (frame.event !== CHAT_MESSAGE_EVENT_KIND) return null;
	return (frame.data['message'] as ChatMessage | undefined) ?? null;
}

function delay(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}
