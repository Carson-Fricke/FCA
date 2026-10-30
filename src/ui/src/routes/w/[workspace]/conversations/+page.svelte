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
	let viewportWidth = $state(1280);
	let listOpen = $state(true);
	let mobileListOpen = $state(false);
	const narrow = $derived(viewportWidth <= 760);
	const pickerShown = $derived(narrow ? mobileListOpen : listOpen);
	function togglePicker(): void {
		if (narrow) mobileListOpen = !mobileListOpen;
		else listOpen = !listOpen;
	}

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
		mobileListOpen = false;
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
		mobileListOpen = false;
	}
</script>

<svelte:head><title>Conversations — Thesis</title></svelte:head>
<svelte:window bind:innerWidth={viewportWidth} onkeydown={(event) => { if (event.key === 'Escape') mobileListOpen = false; }} />

<div class="conversation-workspace" data-testid="conversation-workspace">
	{#if narrow && pickerShown}<button type="button" class="picker-scrim" aria-label="Close conversation list" onclick={() => mobileListOpen = false}></button>{/if}
	<section class="conversation-picker" class:picker-hidden={!pickerShown} aria-label="Conversation selection" id="conversation-picker" data-testid="conversation-picker">
		<div class="picker-header">
			<h2>Conversations</h2>
			<span class="conversation-count">
				{chats.length}
			</span>
			<button
				type="button"
				class="picker-action new-conversation"
				onclick={newConversation}
				title="New conversation"
				aria-label="New conversation"
				data-testid="new-conversation"
			>
				<span aria-hidden="true">＋</span>
			</button>
			<button type="button" class="picker-action" onclick={togglePicker} aria-label="Hide conversation list" title="Hide conversation list"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16m7-11-3 3 3 3"/></svg></button>
		</div>
		<ul class="conversation-list" data-testid="conversation-list">
			{#each visibleChats as chat (chat.chat_id)}
				<li class="group relative">
					<button
						type="button"
						class="conversation-choice"
						class:selected={(drafting && chat.chat_id === DRAFT_CHAT_ID) || chat.chat_id === chatId}
						onclick={() =>
							chat.chat_id === DRAFT_CHAT_ID ? newConversation() : switchChat(chat.chat_id)}
						data-testid="conversation-item"
						data-chat={chat.chat_id}
						aria-current={chat.chat_id === chatId ? 'page' : undefined}
						title={chat.title}
					>
						<p class="conversation-name">
							{chat.title}
						</p>
						<p class="conversation-date">
							{chat.kind === 'investigation' ? 'investigation' : 'chat'} · {chat.updated_at?.slice(0, 10) ?? chat.created_at.slice(0, 10)}
						</p>
					</button>
					{#if chat.chat_id !== DRAFT_CHAT_ID}
					<button
						type="button"
						class="conversation-delete"
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
		<div class="picker-footer">
			Workspace {workspaceId}
		</div>
	</section>

	<section class="conversation-canvas" aria-label="Conversation">
			<header class="conversation-titlebar">
				<button type="button" class="picker-toggle" onclick={togglePicker} aria-label={pickerShown ? 'Hide conversation list' : 'Show conversation list'} title={pickerShown ? 'Hide conversation list' : 'Show conversation list'} aria-expanded={pickerShown} aria-controls="conversation-picker" data-testid="conversation-picker-toggle"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg></button>
				<h1>{selectedChat?.title ?? 'New conversation'}</h1>
				{#if selectedChat?.kind === 'investigation'}
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

		{#if host.error}
			<p class="px-3 py-2 text-xs text-warn" role="status" data-testid="chat-host-error">{host.error}</p>
		{/if}
		{#key `${workspaceId}:conversation`}
		{#if host.workspaceId === workspaceId}
		<ChatSurface
			showOutline={true}
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

<style>
	.conversation-workspace{position:relative;display:flex;height:100%;min-height:0;min-width:0;background:var(--color-surface-900)}
	.conversation-picker{display:flex;flex-direction:column;width:192px;flex-shrink:0;min-height:0;border-right:1px solid var(--color-surface-700);background:var(--color-surface-900)}.conversation-picker.picker-hidden{display:none}
	.picker-header{display:flex;align-items:center;gap:6px;min-height:64px;padding:12px 9px 10px 13px}.picker-header h2{font-size:12px;font-weight:500;color:var(--color-slate-800)}.conversation-count{font-size:10px;color:var(--color-slate-500)}.picker-action,.picker-toggle{display:flex;align-items:center;justify-content:center;width:26px;height:28px;flex-shrink:0;border-radius:4px;color:var(--color-slate-400);cursor:pointer}.picker-action:hover,.picker-toggle:hover{background:var(--color-surface-800);color:var(--color-accent)}.new-conversation{margin-left:auto}.picker-action svg,.picker-toggle svg{width:14px;height:14px}
	.conversation-list{overflow-y:auto;min-height:0;flex:1;padding:0 9px;margin:0;list-style:none}.conversation-list>li{margin-bottom:4px}.conversation-choice{display:block;width:100%;padding:9px 25px 9px 9px;border-radius:5px;text-align:left;color:var(--color-slate-700);cursor:pointer}.conversation-choice:hover{background:var(--color-surface-800)}.conversation-choice.selected{background:color-mix(in srgb,var(--color-accent) 9%,var(--color-surface-900));color:var(--color-accent)}.conversation-name{font-size:12px;line-height:1.5;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;overflow-wrap:anywhere}.conversation-date{margin-top:4px;font-size:10px;color:var(--color-slate-400)}
	.conversation-delete{position:absolute;top:9px;right:4px;display:flex;align-items:center;justify-content:center;width:24px;height:26px;border-radius:4px;color:var(--color-slate-400);opacity:0;cursor:pointer}.conversation-list>li:hover .conversation-delete,.conversation-delete:focus-visible{opacity:1}.conversation-delete:hover{color:var(--color-loss);background:var(--color-surface-800)}.picker-footer{padding:12px 13px;font-size:10px;color:var(--color-slate-400);overflow-wrap:anywhere}
	.conversation-canvas{display:flex;flex:1;flex-direction:column;min-width:0;min-height:0}.conversation-titlebar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:16px 18px 12px;min-height:64px}.conversation-titlebar h1{font-size:18px;line-height:1.45;font-weight:500;color:var(--color-slate-800);margin:0;flex:1;min-width:140px;overflow-wrap:anywhere}.picker-toggle{width:28px;height:30px}.picker-scrim{display:none}
	@media(max-width:760px){.conversation-picker{position:absolute;inset:0 auto 0 0;z-index:35;width:240px;max-width:calc(100% - 28px);box-shadow:5px 0 16px rgb(0 0 0 / .07)}.picker-scrim{display:block;position:absolute;inset:0;z-index:34;background:rgb(0 0 0 / .1)}.conversation-titlebar{padding:12px 10px;gap:7px}.conversation-titlebar h1{font-size:16px;min-width:100px}}
	@media(pointer:coarse){.picker-action,.picker-toggle{min-height:44px;min-width:36px}.conversation-delete{opacity:1;min-height:44px;top:5px}.conversation-choice{min-height:64px}}
</style>
