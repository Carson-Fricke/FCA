<script lang="ts">
	import { goto, replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import ChatComposer from '$lib/components/chat/ChatComposer.svelte';
	import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
	import CleoPlanPanel from '$lib/components/chat/CleoPlanPanel.svelte';
	import ConversationDeleteDialog from '$lib/components/chat/ConversationDeleteDialog.svelte';
	import StreamingReply from '$lib/components/chat/StreamingReply.svelte';
	import WorkflowTimeline from '$lib/components/chat/WorkflowTimeline.svelte';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import {
		confirmConsent,
		conversationTitleFrom,
		createChat,
		deleteChat,
		getConsentProposal,
		getExecution,
		getWorkspaceState,
		listChats,
		listMessages,
		postMessage,
		replayExecutionEvents
	} from '$lib/api';
	import { apiBaseUrl, apiBearerToken, fixtureMode } from '$lib/api';
	import { ExecutionEventStream, LocalCursorStore, executionEventOf } from '$lib/sse';
	import { ChatStreamController, appendMessage } from '$lib/chat-stream.svelte';
	import { notify } from '$lib/workspace.svelte';
	import type {
		ChatMessage,
		ChatThread,
		ConsentProposal,
		EvidenceItem,
		ExecutionEvent,
		SteeringPercolationTarget,
		WorkflowExecutionRecord,
		WorkspaceStateData
	} from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let chats = $state<ChatThread[]>([]);
	let chatId = $state<string | null>(null);
	let messages = $state<ChatMessage[]>([]);
	let execution = $state<WorkflowExecutionRecord | null>(null);
	let events = $state<ExecutionEvent[]>([]);
	let percolations = $state<Record<string, SteeringPercolationTarget[]>>({});
	let proposal = $state<ConsentProposal | null>(null);
	let consentBusy = $state(false);
	let stateData = $state<WorkspaceStateData | null>(null);
	let busy = $state(false);
	/** A new conversation is a draft until its first message is sent. */
	let drafting = $state(false);
	/** The conversation queued for modal delete confirmation. */
	let deleteTarget = $state<ChatThread | null>(null);
	let cursorStore = new LocalCursorStore();
	let stream: ExecutionEventStream | null = null;
	const chatStream = new ChatStreamController({
		workspaceId: () => workspaceId,
		onMessage: (message) => {
			messages = appendMessage(messages, message);
		}
	});

	const activeChat = $derived(chats.find((c) => c.chat_id === chatId) ?? null);
	const activeExecutionId = $derived(execution?.execution_id ?? null);
	const cleoPlan = $derived(stateData?.cleo ?? null);
	const evidence = $derived(stateData?.evidence ?? []);

	$effect(() => {
		let cancelled = false;
		listChats(workspaceId).then((items) => {
			if (cancelled) return;
			chats = items;
			if (!chatId) {
				const requested = page.url.searchParams.get('chat');
				chatId = requested && items.some((c) => c.chat_id === requested)
					? requested
					: (items[0]?.chat_id ?? null);
			}
		});
		getWorkspaceState(workspaceId).then((ws) => {
			if (!cancelled) stateData = ws;
		});
		getConsentProposal(workspaceId).then((p) => {
			if (!cancelled) proposal = p;
		});
		return () => {
			cancelled = true;
		};
	});

	// Load the selected chat's snapshot: messages, execution, and the durable
	// event replay from the persisted cursor.
	$effect(() => {
		const id = chatId;
		if (!id) {
			messages = [];
			execution = null;
			events = [];
			chatStream.stop();
			return;
		}
		let cancelled = false;
		const requested = page.url.searchParams.get('chat');
		if (requested !== id) {
			replaceState(`/w/${workspaceId}/conversations?chat=${id}`, {});
		}
		listMessages(workspaceId, id).then((items) => {
			if (!cancelled) messages = items;
		});
		chatStream.start(id);
		getExecution(workspaceId, id).then((exec) => {
			if (cancelled) return;
			execution = exec;
			if (exec) {
				replayExecutionEvents(workspaceId, exec.execution_id, 0).then((replay) => {
					if (!cancelled) events = replay;
				});
				startStream(exec.execution_id);
			} else {
				stopStream();
			}
		});
		return () => {
			cancelled = true;
			chatStream.stop();
		};
	});

	function startStream(executionId: string): void {
		stopStream();
		if (fixtureMode) {
			// Fixtures feed the initial snapshot; the SSE client itself is real
			// code and runs against the API in real-fetch mode.
			return;
		}
		stream = new ExecutionEventStream({
			baseUrl: apiBaseUrl,
			workspaceId,
			executionId,
			cursorStore,
			bearerToken: apiBearerToken
		});
		void stream.start((frame) => {
			const event = executionEventOf(frame);
			if (event && !events.some((e) => e.eventId === event.eventId)) {
				events = [...events, event];
			}
		});
	}

	function stopStream(): void {
		stream?.stop();
		stream = null;
	}

	/** Start a draft conversation; it persists only when the first message is sent. */
	function newConversation(): void {
		stopStream();
		drafting = true;
		deleteTarget = null;
		chatId = null;
		messages = [];
		execution = null;
		events = [];
		replaceState(`/w/${workspaceId}/conversations`, {});
	}

	async function removeConversation(id: string): Promise<void> {
		try {
			await deleteChat(workspaceId, id);
			chats = chats.filter((chat) => chat.chat_id !== id);
			if (chatId === id) {
				stopStream();
				chatId = chats[0]?.chat_id ?? null;
				messages = [];
				execution = null;
				events = [];
				if (!chatId) drafting = false;
				replaceState(
					chatId
						? `/w/${workspaceId}/conversations?chat=${chatId}`
						: `/w/${workspaceId}/conversations`,
					{}
				);
			}
			notify('Conversation deleted');
		} catch (error) {
			notify(error instanceof Error ? error.message : 'Delete failed', true);
		}
	}

	async function confirmDelete(): Promise<void> {
		const target = deleteTarget;
		if (!target) return;
		deleteTarget = null;
		await removeConversation(target.chat_id);
	}

	async function send(input: {
		body: string;
		attachmentIds: string[];
		steeringIntent: string | null;
	}): Promise<void> {
		busy = true;
		try {
			let id = chatId;
			let created: ChatThread | null = null;
			if (!id) {
				created = await createChat(workspaceId, {
					title: conversationTitleFrom(input.body)
				});
				id = created.chat_id;
			}
			// Post before selecting so the new thread is durable when the
			// selection effect reloads the message list.
			const result = await postMessage(workspaceId, id, input);
			messages = [...messages, result.message];
			if (result.percolated.length) {
				percolations = { ...percolations, [result.message.message_id]: result.percolated };
			}
			if (created) {
				chats = [created, ...chats];
				chatId = created.chat_id;
				drafting = false;
			}
			if (input.steeringIntent && activeExecutionId) {
				notify('Steering acknowledged — durable percolation recorded');
			}
		} catch (error) {
			notify(error instanceof Error ? error.message : 'Message failed', true);
		} finally {
			busy = false;
		}
	}

	async function confirmProposal(confirmation: {
		bundleHash: string;
		environment: string;
		action: string;
		confirmationToken: string;
	}): Promise<void> {
		consentBusy = true;
		try {
			await confirmConsent(workspaceId, confirmation);
			proposal = null;
			notify('Structured consent recorded — grant issued');
		} catch (error) {
			notify(error instanceof Error ? error.message : 'Confirmation failed', true);
		} finally {
			consentBusy = false;
		}
	}

	/** Workspace evidence deep-links mentioned in a message body. Unused in
	 * the transcript since evidence scoping moved to sandboxed subagent
	 * views; kept for the workspace-state projection. */
	function evidenceFor(messageId: string): EvidenceItem[] {
		const message = messages.find((m) => m.message_id === messageId);
		if (!message) return [];
		return evidence.filter((item) => {
			if (item.kind === 'backtest') {
				return /bt-\d+/.test(message.body) && message.body.includes(item.title.match(/bt-\d+/)?.[0] ?? '∅');
			}
			return message.body.includes(item.reference) || message.body.includes(item.title);
		});
	}

	$effect(() => {
		return () => stopStream();
	});
</script>

<svelte:head><title>Conversations — Thesis</title></svelte:head>

<div class="flex h-full min-h-0 gap-4">
	<section class="flex w-72 shrink-0 flex-col rounded-xl border border-surface-700 bg-surface-900">
		<div class="flex items-center gap-2 border-b border-surface-700 px-4 py-3">
			<h2 class="text-sm font-semibold text-slate-800">Conversations</h2>
			<span class="rounded-full bg-surface-800 px-1.5 font-mono text-[10px] text-slate-500">
				{chats.length}
			</span>
			<button
				type="button"
				class="ml-auto rounded-md border border-transparent p-1 text-slate-600 transition hover:border-surface-600 hover:text-accent"
				onclick={newConversation}
				title="New conversation"
				aria-label="New conversation"
				data-testid="new-conversation"
			>
				<span aria-hidden="true">＋</span>
			</button>
		</div>
		<ul class="min-h-0 flex-1 space-y-1 overflow-y-auto p-2" data-testid="conversation-list">
			{#each chats as chat (chat.chat_id)}
				<li class="group relative">
					<button
						type="button"
						class="w-full rounded-lg px-3 py-2 pr-9 text-left transition
							{chat.chat_id === chatId
							? 'border border-accent/40 bg-accent/10'
							: 'border border-transparent hover:bg-surface-800/60'}"
						onclick={() => {
							chatId = chat.chat_id;
							drafting = false;
							deleteTarget = null;
						}}
						data-testid="conversation-item"
						data-chat={chat.chat_id}
					>
						<p class="truncate text-sm {chat.chat_id === chatId ? 'text-accent' : 'text-slate-700'}">
							{chat.title}
						</p>
						<p class="text-[11px] text-slate-500">
							{chat.kind === 'investigation' ? 'investigation' : 'chat'} · {chat.updated_at?.slice(0, 10) ?? chat.created_at.slice(0, 10)}
						</p>
					</button>
					<button
						type="button"
						class="absolute top-2 right-2 hidden rounded p-1 text-slate-500 transition group-hover:block hover:text-loss"
						title="Delete conversation"
						aria-label="Delete conversation"
						onclick={() => (deleteTarget = chat)}
						data-testid="conversation-delete"
					>
						<svg
							xmlns="http://www.w3.org/2000/svg"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
							class="h-3.5 w-3.5"
							aria-hidden="true"
						>
							<path d="M3 6h18" />
							<path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
							<path d="M18.5 6l-1 13a2 2 0 0 1-2 1.9h-7A2 2 0 0 1 6.5 19l-1-13" />
							<path d="M10 11v6M14 11v6" />
						</svg>
					</button>
				</li>
			{:else}
				<li class="px-3 py-6 text-center text-xs text-slate-500">
					Your next good idea starts with a conversation.
				</li>
			{/each}
		</ul>
		<div class="border-t border-surface-700 px-4 py-2 font-mono text-[10px] text-slate-500">
			Scoped to workspace {workspaceId}
		</div>
	</section>

	<section class="flex min-w-0 flex-1 flex-col rounded-xl border border-surface-700 bg-surface-900">
		{#if activeChat}
			<header class="flex items-center gap-2 border-b border-surface-700 px-4 py-3">
				<h1 class="truncate text-sm font-semibold text-slate-800">{activeChat.title}</h1>
				{#if activeChat.kind === 'investigation'}
					<span class="rounded-full border border-accent/40 px-2 py-0.5 font-mono text-[10px] text-accent">
						investigation
					</span>
				{/if}
				{#if activeExecutionId}
					<span class="ml-auto flex items-center gap-1.5 font-mono text-[10px] text-warn">
						<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-warn" aria-hidden="true"></span>
						execution {activeExecutionId} · {execution?.state} · phase {execution?.phase ?? '—'}
					</span>
				{/if}
			</header>

			<div class="min-h-0 flex-1 overflow-y-auto p-4" data-testid="transcript-scroll">
				<ChatTranscript
					{messages}
					{percolations}
					{proposal}
					onConfirmConsent={confirmProposal}
					onRejectConsent={() => {
						proposal = null;
						notify('Consent proposal rejected');
					}}
					{consentBusy}
				/>
				<StreamingReply
					active={chatStream.active}
					text={chatStream.streamingText}
					testId="streaming-reply"
				/>
			</div>

			<div class="border-t border-surface-700 p-3">
				<WorkflowTimeline events={events} />
			</div>
		{:else}
			<div class="flex flex-1 items-center justify-center p-6">
				<EmptyState
					title={drafting ? 'New conversation' : 'No conversation selected'}
					hint={drafting
						? 'Send a message to start this conversation.'
						: 'Start a chat from the copilot or ask the agent to draft a strategy.'}
				/>
			</div>
		{/if}

		<ChatComposer
			onSubmit={send}
			{activeExecutionId}
			{busy}
		/>
	</section>

	<aside class="hidden w-80 shrink-0 overflow-y-auto rounded-xl border border-surface-700 bg-surface-900 p-3 xl:block">
		<h2 class="mb-2 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
			CLEO projection · read-only
		</h2>
		{#if cleoPlan}
			<CleoPlanPanel plan={cleoPlan} timeline={stateData?.taskTimeline ?? []} />
		{:else}
			<EmptyState title="No CLEO plan projected" hint="Plans appear when a workflow drives this workspace." />
		{/if}
	</aside>
</div>

{#if deleteTarget}
	<ConversationDeleteDialog
		title={deleteTarget.title}
		onConfirm={confirmDelete}
		onClose={() => (deleteTarget = null)}
	/>
{/if}
