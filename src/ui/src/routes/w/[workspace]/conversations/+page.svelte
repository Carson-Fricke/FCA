<script lang="ts">
	import { replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import ChatSurface from '$lib/components/chat/ChatSurface.svelte';
	import CleoPlanPanel from '$lib/components/chat/CleoPlanPanel.svelte';
	import ConversationDeleteDialog from '$lib/components/chat/ConversationDeleteDialog.svelte';
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
		postMessage
	} from '$lib/api';
	import { TimelineController } from '$lib/timeline.svelte';
	import { notify } from '$lib/workspace.svelte';
	import type {
		ChatThread,
		ConsentProposal,
		SteeringPercolationTarget,
		WorkflowExecutionRecord,
		WorkspaceStateData
	} from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let chats = $state<ChatThread[]>([]);
	let chatId = $state<string | null>(null);
	let execution = $state<WorkflowExecutionRecord | null>(null);
	let percolations = $state<Record<string, SteeringPercolationTarget[]>>({});
	let proposal = $state<ConsentProposal | null>(null);
	let consentBusy = $state(false);
	let stateData = $state<WorkspaceStateData | null>(null);
	let busy = $state(false);
	/** A new conversation is a draft until its first message is sent. */
	let drafting = $state(false);
	/** The conversation queued for modal delete confirmation. */
	let deleteTarget = $state<ChatThread | null>(null);
	const timeline = new TimelineController();
	const messages = $derived(timeline.messages);
	const events = $derived(timeline.events);

	const activeChat = $derived(chats.find((c) => c.chat_id === chatId) ?? null);
	const DRAFT_CHAT_ID = '__draft_conversation__';
	function draftChat(): ChatThread {
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
	const visibleChats = $derived(drafting ? [draftChat(), ...chats] : chats);
	const selectedChat = $derived(drafting ? draftChat() : activeChat);
	const activeExecutionId = $derived(execution?.execution_id ?? null);
	const cleoPlan = $derived(stateData?.cleo ?? null);

	$effect(() => {
		let cancelled = false;
		listChats(workspaceId).then((items) => {
			if (cancelled) return;
			chats = items;
			if (!drafting && !chatId) {
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

	// Load the selected chat's unified timeline and its workflow execution.
	$effect(() => {
		const id = chatId;
		if (!id) {
			execution = null;
			timeline.stop();
			return;
		}
		let cancelled = false;
		const requested = page.url.searchParams.get('chat');
		if (requested !== id) {
			replaceState(`/w/${workspaceId}/conversations?chat=${id}`, {});
		}
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
		deleteTarget = null;
		chatId = null;
		execution = null;
		replaceState(`/w/${workspaceId}/conversations`, {});
	}

	async function removeConversation(id: string): Promise<void> {
		try {
			await deleteChat(workspaceId, id);
			chats = chats.filter((chat) => chat.chat_id !== id);
			if (chatId === id) {
				timeline.stop();
				chatId = chats[0]?.chat_id ?? null;
				execution = null;
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

	/** Reflect the in-pane scope path in the conversation URL. */
	function syncScope(scope: string): void {
		const url = new URL(page.url);
		const current = url.searchParams.get('scope') ?? '';
		if (current === scope) return;
		if (scope) url.searchParams.set('scope', scope);
		else url.searchParams.delete('scope');
		replaceState(url, {});
	}

	function switchChat(id: string): void {
		chatId = id;
		drafting = false;
		deleteTarget = null;
	}
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
			{#each visibleChats as chat (chat.chat_id)}
				<li class="group relative">
					<button
						type="button"
						class="w-full rounded-lg px-3 py-2 pr-9 text-left transition
							{((drafting && chat.chat_id === DRAFT_CHAT_ID) ||
							chat.chat_id === chatId)
							? 'border border-accent/40 bg-accent/10'
							: 'border border-transparent hover:bg-surface-800/60'}"
						onclick={() =>
							chat.chat_id === DRAFT_CHAT_ID ? newConversation() : switchChat(chat.chat_id)}
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
					{#if chat.chat_id !== DRAFT_CHAT_ID}
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
					{/if}
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
		{#if selectedChat}
			<header class="flex items-center gap-2 border-b border-surface-700 px-4 py-3">
				<h1 class="truncate text-sm font-semibold text-slate-800">{selectedChat.title}</h1>
				{#if selectedChat.kind === 'investigation'}
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
		{/if}

		<ChatSurface
			{workspaceId}
			{chatId}
			{messages}
			{events}
			{execution}
			{percolations}
			{proposal}
			onConfirmConsent={confirmProposal}
			onRejectConsent={() => {
				proposal = null;
				notify('Consent proposal rejected');
			}}
			{consentBusy}
			{busy}
			onSend={send}
			onScopeChange={syncScope}
			hostId="conversation"
			rootLabel={selectedChat?.title ?? 'Conversation'}
			initialScope={page.url.searchParams.get('scope')}
			initialHash={page.url.hash}
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
