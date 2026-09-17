<script lang="ts">
	import ChatSurface from '$lib/components/chat/ChatSurface.svelte';
	import ConversationDeleteDialog from '$lib/components/chat/ConversationDeleteDialog.svelte';
	import {
		conversationTitleFrom,
		createChat,
		deleteChat,
		getExecution,
		listChats,
		postMessage
	} from '$lib/api';
	import { TimelineController } from '$lib/timeline.svelte';
	import type {
		ChatThread,
		SteeringPercolationTarget,
		WorkflowExecutionRecord
	} from '$lib/types';

	/**
	 * Collapsible right-hand assistant on every page except the full-page
	 * conversations view. The header carries icon-only controls with tooltips:
	 * new conversation, conversation history overlay, expand to the full-page
	 * view, and close. Evidence scoping and knowledge search are agent-side
	 * capabilities and never surface here.
	 */
	let {
		workspaceId,
		workspaceName = '',
		onExpand,
		onClose
	}: {
		workspaceId: string;
		workspaceName?: string;
		onExpand: () => void;
		onClose: () => void;
	} = $props();

	let chats = $state<ChatThread[]>([]);
	let chatId = $state<string | null>(null);
	let drafting = $state(false);
	let execution = $state<WorkflowExecutionRecord | null>(null);
	let busy = $state(false);
	let historyOpen = $state(false);
	/** The conversation queued for modal delete confirmation. */
	let deleteTarget = $state<ChatThread | null>(null);
	let percolations = $state<Record<string, SteeringPercolationTarget[]>>({});
	const timeline = new TimelineController();
	const messages = $derived(timeline.messages);
	const events = $derived(timeline.events);

	const activeThread = $derived(chats.find((c) => c.chat_id === chatId) ?? null);
	const DRAFT_CHAT_ID = '__draft_conversation__';
	function draftThread(): ChatThread {
		return {
			chat_id: DRAFT_CHAT_ID,
			workspace_id: workspaceId,
			title: 'New conversation',
			state: 'active',
			kind: 'chat',
			owner_actor_id: 'local-operator',
			created_at: new Date().toISOString(),
			updated_at: null,
			investigation_ids: [],
			strategy_ids: []
		};
	}
	const visibleChats = $derived(drafting ? [draftThread(), ...chats] : chats);
	const selectedThread = $derived(drafting ? draftThread() : activeThread);

	$effect(() => {
		let cancelled = false;
		listChats(workspaceId).then((items) => {
			if (cancelled) return;
			chats = items;
			if (!drafting) chatId = items[0]?.chat_id ?? null;
		});
		return () => {
			cancelled = true;
		};
	});

	$effect(() => {
		const id = chatId;
		if (!id) {
			execution = null;
			timeline.stop();
			return;
		}
		let cancelled = false;
		timeline.start(workspaceId, id);
		getExecution(workspaceId, id).then((exec) => {
			if (!cancelled) execution = exec;
		});
		return () => {
			cancelled = true;
			timeline.stop();
		};
	});

	/** Start a draft conversation; it persists only when the first message is sent. */
	function newConversation(): void {
		timeline.reset();
		drafting = true;
		chatId = null;
		historyOpen = false;
		deleteTarget = null;
	}

	async function removeConversation(id: string): Promise<void> {
		try {
			await deleteChat(workspaceId, id);
			chats = chats.filter((thread) => thread.chat_id !== id);
			if (chatId === id) {
				chatId = chats[0]?.chat_id ?? null;
				drafting = false;
			}
		} catch {
			// surfaced by toast in the full conversations view; copilot stays quiet
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
			const result = await postMessage(workspaceId, id, input);
			if (created) {
				chats = [created, ...chats];
				chatId = created.chat_id;
				drafting = false;
			}
			// The unified timeline stream delivers the persisted message; do not
			// append locally or it would duplicate.
			if (result.percolated.length) {
				percolations = {
					...percolations,
					[result.message.message_id]: result.percolated
				};
			}
		} catch {
			// surfaced by toast in the full chat view; copilot stays quiet
		} finally {
			busy = false;
		}
	}
</script>

<aside
	class="flex h-full w-full min-w-0 flex-col bg-surface-900"
	data-testid="copilot-panel"
>
	<div class="flex items-center gap-2 border-b border-surface-700 px-4 py-3">
		<span class="text-accent" aria-hidden="true">✦</span>
		<h2 class="text-sm font-semibold text-slate-800">Copilot</h2>
		{#if selectedThread}
			<span class="ml-1 min-w-0 truncate text-[11px] text-slate-500">
				{selectedThread.title}
			</span>
		{/if}
		<div class="ml-auto flex items-center gap-1">
			<button
				type="button"
				class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent text-slate-700 transition hover:bg-surface-800 hover:text-accent"
				onclick={newConversation}
				title="New conversation"
				aria-label="New conversation"
				data-testid="copilot-new-conversation"
			>
				<span class="leading-none" aria-hidden="true">＋</span>
			</button>
			<button
				type="button"
				class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent transition {historyOpen
					? 'bg-surface-800 text-accent'
					: 'text-slate-700 hover:bg-surface-800 hover:text-accent'}"
				onclick={() => (historyOpen = !historyOpen)}
				title="Conversation history"
				aria-label="Conversation history"
				data-testid="copilot-history"
			>
				<span class="leading-none" aria-hidden="true">☰</span>
			</button>
			<button
				type="button"
				class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent text-slate-700 transition hover:bg-surface-800 hover:text-accent"
				onclick={onExpand}
				title="Expand to full chat"
				aria-label="Expand to full chat"
				data-testid="copilot-expand"
			>
				<span class="leading-none" aria-hidden="true">⤢</span>
			</button>
			<button
				type="button"
				class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent text-slate-700 transition hover:bg-surface-800 hover:text-accent"
				onclick={onClose}
				title="Close copilot"
				aria-label="Close copilot"
				data-testid="copilot-close"
			>
				<span class="leading-none" aria-hidden="true">✕</span>
			</button>
		</div>
	</div>

	{#if historyOpen}
		<div
			class="absolute top-14 right-0 bottom-0 z-30 flex w-full flex-col border-l border-surface-700 bg-surface-900 p-3 shadow-[var(--shadow-elevated)]"
			data-testid="copilot-history-overlay"
		>
			<div class="mb-2 flex items-center justify-between">
				<h3 class="text-xs font-semibold text-slate-700">Conversation history</h3>
				<button
					type="button"
					class="rounded p-1 text-slate-500 hover:text-slate-800"
					onclick={() => (historyOpen = false)}
					title="Close history"
					aria-label="Close history"
				>
					<span aria-hidden="true">✕</span>
				</button>
			</div>
			<div class="min-h-0 flex-1 space-y-2 overflow-y-auto">
				{#each visibleChats as thread (thread.chat_id)}
					<div class="group relative">
						<button
							type="button"
							class="w-full rounded-lg border border-surface-700 bg-surface-950 p-3 pr-9 text-left transition hover:border-accent/40 hover:bg-surface-800 {(drafting &&
							thread.chat_id === DRAFT_CHAT_ID) ||
							(!drafting && thread.chat_id === chatId)
								? 'border-accent/50'
								: ''}"
							onclick={() => {
								if (thread.chat_id === DRAFT_CHAT_ID) {
									newConversation();
								} else {
									chatId = thread.chat_id;
									drafting = false;
								}
								historyOpen = false;
								deleteTarget = null;
							}}
							data-testid="copilot-history-item"
						>
							<p class="truncate text-xs font-semibold text-slate-800">{thread.title}</p>
							{#if workspaceName}
								<p class="mt-1 text-[10px] text-slate-500">Scoped to {workspaceName}</p>
							{/if}
						</button>
						{#if thread.chat_id !== DRAFT_CHAT_ID}
						<button
							type="button"
							class="absolute top-2 right-2 hidden rounded p-1 text-slate-500 transition group-hover:block hover:text-loss"
							title="Delete conversation"
							aria-label="Delete conversation"
							onclick={() => (deleteTarget = thread)}
							data-testid="copilot-history-delete"
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
						{/if}
					</div>
				{:else}
					<p class="px-1 py-4 text-center text-xs text-slate-500">
						No conversations yet. Send a message to start one.
					</p>
				{/each}
			</div>
		</div>
	{/if}

	<ChatSurface
		{workspaceId}
		{chatId}
		{messages}
		{events}
		{execution}
		{percolations}
		proposal={null}
		{busy}
		onSend={send}
		hostId="copilot"
		rootLabel={selectedThread?.title ?? 'Conversation'}
	/>
</aside>

{#if deleteTarget}
	<ConversationDeleteDialog
		title={deleteTarget.title}
		onConfirm={confirmDelete}
		onClose={() => (deleteTarget = null)}
	/>
{/if}
