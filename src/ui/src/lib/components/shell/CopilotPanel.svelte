<script lang="ts">
	import { goto } from '$app/navigation';
	import ChatComposer from '$lib/components/chat/ChatComposer.svelte';
	import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
	import { listChats, listMessages, postMessage } from '$lib/api';
	import type { ChatMessage, ChatThread, SteeringPercolationTarget } from '$lib/types';

	/**
	 * Collapsible right-hand assistant on every page except the full-page
	 * chats view. Evidence scoping and knowledge search are agent-side
	 * capabilities (sandboxed subagents and MCP tools) and never surface
	 * here.
	 */
	let { workspaceId, onExpand }: { workspaceId: string; onExpand: () => void } = $props();

	let chats = $state<ChatThread[]>([]);
	let chatId = $state<string | null>(null);
	let messages = $state<ChatMessage[]>([]);
	let busy = $state(false);
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

	async function send(input: { body: string; attachmentIds: string[]; steeringIntent: string | null }): Promise<void> {
		if (!chatId) return;
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
	}
</script>

<aside
	class="flex w-80 shrink-0 flex-col border-l border-surface-700 bg-surface-900"
	data-testid="copilot-panel"
>
	<div class="flex items-center gap-2 border-b border-surface-700 px-4 py-3">
		<span class="text-accent" aria-hidden="true">✦</span>
		<h2 class="text-sm font-semibold text-slate-200">Copilot</h2>
		<button
			type="button"
			class="ml-auto rounded border border-surface-600 px-1.5 py-0.5 text-[10px] text-slate-400 hover:text-accent"
			onclick={onExpand}
			title="Expand to full chat"
		>
			⤢ full chat
		</button>
	</div>

	{#if activeThread}
		<div class="border-b border-surface-700 px-4 py-2">
			<select
				class="w-full rounded border border-surface-600/60 bg-surface-950 px-2 py-1 text-xs text-slate-300 outline-none"
				value={chatId}
				onchange={(e) => (chatId = (e.currentTarget as HTMLSelectElement).value)}
				aria-label="Select conversation"
			>
				{#each chats as thread (thread.chat_id)}
					<option value={thread.chat_id}>{thread.title}</option>
				{/each}
			</select>
		</div>
	{/if}

	<div class="min-h-0 flex-1 overflow-y-auto p-3">
		<ChatTranscript {messages} {percolations} />
	</div>

	<ChatComposer onSubmit={send} activeExecutionId={null} {busy} />

	<button
		type="button"
		class="border-t border-surface-700 px-4 py-2 text-center text-[11px] text-slate-600 hover:text-accent"
		onclick={() => goto(`/w/${workspaceId}/chats`)}
	>
		Open full-page chats →
	</button>
</aside>
