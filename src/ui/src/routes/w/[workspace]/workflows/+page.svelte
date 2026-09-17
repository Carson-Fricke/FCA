<script lang="ts">
	import { page } from '$app/state';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { getExecution, listChats } from '$lib/api';
	import type { ChatThread, WorkflowExecutionRecord } from '$lib/types';

	/** Workspace workflow index: every chat-backed execution, deep-linkable. */
	const workspaceId = page.params.workspace ?? '';
	let rows = $state<{ chat: ChatThread; execution: WorkflowExecutionRecord }[]>([]);
	let loading = $state(true);

	$effect(() => {
		let cancelled = false;
		loading = true;
		listChats(workspaceId)
			.then((chats) => Promise.all(chats.map((chat) => getExecution(workspaceId, chat.chat_id).then((execution) => ({ chat, execution })))))
			.then((pairs) => {
				if (cancelled) return;
				rows = pairs.filter(
					(pair): pair is { chat: ChatThread; execution: WorkflowExecutionRecord } =>
						pair.execution !== null
				);
				loading = false;
			})
			.catch(() => {
				if (!cancelled) loading = false;
			});
		return () => {
			cancelled = true;
		};
	});
</script>

<svelte:head><title>Workflows — Thesis</title></svelte:head>

<div class="mx-auto max-w-4xl space-y-4">
	<header class="flex items-baseline gap-3">
		<h1 class="text-xl font-semibold text-slate-800">Workflows</h1>
		<span class="font-mono text-xs text-slate-500">{rows.length} execution(s)</span>
	</header>

	{#if loading}
		<div class="h-40 animate-pulse rounded-xl border border-surface-700 bg-surface-900"></div>
	{:else if rows.length === 0}
		<EmptyState
			title="No workflow executions"
			hint="Executions appear here once a conversation launches one."
		/>
	{:else}
		<ul class="space-y-2" data-testid="workflow-index">
			{#each rows as row (row.execution.execution_id)}
				<li>
					<a
						href="/w/{workspaceId}/conversations?chat={row.chat.chat_id}"
						class="flex items-center gap-3 rounded-xl border border-surface-700 bg-surface-900 p-3 transition hover:border-accent/40"
						data-testid="workflow-index-row"
					>
						<div class="min-w-0 flex-1">
							<p class="truncate text-sm font-semibold text-slate-800">{row.chat.title}</p>
							<p class="font-mono text-[10px] text-slate-500">
								{row.execution.workflow_id} · {row.execution.execution_id} · phase {row.execution.phase ?? '—'}
							</p>
						</div>
						<span class="rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500">
							{row.execution.state}
						</span>
					</a>
				</li>
			{/each}
		</ul>
	{/if}
</div>
