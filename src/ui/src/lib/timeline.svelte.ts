/** One chat data owner: live stream, history, identity and ordered thread selectors. */
import { apiBaseUrl, fixtureMode, listTimelinePage } from './api';
import { untrack } from 'svelte';
import { TimelineStream } from './sse';
import { fixtureTimeline } from './fixture-timeline';
import type { ChatMessage, ExecutionEvent, TimelineItem, WorkflowExecutionRecord } from './types';
import { deriveWorkflowLive, hasActiveWorkflow } from './workflow-live';

export class TimelineController {
	items = $state.raw<TimelineItem[]>([]);
	connected = $state(false);
	initialLoading = $state(false);
	streamError = $state('');
	history = $state<Record<string, { loading: boolean; complete: boolean; error: string }>>({});
	#stream: TimelineStream | null = null;
	#unsubscribe: (() => void) | null = null;
	#workspaceId = '';
	#chatId = '';
	#loads = new Map<string, AbortController>();
	#generation = 0;

	#index = $derived.by(() => {
		const messages: ChatMessage[] = [];
		const events: ExecutionEvent[] = [];
		const messagesByThread = new Map<string, ChatMessage[]>();
		const eventsByThread = new Map<string, ExecutionEvent[]>();
		const eventsByExecution = new Map<string, ExecutionEvent[]>();
		const snapshots = new Map<string, { execution: WorkflowExecutionRecord; seq: number }>();
		let nodeRevision = '';
		for (const item of this.items) {
			const thread = item.threadId ?? (item.kind === 'message'
				? String(item.payload.data?.subAgentId ?? 'root')
				: item.kind === 'event' ? String(item.payload.payload.subAgentId ?? 'root') : 'root');
			if (item.kind === 'message') {
				const message = { ...item.payload, seq: item.seq, threadId: item.threadId };
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
			} else if (!snapshots.has(item.payload.execution_id)) snapshots.set(item.payload.execution_id, { execution: item.payload, seq: item.seq });
		}
		return { messages, events, messagesByThread, eventsByThread, eventsByExecution, snapshots, nodeRevision };
	});
	workflows = $derived(deriveWorkflowLive(this.#index.messages, this.#index.eventsByExecution, this.#index.snapshots));
	get anyActiveWorkflow(): boolean { return hasActiveWorkflow(this.workflows); }
	get nodeRevision(): string { return this.#index.nodeRevision; }
	eventsForExecution(id: string): ExecutionEvent[] { return this.#index.eventsByExecution.get(id) ?? []; }

	messagesFor(threadId?: string): ChatMessage[] {
		return threadId === undefined ? this.#index.messages : this.#index.messagesByThread.get(threadId) ?? [];
	}

	eventsFor(threadId?: string): ExecutionEvent[] {
		return threadId === undefined ? this.#index.events : this.#index.eventsByThread.get(threadId) ?? [];
	}

	get messages(): ChatMessage[] { return this.messagesFor(); }
	get events(): ExecutionEvent[] { return this.eventsFor(); }

	append(item: TimelineItem): void { this.merge([item]); }

	merge(items: TimelineItem[]): void {
		const byId = new Map(this.items.map((item) => [item.id, item]));
		for (const item of items) byId.set(item.id, item);
		this.items = [...byId.values()].sort((a, b) => a.seq - b.seq);
	}

	/** Partial pages remain visible on failure; aborted and previous-chat replies cannot merge. */
	async loadHistory(threadId = 'root'): Promise<void> {
		if (!this.#chatId || untrack(() => this.history[threadId]?.complete) || this.#loads.has(threadId)) return;
		const request = new AbortController();
		this.#loads.set(threadId, request);
		this.history[threadId] = { loading: true, complete: false, error: '' };
		let before: number | null = null;
		try {
			do {
				const page = await listTimelinePage(this.#workspaceId, this.#chatId, threadId === 'root' ? null : threadId, before, request.signal);
				if (request.signal.aborted || this.#loads.get(threadId) !== request) return;
				this.merge(page.items);
				if (threadId === 'root' && before === null) {
					this.initialLoading = false;
					this.#startStream(page.after);
				}
				if (page.nextBefore !== null && before !== null && page.nextBefore >= before) {
					throw new Error('Timeline history cursor did not advance');
				}
				before = page.nextBefore;
			} while (before !== null);
			this.history[threadId].complete = true;
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
		this.initialLoading = false;
		this.history = {};
		this.streamError = '';
	}
}
