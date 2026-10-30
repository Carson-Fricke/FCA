/** Shared conversation lifecycle; hosts supply only navigation and presentation. */
import { conversationTitleFrom, createChat, deleteChat, listChats, postMessage, type PostMessageInput } from './api';
import { TimelineController } from './timeline.svelte';
import type { ChatThread, SteeringPercolationTarget } from './types';

export const DRAFT_CHAT_ID = '__draft_conversation__';

export interface ChatHostNavigation {
	read(): { chatId: string | null; scope: string | null; hash: string | null };
	select(chatId: string | null): void;
	scope(scope: string): void;
}

export class ChatHostController {
	chats = $state<ChatThread[]>([]);
	chatId = $state<string | null>(null);
	drafting = $state(false);
	loadingChats = $state(false);
	percolations = $state<Record<string, SteeringPercolationTarget[]>>({});
	busy = $state(false);
	error = $state('');
	scopeGrant = $state<string | null>(null);
	hashGrant = $state<string | null>(null);
	workspaceId = $state('');
	readonly timeline = new TimelineController();
	#workspaceId = '';
	#generation = 0;
	#selection = 0;
	#requests = new AbortController();
	#failedSend: { chatId: string | null; input: string; messageId: string } | null = null;

	constructor(private readonly navigation?: ChatHostNavigation) {}

	get selectedChat(): ChatThread | null {
		return this.drafting ? {
			chat_id: DRAFT_CHAT_ID, workspace_id: this.#workspaceId, title: 'New conversation',
			conversation_id: DRAFT_CHAT_ID, root_conversation_id: DRAFT_CHAT_ID, parent_conversation_id: null,
			state: 'active', kind: 'chat', owner_actor_id: 'local-operator',
			created_at: '', updated_at: null, investigation_ids: [], strategy_ids: [],
			created_by: 'local-operator', schema_version: 1, content_hash: '', links: {}, input_consumed_positions: {}
		} : this.chats.find((chat) => chat.chat_id === this.chatId) ?? null;
	}
	get visibleChats(): ChatThread[] {
		return this.drafting && this.selectedChat ? [this.selectedChat, ...this.chats] : this.chats;
	}

	async open(workspaceId: string): Promise<void> {
		this.stop();
		this.timeline.reset();
		const generation = this.#generation;
		const selection = this.#selection;
		this.#workspaceId = workspaceId;
		this.workspaceId = workspaceId;
		this.#requests = new AbortController();
		this.chats = [];
		this.chatId = null;
		this.drafting = false;
		this.percolations = {};
		this.error = '';
		this.loadingChats = true;
		this.scopeGrant = null;
		this.hashGrant = null;
		const initial = this.navigation?.read();
		try {
			const chats = await listChats(workspaceId, this.#requests.signal);
			if (generation !== this.#generation) return;
			this.chats = chats;
			if (selection !== this.#selection) return;
			const id = chats.find((chat) => chat.chat_id === initial?.chatId)?.chat_id ?? chats[0]?.chat_id ?? null;
			this.select(id);
			if (!id) this.drafting = true;
			if (id === initial?.chatId) {
				this.scopeGrant = initial.scope;
				this.hashGrant = initial.hash;
			}
		} catch (error) {
			if (generation === this.#generation) this.error = this.failure(error, 'Conversations could not be loaded');
		} finally {
			if (generation === this.#generation) this.loadingChats = false;
		}
	}

	select(chatId: string | null): void {
		if (chatId !== null && chatId === this.chatId && !this.drafting) return;
		++this.#selection;
		this.timeline.reset();
		this.chatId = chatId;
		this.drafting = false;
		this.scopeGrant = null;
		this.hashGrant = null;
		this.error = '';
		this.navigation?.select(chatId);
		if (!chatId) return;
		this.timeline.start(this.#workspaceId, chatId);
	}

	newConversation(): void {
		this.select(null);
		this.drafting = true;
	}

	syncScope(scope: string): void { this.navigation?.scope(scope); }

	async remove(chatId: string): Promise<boolean> {
		const generation = this.#generation;
		this.error = '';
		try {
			await deleteChat(this.#workspaceId, chatId, this.#requests.signal);
			if (generation !== this.#generation) return false;
			this.chats = this.chats.filter((chat) => chat.chat_id !== chatId);
			if (this.chatId === chatId) this.select(this.chats[0]?.chat_id ?? null);
			return true;
		} catch (error) {
			if (generation === this.#generation) this.error = this.failure(error, 'Delete failed');
			return false;
		}
	}

	async send(input: PostMessageInput): Promise<void> {
		if (this.busy) throw new Error('A message is already being sent');
		const generation = this.#generation;
		const selection = this.#selection;
		const workspaceId = this.#workspaceId;
		const signal = this.#requests.signal;
		const inputKey = JSON.stringify(input);
		const failed = this.#failedSend;
		const messageId = failed?.chatId === this.chatId && failed.input === inputKey
			? failed.messageId : `message-${crypto.randomUUID()}`;
		this.timeline.beginSend(workspaceId, this.chatId ?? DRAFT_CHAT_ID, input.body, 'root', messageId);
		this.busy = true;
		let id = this.chatId;
		try {
			if (!id) {
				const created = await createChat(workspaceId, { title: conversationTitleFrom(input.body) }, signal);
				signal.throwIfAborted();
				id = created.chat_id;
				if (generation === this.#generation) {
					this.chats = [created, ...this.chats];
					// Keep the persisted thread selected even if its first POST fails.
					if (selection === this.#selection) {
						this.select(id);
						this.timeline.beginSend(workspaceId, id, input.body, 'root', messageId);
					}
				}
			}
			const result = await postMessage(workspaceId, id, { ...input, messageId }, signal);
			if (generation === this.#generation && id === this.chatId) {
				this.timeline.acknowledgeSend(result.message);
				this.#failedSend = null;
			}
			if (generation === this.#generation && result.percolated.length) {
				this.percolations = { ...this.percolations, [result.message.message_id]: result.percolated };
			}
		} catch (error) {
			if (generation === this.#generation && id === this.chatId) {
				this.timeline.failSend(messageId);
				this.#failedSend = { chatId: id, input: inputKey, messageId };
			}
			throw error;
		} finally {
			if (generation === this.#generation) this.busy = false;
		}
	}

	stop(): void {
		this.#requests.abort();
		this.#failedSend = null;
		this.busy = false;
		this.#generation += 1;
		this.#selection += 1;
		this.timeline.stop();
	}

	private failure(error: unknown, fallback: string): string {
		return error instanceof Error ? error.message : fallback;
	}
}
