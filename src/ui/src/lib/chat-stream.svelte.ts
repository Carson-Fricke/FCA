import { apiBaseUrl, apiBearerToken, fixtureMode } from './api';
import { ChatEventStream, LocalCursorStore, chatMessageOf, providerDeltaOf } from './sse';
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
 * `streamingId` fields; persisted `chat.message` frames are forwarded to
 * `onMessage`. Shared by the conversations page and the copilot pane so the
 * live-update behavior (including the bearer and base URL) exists once.
 */
export class ChatStreamController {
	streamingText = $state('');
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
		return this.streamingId !== null || this.streamingText.length > 0;
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
			cursorStore: this.#cursorStore,
			bearerToken: apiBearerToken
		});
		this.#stream = stream;
		void stream.start((frame) => {
			const delta = providerDeltaOf(frame);
			if (delta) {
				if (delta.start) {
					this.streamingId = delta.messageId ?? null;
					this.streamingText = '';
				}
				if (delta.text) this.streamingText += delta.text;
				if (delta.done) this.clearStreaming();
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
	}
}
