<script lang="ts">
	import { goto } from '$app/navigation';
	import ChatComposer from '$lib/components/chat/ChatComposer.svelte';
	import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
	import { listChats, listMessages, postMessage } from '$lib/api';
	import type { ChatMessage, ChatThread, SteeringPercolationTarget } from '$lib/types';

	/**
	 * Collapsible right-hand assistant on every page except the full-page
	 * conversations view. The header carries three icon-only controls with
	 * tooltips: new conversation, conversation history overlay, and expand
	 * to the full-page view. Evidence scoping and knowledge search are
	 * agent-side capabilities and never surface here.
	 */
	let {
		workspaceId,
		workspaceName = '',
		onExpand
	}: { workspaceId: string; workspaceName?: string; onExpand: () => void } = $props();

	let chats = $state<ChatThread[]>([]);
	let chatId = $state<string | null>(null);
	let messages = $state<ChatMessage[]>([]);
	let busy = $state(false);
	let historyOpen = $state(false);
	let percolations = $state<Record<string, SteeringPercolationTarget[]>>({});

	const activeThread = $derived(chats.find((c) => c.chat_id === chatId) ?? null);

	$effect(() => {
		let cancelled = false;
		listChats(workspaceId).then((items) => {
			if (cancelled) return;
			chats = items;
			chatId = items[0]?.chat_id ?? null;
		});
		return () => {
			cancelled = true;
		};
	});

	$effect(() => {
		const id = chatId;
		if (!id) {
			messages = [];
			return;
		}
		let cancelled = false;
		listMessages(workspaceId, id).then((items) => {
			if (!cancelled) messages = items;
		});
		return () => {
			cancelled = true;
		};
	});

	function newConversation(): void {
		chatId = null;
		messages = [];
		historyOpen = false;
	}

	async function send(input: {
		body: string;
		attachmentIds: string[];
		steeringIntent: string | null;
	}): Promise<void> {
		if (chatId) {
			busy = true;
			try {
				const result = await postMessage(workspaceId, chatId, input);
				messages = [...messages, result.message];
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
			return;
		}
		// No conversation selected: route to the full conversations view where
		// a new thread can be started (the copilot attaches to an existing
		// conversation by design).
		await goto(`/w/${workspaceId}/conversations`);
	}
</script>

<aside
	class="flex w-80 shrink-0 flex-col border-l border-surface-700 bg-surface-900"
	data-testid="copilot-panel"
>
	<div class="flex items-center gap-2 border-b border-surface-700 px-4 py-3">
		<span class="text-accent" aria-hidden="true">✦</span>
		<h2 class="text-sm font-semibold text-slate-800">Copilot</h2>
		{#if activeThread}
			<span class="ml-1 min-w-0 truncate text-[11px] text-slate-500">
				{activeThread.title}
			</span>
		{/if}
		<div class="ml-auto flex items-center gap-1">
			<button
				type="button"
				class="rounded-md border border-transparent p-1.5 text-slate-600 transition hover:border-surface-600 hover:text-accent"
				onclick={newConversation}
				title="New conversation"
				aria-label="New conversation"
				data-testid="copilot-new-conversation"
			>
				<span aria-hidden="true">＋</span>
			</button>
			<button
				type="button"
				class="rounded-md border border-transparent p-1.5 text-slate-600 transition hover:border-surface-600 hover:text-accent {historyOpen
					? 'border-surface-600 text-accent'
					: ''}"
				onclick={() => (historyOpen = !historyOpen)}
				title="Conversation history"
				aria-label="Conversation history"
				data-testid="copilot-history"
			>
				<span aria-hidden="true">☰</span>
			</button>
			<button
				type="button"
				class="rounded-md border border-transparent p-1.5 text-slate-600 transition hover:border-surface-600 hover:text-accent"
				onclick={onExpand}
				title="Expand to full chat"
				aria-label="Expand to full chat"
				data-testid="copilot-expand"
			>
				<span aria-hidden="true">⤢</span>
			</button>
		</div>
	</div>

	{#if historyOpen}
		<div
			class="absolute top-14 right-0 bottom-0 z-30 flex w-80 flex-col border-l border-surface-700 bg-surface-900 p-3 shadow-[var(--shadow-elevated)]"
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
				{#each chats as thread (thread.chat_id)}
					<button
						type="button"
						class="w-full rounded-lg border border-surface-700 bg-surface-950 p-3 text-left transition hover:border-accent/40 hover:bg-surface-800 {thread.chat_id ===
						chatId
							? 'border-accent/50'
							: ''}"
						onclick={() => {
							chatId = thread.chat_id;
							historyOpen = false;
						}}
						data-testid="copilot-history-item"
					>
						<p class="truncate text-xs font-semibold text-slate-800">{thread.title}</p>
						{#if workspaceName}
							<p class="mt-1 text-[10px] text-slate-500">Scoped to {workspaceName}</p>
						{/if}
					</button>
				{:else}
					<p class="px-1 py-4 text-center text-xs text-slate-500">
						No conversations yet. Send a message to start one.
					</p>
				{/each}
			</div>
		</div>
	{/if}

	<div class="min-h-0 flex-1 overflow-y-auto p-3">
		<ChatTranscript
			{messages}
			{percolations}
			proposal={null}
			onConfirmConsent={undefined}
			onRejectConsent={undefined}
		/>
	</div>

	<ChatComposer onSubmit={send} activeExecutionId={null} {busy} />
</aside>
