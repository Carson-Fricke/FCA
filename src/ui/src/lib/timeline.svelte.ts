/**
 * Client controller for one chat's unified Cosmos timeline.
 *
 * One SSE connection carries every message and execution event for the chat in
 * a single `seq` order. The controller exposes them as the `messages` and
 * `events` projections the chat surface already renders, so a sub-agent
 * drill-in is just a filter over the same buffered items — no second stream.
 */
import { apiBaseUrl, fixtureMode } from './api';
import { TimelineStream } from './sse';
import type { ChatMessage, ExecutionEvent, TimelineItem } from './types';

export class TimelineController {
	items = $state<TimelineItem[]>([]);
	connected = $state(false);
	#stream: TimelineStream | null = null;

	/** Root + sub-agent chat messages, newest last. */
	get messages(): ChatMessage[] {
		return this.items
			.filter((item) => item.kind === 'message')
			.map((item) => item.payload as unknown as ChatMessage);
	}

	/** Execution events (tools, sub-agent activity, gates), newest last. */
	get events(): ExecutionEvent[] {
		return this.items
			.filter((item) => item.kind === 'event')
			.map((item) => item.payload as unknown as ExecutionEvent);
	}

	/** Merge one item, deduplicating by id and keeping chronological order. */
	append(item: TimelineItem): void {
		if (this.items.some((existing) => existing.id === item.id)) return;
		this.items = [...this.items, item].sort((a, b) => a.seq - b.seq);
	}

	/** Subscribe to one chat; the first connect backfills the last N items. */
	start(workspaceId: string, chatId: string, limit = 200): void {
		this.stop();
		this.items = [];
		if (fixtureMode) {
			this.connected = true;
			return;
		}
		const stream = new TimelineStream({
			baseUrl: apiBaseUrl,
			workspaceId,
			chatId,
			limit
		});
		this.#stream = stream;
		this.connected = true;
		void stream.start((item) => this.append(item));
	}

	stop(): void {
		this.#stream?.stop();
		this.#stream = null;
		this.connected = false;
	}

	/** Stop the current stream and clear the visible conversation contents. */
	reset(): void {
		this.stop();
		this.items = [];
	}
}
