<script lang="ts">
	import { replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import WorkflowScope from '$lib/components/chat/WorkflowScope.svelte';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { getWorkflowExecution } from '$lib/api';
	import { derivePhases } from '$lib/subagent';
	import { WorkflowScopeController } from '$lib/workflow-scope.svelte';
	import type { WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';

	/**
	 * Standalone workflow route, deep-linkable to a nested scope:
	 * `/w/{workspace}/workflows/{execution_id}?scope=…` plus optional
	 * `#sub_agent={id}`. Renders the same `WorkflowScope` stack as the
	 * conversation pane.
	 */
	const workspaceId = page.params.workspace ?? '';
	const executionId = page.params.execution_id ?? '';
	const controller = new WorkflowScopeController(workspaceId, `workflow-page:${executionId}`);

	let execution = $state<WorkflowExecutionRecord | null>(null);
	let notFound = $state(false);
	let chatId = $state<string | null>(null);

	$effect(() => {
		let cancelled = false;
		getWorkflowExecution(workspaceId, executionId)
			.then((record) => {
				if (cancelled) return;
				execution = record;
				chatId = record?.chat_id ?? null;
				notFound = record === null;
			})
			.catch(() => {
				if (!cancelled) notFound = true;
			});
		return () => {
			cancelled = true;
		};
	});

	const phases = $derived.by((): WorkflowPhase[] => {
		const derived = derivePhases(controller.rootEvents);
		if (derived.length || !execution?.phase) return derived;
		return [{ name: execution.phase, status: 'active' }];
	});

	function syncScope(scope: string): void {
		const url = new URL(page.url);
		if (scope) url.searchParams.set('scope', scope);
		else url.searchParams.delete('scope');
		replaceState(url, {});
	}
</script>

<svelte:head><title>Workflow {executionId} — Thesis</title></svelte:head>

<div class="mx-auto flex min-h-full max-w-6xl flex-col gap-3">
	<header class="flex flex-wrap items-center gap-3">
		<a href="/w/{workspaceId}/conversations" class="text-xs text-slate-500 hover:text-accent">
			← Conversations
		</a>
		<h1 class="text-xl font-semibold text-slate-800">Workflow execution</h1>
		<span class="font-mono text-xs text-slate-500">{executionId}</span>
		{#if execution}
			<span class="rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500">
				{execution.workflow_id} · {execution.state}
			</span>
		{/if}
		{#if chatId}
			<a
				href="/w/{workspaceId}/conversations?chat={chatId}"
				class="ml-auto text-xs text-accent hover:underline"
				data-testid="workflow-origin-chat"
			>
				Open originating chat
			</a>
		{/if}
	</header>

	{#if notFound}
		<EmptyState
			title="Workflow unavailable"
			hint="The execution id is unknown to this workspace, or it was never persisted."
		/>
	{:else}
		<div class="min-h-0 flex-1">
			<WorkflowScope
				{controller}
				{workspaceId}
				{execution}
				{phases}
				scope={page.url.searchParams.get('scope')}
				hash={page.url.hash}
				rootLabel="Conversation"
				onScopeChange={syncScope}
			/>
		</div>
	{/if}
</div>
