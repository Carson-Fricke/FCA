/** One chat data owner: live stream, history, identity and ordered thread selectors. */
import { apiBaseUrl, fixtureMode, getTimelineDetail, listConversationOutline, listTimelinePage, listWorkflowMetadata } from './api';
import type { ConversationOutlineEntryWire } from './generated';
import { untrack } from 'svelte';
import { TimelineStream } from './sse';
import { fixtureTimeline } from './fixture-timeline';
import type { ChatMessage, ExecutionEvent, TimelineItem, WorkflowExecutionRecord } from './types';
import { deriveWorkflowLive, hasActiveWorkflow } from './workflow-live';
import { toChatMessage } from './transcript';
import { ModelOutputProjection } from './model-output';

export class TimelineController {
	items = $state.raw<TimelineItem[]>([]);
	#materialItems = $state.raw<TimelineItem[]>([]);
	#outputs = $state.raw<ChatMessage[]>([]);
	#outputProjection = new ModelOutputProjection();
	#itemsById = new Map<string, TimelineItem>();
	#localMessages = $state.raw<ChatMessage[]>([]);
	connected = $state(false);
	initialLoading = $state(false);
	streamError = $state('');
	history = $state<Record<string, { loading: boolean; loaded: boolean; complete: boolean; error: string; nextBefore: number | null }>>({});
	#metadata = $state.raw<Map<string, { execution: WorkflowExecutionRecord; seq: number }>>(new Map());
	metadataError = $state('');
	#metadataRequest: AbortController | null = null;
	#stream: TimelineStream | null = null;
	#unsubscribe: (() => void) | null = null;
	#workspaceId = '';
	#chatId = '';
	#loads = new Map<string, AbortController>();
	#generation = 0;
	outlineEntries = $state.raw<ConversationOutlineEntryWire[]>([]);
	outlineLoading = $state(false);
	outlineHasMore = $state(false);
	outlineError = $state('');
	#outlineEnabled = false;
	#outlineAfter = 0;
	#outlineRequest: AbortController | null = null;
	#outlinePending = false;
	#anchorRequest: AbortController | null = null;
	#details = new Map<number, { request: AbortController; promise: Promise<void> }>();
	#outlineCache = new Map<string, { entries: ConversationOutlineEntryWire[]; after: number; more: boolean }>();
	#cacheKey(): string { return JSON.stringify([apiBaseUrl, fixtureMode, this.#workspaceId, this.#chatId]); }

	#index = $derived.by(() => {
		const messages: ChatMessage[] = [];
		const events: ExecutionEvent[] = [];
		const messagesByThread = new Map<string, ChatMessage[]>();
		const eventsByThread = new Map<string, ExecutionEvent[]>();
		const eventsByExecution = new Map<string, ExecutionEvent[]>();
		const snapshots = new Map<string, { execution: WorkflowExecutionRecord; seq: number }>();
		let nodeRevision = '';
		for (const item of this.#materialItems) {
			const thread = item.threadId ?? (item.kind === 'message'
				? String(item.payload.data?.subAgentId ?? 'root')
				: item.kind === 'event' ? String(item.payload.payload.subAgentId ?? 'root') : 'root');
			if (item.kind === 'message') {
				const message = { ...toChatMessage(item.payload), seq: item.seq, threadId: item.threadId };
				messages.push(message);
				const bucket = messagesByThread.get(thread) ?? [];
				bucket.push(message);
				messagesByThread.set(thread, bucket);
			} else if (item.kind === 'event') {
				const event = { ...item.payload, seq: item.seq, threadId: item.threadId };
				events.push(event);
				const bucket = eventsByThread.get(thread) ?? [];
				bucket.push(event);
				eventsByThread.set(thread, bucket);
				const executionBucket = eventsByExecution.get(event.executionId) ?? [];
				executionBucket.push(event);
				eventsByExecution.set(event.executionId, executionBucket);
				if (['workflow.phase', 'workflow.started', 'sub_agent.spawned', 'sub_agent.completed', 'sub_agent.failed', 'sub_agent.cancelled', 'sub_agent.needs_input'].includes(event.eventType) ||
					'acceptsInput' in event.payload || 'capabilities' in event.payload) nodeRevision = `${item.id}:${item.seq}`;
			} else if (item.kind === 'execution' && !snapshots.has(item.payload.execution_id)) snapshots.set(item.payload.execution_id, { execution: item.payload, seq: item.seq });
		}
		return { messages, events, messagesByThread, eventsByThread, eventsByExecution, snapshots, nodeRevision };
	});
	workflows = $derived(deriveWorkflowLive(this.#index.messages, this.#index.eventsByExecution, this.#index.snapshots, this.#metadata));
	get anyActiveWorkflow(): boolean { return hasActiveWorkflow(this.workflows); }
	get nodeRevision(): string { return this.#index.nodeRevision; }
	eventsForExecution(id: string): ExecutionEvent[] { return this.#index.eventsByExecution.get(id) ?? []; }

	messagesFor(threadId?: string): ChatMessage[] {
		const durable = threadId === undefined ? this.#index.messages : this.#index.messagesByThread.get(threadId) ?? [];
		const ids = new Set(this.#index.messages.map((message) => message.message_id));
		return [...durable, ...[...this.#outputs, ...this.#localMessages].filter((message) => !ids.has(message.message_id) &&
			(threadId === undefined || message.threadId === threadId))].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
	}

	/** Presentation only: pending sends never advance the durable stream cursor. */
	beginSend(workspaceId: string, chatId: string, body: string, threadId = 'root', messageId = `message-${crypto.randomUUID()}`): string {
		this.#localMessages = [...this.#localMessages.filter((message) => message.message_id !== messageId), {
			message_id: messageId, workspace_id: workspaceId, chat_id: chatId,
			actor_id: 'you', role: 'user', body, state: 'accepted', attachment_ids: [],
			created_at: new Date().toISOString(), updated_at: null, execution_id: null, intent_epoch: 0,
			kind: threadId === 'root' ? 'message' : 'sub_agent', threadId,
			data: threadId === 'root' ? null : { subAgentId: threadId },
			seq: Number.POSITIVE_INFINITY, deliveryState: 'sending'
		}];
		return messageId;
	}

	acknowledgeSend(message: ChatMessage): void {
		const threadId = message.threadId ?? String(message.data?.subAgentId ?? 'root');
		if (this.#index.messages.some((durable) => durable.message_id === message.message_id)) {
			this.#localMessages = this.#localMessages.filter((local) => local.message_id !== message.message_id);
			return;
		}
		this.#localMessages = [...this.#localMessages.filter((local) => local.message_id !== message.message_id), {
			...message, threadId, seq: message.seq ?? Number.POSITIVE_INFINITY
		}];
	}

	failSend(messageId: string): void {
		this.#localMessages = this.#localMessages.map((message) => message.message_id === messageId
			? { ...message, deliveryState: 'failed' } : message);
	}

	eventsFor(threadId?: string): ExecutionEvent[] {
		return threadId === undefined ? this.#index.events : this.#index.eventsByThread.get(threadId) ?? [];
	}

	get messages(): ChatMessage[] { return this.messagesFor(); }
	get events(): ExecutionEvent[] { return this.eventsFor(); }

	append(item: TimelineItem): void { this.merge([item]); }

	merge(items: TimelineItem[]): void {
		items = items.filter(item => {
			const existing = this.#itemsById.get(item.id);
			return !(item.kind === 'message' && 'receipt' in item.payload && existing?.kind === 'message' && !('receipt' in existing.payload));
		});
		let last = this.items.at(-1)?.seq ?? 0;
		const appendOnly = items.every((item) => {
			const append = !this.#itemsById.has(item.id) && item.seq > last;
			last = item.seq;
			return append;
		});
		for (const item of items) this.#itemsById.set(item.id, item);
		this.items = appendOnly ? [...this.items, ...items] : [...this.#itemsById.values()].sort((a, b) => a.seq - b.seq);
		if (items.some((item) => item.kind !== 'output')) this.#materialItems = this.items.filter((item) => item.kind !== 'output');
		this.#outputs = this.#outputProjection.merge(items);
		const messageIds = new Set(this.items.flatMap((item) => item.kind === 'message' ? [item.payload.message_id] : []));
		this.#localMessages = this.#localMessages.filter((message) => !messageIds.has(message.message_id));
		if (this.#outlineEnabled && !this.outlineHasMore && items.some((item) => item.kind === 'message')) void this.refreshOutline();
	}

	/** Opt-in read model for the fullscreen host; no extra stream or transcript cache. */
	enableOutline(): void {
		this.#outlineEnabled = true;
		const cached = this.#outlineCache.get(this.#cacheKey());
		if (cached && !this.outlineEntries.length) {
			this.outlineEntries = cached.entries;
			this.#outlineAfter = cached.after;
			this.outlineHasMore = cached.more;
		}
		void this.refreshOutline();
	}

	async refreshOutline(): Promise<void> {
		if (!this.#chatId || !this.#outlineEnabled) return;
		if (this.#outlineRequest) { this.#outlinePending = true; return; }
		const request = new AbortController();
		this.#outlineRequest = request;
		this.outlineLoading = true;
		this.outlineError = '';
		try {
			const page = await listConversationOutline(this.#workspaceId, this.#chatId, this.#outlineAfter, request.signal);
			if (request.signal.aborted || this.#outlineRequest !== request) return;
			if (page.nextAfter !== null && page.nextAfter <= this.#outlineAfter) throw new Error('Conversation outline cursor did not advance');
			this.outlineEntries = [...new Map([...this.outlineEntries, ...page.items].map((item) => [item.id, item])).values()];
			this.#outlineAfter = page.after;
			this.outlineHasMore = page.nextAfter !== null;
			const key = this.#cacheKey();
			this.#outlineCache.delete(key);
			this.#outlineCache.set(key, { entries: this.outlineEntries, after: this.#outlineAfter, more: this.outlineHasMore });
			if (this.#outlineCache.size > 8) this.#outlineCache.delete(this.#outlineCache.keys().next().value!);
		} catch (error) {
			if (!request.signal.aborted) this.outlineError = error instanceof Error ? error.message : 'Conversation outline could not be loaded.';
		} finally {
			if (this.#outlineRequest === request) {
				this.#outlineRequest = null;
				this.outlineLoading = false;
				if (this.#outlinePending) { this.#outlinePending = false; if (!this.outlineHasMore) void this.refreshOutline(); }
			}
		}
	}

	/** Detail reads join by canonical identity and never update the live cursor. */
	loadDetails(sequence: number): Promise<void> {
		const active = this.#details.get(sequence);
		if (active) return active.promise;
		const existing = this.items.find(item => item.seq === sequence);
		if (existing?.kind === 'message' && !('receipt' in existing.payload)) return Promise.resolve();
		const request = new AbortController();
		const generation = this.#generation;
		const promise = getTimelineDetail(this.#workspaceId, this.#chatId, sequence, request.signal).then(item => {
			if (!request.signal.aborted && generation === this.#generation) this.merge([item]);
		}).finally(() => { if (this.#details.get(sequence)?.request === request) this.#details.delete(sequence); });
		this.#details.set(sequence, { request, promise });
		return promise;
	}

	/** Load only the selected anchor's page; keep the ordinary older-history cursor. */
	async revealOutlineEntry(entry: Pick<ConversationOutlineEntryWire, 'threadId' | 'messageId' | 'seq'>): Promise<boolean> {
		this.#anchorRequest?.abort();
		const request = new AbortController();
		this.#anchorRequest = request;
		const generation = this.#generation;
		if (!this.messagesFor(entry.threadId).some((message) => message.message_id === entry.messageId)) {
			const page = await listTimelinePage(this.#workspaceId, this.#chatId, entry.threadId, entry.seq + 1, request.signal);
			if (request.signal.aborted || generation !== this.#generation) return false;
			this.merge([...(page.outputPrefixes ?? []), ...page.items]);
		}
		return !request.signal.aborted && generation === this.#generation;
	}

	/** One selected-thread page per request; the viewport requests older pages near its top. */
	async loadHistory(threadId = 'root', older = false): Promise<void> {
		const previous = untrack(() => this.history[threadId]);
		if (!this.#chatId || previous?.complete || (previous?.loaded && !older && !previous.error) || this.#loads.has(threadId)) return;
		const request = new AbortController();
		this.#loads.set(threadId, request);
		this.history[threadId] = { loaded: previous?.loaded ?? false, loading: true, complete: false, error: '', nextBefore: previous?.nextBefore ?? null };
		const before = previous?.nextBefore ?? null;
		try {
			const page = await listTimelinePage(this.#workspaceId, this.#chatId, threadId, before, request.signal);
			if (request.signal.aborted || this.#loads.get(threadId) !== request) return;
			if (page.nextBefore !== null && before !== null && page.nextBefore >= before) throw new Error('Timeline history cursor did not advance');
			this.merge([...(page.outputPrefixes ?? []), ...page.items]);
			if (threadId === 'root' && before === null) {
				this.initialLoading = false;
				this.#startStream(page.after);
			}
			this.history[threadId].loaded = true;
			this.history[threadId].nextBefore = page.nextBefore;
			this.history[threadId].complete = page.nextBefore === null;
		} catch (error) {
			if (!request.signal.aborted && this.#loads.get(threadId) === request) this.history[threadId].error = error instanceof Error ? error.message : 'Conversation history could not be loaded.';
		} finally {
			if (this.#loads.get(threadId) === request) {
				if (threadId === 'root') this.initialLoading = false;
				this.#loads.delete(threadId);
				this.history[threadId].loading = false;
			}
		}
	}

	async refreshMetadata(): Promise<void> {
		if (!this.#chatId || this.#metadataRequest) return;
		const request = new AbortController();
		this.#metadataRequest = request;
		try {
			const metadata = await listWorkflowMetadata(this.#workspaceId, this.#chatId, request.signal);
			if (request.signal.aborted || this.#metadataRequest !== request) return;
			this.#metadata = new Map(metadata.items.map((execution) => [execution.execution_id, { execution, seq: metadata.after }]));
			this.metadataError = '';
		} catch (error) {
			if (!request.signal.aborted) this.metadataError = error instanceof Error ? error.message : 'Workflow metadata could not be loaded.';
		} finally { if (this.#metadataRequest === request) this.#metadataRequest = null; }
	}

	cancelHistory(threadId: string): void {
		this.#loads.get(threadId)?.abort();
		this.#loads.delete(threadId);
		untrack(() => { if (this.history[threadId]) this.history[threadId].loading = false; });
		if (threadId === 'root') this.initialLoading = false;
	}

	start(workspaceId: string, chatId: string): void {
		this.reset();
		this.#workspaceId = workspaceId;
		this.#chatId = chatId;
		this.initialLoading = true;
		void this.loadHistory();
		void this.refreshMetadata();
	}

	#startStream(after: number): void {
		if (this.#stream || this.#unsubscribe) return;
		const generation = this.#generation;
		const receive = (item: TimelineItem) => {
			if (this.#generation === generation) this.append(item);
		};
		if (fixtureMode) {
			this.#unsubscribe = fixtureTimeline.subscribe(this.#workspaceId, this.#chatId, after, receive);
		} else {
			const stream = new TimelineStream({ baseUrl: apiBaseUrl, workspaceId: this.#workspaceId, chatId: this.#chatId, after });
			this.#stream = stream;
			void stream.start((item) => {
				if (this.#stream === stream) receive(item);
			}).catch((error) => {
				if (this.#stream !== stream) return;
				this.connected = false;
				this.streamError = error instanceof Error ? error.message : 'Conversation stream failed';
			});
		}
		this.connected = true;
	}

	stop(): void {
		this.#generation += 1;
		for (const { request } of this.#details.values()) request.abort();
		this.#details.clear();
		this.#outlineRequest?.abort();
		this.#outlineRequest = null;
		this.#anchorRequest?.abort();
		this.#anchorRequest = null;
		this.#outlineEnabled = false;
		this.#outlinePending = false;
		this.#metadataRequest?.abort();
		this.#metadataRequest = null;
		this.#unsubscribe?.();
		this.#unsubscribe = null;
		this.#stream?.stop();
		this.#stream = null;
		for (const thread of this.#loads.keys()) this.cancelHistory(thread);
		this.#chatId = '';
		this.connected = false;
	}

	reset(): void {
		this.stop();
		this.items = [];
		this.#materialItems = [];
		this.#outputs = [];
		this.#outputProjection = new ModelOutputProjection();
		this.#itemsById.clear();
		this.#localMessages = [];
		this.initialLoading = false;
		this.history = {};
		this.#metadata = new Map();
		this.metadataError = '';
		this.outlineEntries = [];
		this.outlineLoading = false;
		this.outlineHasMore = false;
		this.outlineError = '';
		this.#outlineAfter = 0;
		this.streamError = '';
	}
}
