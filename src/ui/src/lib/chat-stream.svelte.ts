import { apiBaseUrl, fixtureMode } from './api';
import {
	ChatEventStream,
	LocalCursorStore,
	chatMessageOf,
	providerDeltaOf,
	type ProviderDelta
} from './sse';
import type { ChatMessage } from './types';

/** Append a message unless it is already present (stream frames can repeat). */
export function appendMessage(
	messages: ChatMessage[],
	message: ChatMessage
): ChatMessage[] {
	return messages.some((existing) => existing.message_id === message.message_id)
		? messages
		: [...messages, message];
}

/** The live streaming state: the reply id, visible answer, and reasoning trace. */
export interface StreamingState {
	id: string | null;
	text: string;
	reasoning: string;
}

/**
 * Fold one `provider.delta` frame into the visible streaming state.
 *
 * Reasoning-`kind` chunks append to `reasoning` and are never mixed into the
 * answer; answer chunks (no `kind`) append to `text`. A `start` frame resets
 * both buffers for a new reply, and a `done` frame clears them.
 */
export function applyProviderDelta(current: StreamingState, delta: ProviderDelta): StreamingState {
	if (delta.start) {
		return { id: delta.messageId ?? null, text: '', reasoning: '' };
	}
	if (delta.done) {
		return { id: null, text: '', reasoning: '' };
	}
	if (delta.text) {
		if (delta.kind === 'reasoning') {
			return { ...current, reasoning: current.reasoning + delta.text };
		}
		return { ...current, text: current.text + delta.text };
	}
	return current;
}

export interface ChatStreamOptions {
	/** Resolve the workspace per call, so the controller tracks reactive props. */
	workspaceId: () => string;
	/** Called for each persisted `chat.message` frame (dedupe with appendMessage). */
	onMessage: (message: ChatMessage) => void;
}

/**
 * Owns one chat-scoped SSE subscription for an assistant reply.
 *
 * Streamed `provider.delta` text lands in the reactive `streamingText` /
 * `streamingId` fields; reasoning-`kind` deltas land in `streamingReasoning`
 * instead, so the visible answer never includes the model's thoughts.
 * Persisted `chat.message` frames are forwarded to `onMessage`. Shared by the
 * conversations page and the copilot pane so the live-update behavior
 * (including same-origin base URL resolution) exists once.
 */
export class ChatStreamController {
	streamingText = $state('');
	streamingReasoning = $state('');
	streamingId = $state<string | null>(null);
	#stream: ChatEventStream | null = null;
	#cursorStore = new LocalCursorStore();
	#workspaceId: () => string;
	#onMessage: (message: ChatMessage) => void;

	constructor({ workspaceId, onMessage }: ChatStreamOptions) {
		this.#workspaceId = workspaceId;
		this.#onMessage = onMessage;
	}

	/** Whether a reply is currently streaming. */
	get active(): boolean {
		return (
			this.streamingId !== null ||
			this.streamingText.length > 0 ||
			this.streamingReasoning.length > 0
		);
	}

	/** Subscribe to the given chat's stream, replacing any current one. */
	start(chatId: string): void {
		this.stop();
		if (fixtureMode) {
			// Fixture mode renders seeded replies; the SSE client is real code
			// and runs against the API in real-fetch mode.
			return;
		}
		const stream = new ChatEventStream({
			baseUrl: apiBaseUrl,
			workspaceId: this.#workspaceId(),
			chatId,
			cursorStore: this.#cursorStore
		});
		this.#stream = stream;
		void stream.start((frame) => {
			const delta = providerDeltaOf(frame);
			if (delta) {
				const next = applyProviderDelta(
					{
						id: this.streamingId,
						text: this.streamingText,
						reasoning: this.streamingReasoning
					},
					delta
				);
				this.streamingId = next.id;
				this.streamingText = next.text;
				this.streamingReasoning = next.reasoning;
				return;
			}
			const message = chatMessageOf(frame);
			if (message) this.#onMessage(message);
		});
	}

	/** Unsubscribe and clear any in-flight streaming text. */
	stop(): void {
		this.#stream?.stop();
		this.#stream = null;
		this.clearStreaming();
	}

	private clearStreaming(): void {
		this.streamingId = null;
		this.streamingText = '';
		this.streamingReasoning = '';
	}
}
