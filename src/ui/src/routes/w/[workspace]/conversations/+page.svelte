<script lang="ts">
	import { untrack } from 'svelte';
	import { replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import { ChatHostController, DRAFT_CHAT_ID } from '$lib/chat-host.svelte';
	import { conversationNavigation } from '$lib/chat-navigation';
	import ChatSurface from '$lib/components/chat/ChatSurface.svelte';
	import ConversationDeleteDialog from '$lib/components/chat/ConversationDeleteDialog.svelte';
	import {
		confirmConsent,
		getConsentProposal
	} from '$lib/api';
	import { notify } from '$lib/workspace.svelte';
	import type {
		ChatThread,
		ConsentProposal
	} from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	const host = new ChatHostController(conversationNavigation(
		() => typeof location === 'undefined' ? page.url : new URL(location.href),
		(url) => replaceState(url, {})
	));
	const chats = $derived(host.chats);
	const chatId = $derived(host.chatId);
	const percolations = $derived(host.percolations);
	let proposal = $state<ConsentProposal | null>(null);
	let consentBusy = $state(false);
	const busy = $derived(host.busy);
	/** A new conversation is a draft until its first message is sent. */
	const drafting = $derived(host.drafting);
	/** The conversation queued for modal delete confirmation. */
	let deleteTarget = $state<ChatThread | null>(null);
	/**
	 * The scope/hash deep link is a mount-time URL carrier: captured once so
	 * a scoped load (F5/deep link) restores its view, and dropped on any
	 * conversation switch so the surface attaches at the root instead of
	 * replaying a view that belongs to the previous conversation.
	 */
	const scopeGrant = $derived(host.scopeGrant);
	const hashGrant = $derived(host.hashGrant);
	const timeline = host.timeline;
	const visibleChats = $derived(host.visibleChats);
	const selectedChat = $derived(host.selectedChat);
	const anyActiveWorkflow = $derived(timeline.anyActiveWorkflow);

	$effect(() => {
		const id = workspaceId;
		untrack(() => { deleteTarget = null; void host.open(id); });
		return () => host.stop();
	});

	$effect(() => {
		let cancelled = false;
		proposal = null;
		getConsentProposal(workspaceId).then((p) => {
			if (!cancelled) proposal = p;
		}).catch((error) => { if (!cancelled) notify(error instanceof Error ? error.message : 'Consent proposal failed', true); });
		return () => {
			cancelled = true;
		};
	});

	/** Start a draft conversation; it persists only when the first message is sent. */
	function newConversation(): void {
		host.newConversation();
		deleteTarget = null;
	}

	async function confirmDelete(): Promise<void> {
		const target = deleteTarget;
		if (!target) return;
		deleteTarget = null;
		await host.remove(target.chat_id);
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

	function switchChat(id: string): void {
		host.select(id);
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
					{host.loadingChats ? 'Loading conversations...' : 'Your next good idea starts with a conversation.'}
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
				{#if anyActiveWorkflow}
					<span class="ml-auto flex items-center gap-1.5 font-mono text-[10px] text-warn">
						<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-warn" aria-hidden="true"></span>
						workflow execution active
					</span>
				{/if}
			</header>
		{/if}

		{#if host.error}
			<p class="px-3 py-2 text-xs text-warn" role="status" data-testid="chat-host-error">{host.error}</p>
		{/if}
		{#key `${workspaceId}:conversation`}
		{#if host.workspaceId === workspaceId}
		<ChatSurface
			{timeline}
			{workspaceId}
			{chatId}
			{drafting}
			terminal={['completed', 'failed', 'cancelled', 'closed', 'archived'].includes(selectedChat?.state ?? '')}
			{percolations}
			{proposal}
			onConfirmConsent={confirmProposal}
			onRejectConsent={() => {
				proposal = null;
				notify('Consent proposal rejected');
			}}
			{consentBusy}
			{busy}
			onSend={(input) => host.send(input)}
			onScopeChange={(scope) => host.syncScope(scope)}
			hostId="conversation"
			rootLabel={selectedChat?.title ?? 'Conversation'}
			initialScope={scopeGrant}
			initialHash={hashGrant}
		/>
		{/if}
		{/key}
	</section>
</div>

{#if deleteTarget}
	<ConversationDeleteDialog
		title={deleteTarget.title}
		onConfirm={confirmDelete}
		onClose={() => (deleteTarget = null)}
	/>
{/if}
