/** Offline timeline storage. No agent behavior or backend admission rules are simulated. */
import { fixtures } from './fixtures';
import type { ChatMessage } from './types';
import type { TimelineItemWire as TimelineItem } from './generated';
import { decodeWire } from './wire';

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));

class FixtureTimeline {
	#items = new Map<string, TimelineItem[]>();
	#listeners = new Map<string, Set<(item: TimelineItem) => void>>();

	constructor() {
		for (const doc of Object.values(fixtures.chats)) {
			for (const chat of doc.items) {
				const execution = fixtures.executions[chat.chat_id];
				const records = [
					...(fixtures.chatMessages[chat.chat_id]?.items ?? []).map((message) => ({
						id: message.message_id, kind: 'message', ts: message.created_at,
					threadId: String(message.data?.subAgentId ?? 'root'), payload: clone(message)
					})),
					...(fixtures.executionEvents[execution?.execution_id]?.items ?? []).map((event) => ({
						id: event.eventId, kind: 'event', ts: event.occurredAt, executionId: event.executionId,
					threadId: String(event.payload.subAgentId ?? 'root'), payload: clone(event)
					})),
					...(execution?.execution_id ? [{ id: execution.execution_id, kind: 'execution', ts: execution.created_at,
					executionId: execution.execution_id, threadId: 'root', payload: clone(execution) }] : [])
				].sort((a, b) => a.ts.localeCompare(b.ts));
				this.#items.set(this.key(chat.workspace_id, chat.chat_id), records.map((item, index) => ({
					...item, workspaceId: chat.workspace_id, chatId: chat.chat_id, seq: index + 1,
					payload: item.kind === 'event' ? { ...item.payload, cursor: index + 1 } : item.payload
				})).map((item) => decodeWire('TimelineItemWire', item, 'fixture/seed')));
			}
		}
	}

	private key(workspaceId: string, chatId: string): string { return JSON.stringify([workspaceId, chatId]); }

	message(message: ChatMessage): void {
		const { receipt, detailsAvailable, seq, threadId, deliveryState, outputState, outputFailure, ...canonical } = message;
		const key = this.key(message.workspace_id, message.chat_id);
		const items = this.#items.get(key) ?? [];
		const item = decodeWire('TimelineItemWire', {
			id: message.message_id, kind: 'message', seq: (items.at(-1)?.seq ?? 0) + 1,
			chatId: message.chat_id, workspaceId: message.workspace_id, ts: message.created_at,
			threadId: String(message.data?.subAgentId ?? 'root'), payload: clone(canonical)
		}, 'fixture/message');
		items.push(item);
		this.#items.set(key, items);
		for (const listener of this.#listeners.get(key) ?? []) listener(clone(item));
	}

	page(workspaceId: string, chatId: string, thread: string | null, before: number | null) {
		const snapshot = this.#items.get(this.key(workspaceId, chatId)) ?? [];
		const items = snapshot
			.filter((item) => (thread === null || item.threadId === thread) && (before === null || item.seq < before));
		const page = items.slice(-40);
		return { items: clone(page), nextBefore: items.length > page.length ? page[0].seq : null, after: snapshot.at(-1)?.seq ?? 0 };
	}

	subscribe(workspaceId: string, chatId: string, after: number, listener: (item: TimelineItem) => void): () => void {
		const key = this.key(workspaceId, chatId);
		const listeners = this.#listeners.get(key) ?? new Set();
		listeners.add(listener);
		this.#listeners.set(key, listeners);
		for (const item of this.#items.get(key) ?? []) {
			if (item.seq > after) listener(clone(item));
		}
		return () => {
			listeners.delete(listener);
			if (!listeners.size) this.#listeners.delete(key);
		};
	}

	remove(workspaceId: string, chatId: string): void { this.#items.delete(this.key(workspaceId, chatId)); }
}

export const fixtureTimeline = new FixtureTimeline();
