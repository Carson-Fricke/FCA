<script lang="ts">
	import ScopeStack from './ScopeStack.svelte';
	import WorkflowCard from './WorkflowCard.svelte';
	import SubAgentElement from './SubAgentElement.svelte';
	import { ORCHESTRATOR_ID, type WorkflowScopeController } from '$lib/workflow-scope.svelte';
	import type { WorkflowExecutionRecord, WorkflowPhase } from '$lib/types';

	/**
	 * Hosts the in-pane scope stack: breadcrumb + back plus the active node's
	 * content (workflow card, orchestrator, or a sub-agent). Used by the
	 * conversation pane and the standalone workflow route so both share the
	 * same enter/back behavior, lazy loading, and composer gating.
	 */
	let {
		controller,
		workspaceId,
		execution = null,
		phases = [],
		scope = null,
		hash = null,
		rootLabel = 'Conversation',
		fullPageHref = null,
		autoAttach = true,
		onOpenFullPage,
		onScopeChange
	}: {
		controller: WorkflowScopeController;
		workspaceId: string;
		execution?: WorkflowExecutionRecord | null;
		phases?: WorkflowPhase[];
		scope?: string | null;
		hash?: string | null;
		rootLabel?: string;
		fullPageHref?: string | null;
		/** When false the host already attached the controller. */
		autoAttach?: boolean;
		onOpenFullPage?: () => void;
		onScopeChange?: (scope: string) => void;
	} = $props();

	let attachSignature = $state('');

	$effect(() => {
		if (!autoAttach) return;
		const current = execution;
		const signature = current ? current.execution_id : '';
		if (!current || signature === attachSignature) return;
		attachSignature = signature;
		controller.attach(current.execution_id, current, {
			chatId: current.chat_id,
			rootLabel,
			scope,
			hash
		});
	});

	$effect(() => {
		if (!autoAttach) return;
		return () => controller.detach();
	});

	$effect(() => {
		const serialized = controller.scope.toQuery();
		onScopeChange?.(serialized);
	});

	const active = $derived(controller.activeSegment);
	const fullHref = $derived(
		fullPageHref ??
			(execution ? `/w/${workspaceId}/workflows/${execution.execution_id}` : null)
	);

	/** The workflow card is the entry surface for the conversation root. */
	function enterPhase(phase: string, options: { readOnly: boolean }): void {
		controller.enterWorkflow();
		controller.enterOrchestrator(phase, options.readOnly);
	}
</script>

<ScopeStack
	segments={controller.scope.segments}
	title="Workflow"
	onBack={() => controller.back()}
	onNavigate={(index) => controller.navigateTo(index)}
>
	{#if execution}
		{#if active?.kind === 'sub_agent' && active.id === ORCHESTRATOR_ID}
			<SubAgentElement
				node={controller.orchestratorNode()}
				events={controller.rootEvents}
				children={controller.tree}
				{workspaceId}
				expanded={true}
				isExpanded={(id) => controller.isExpanded(id)}
				onToggle={(id) => controller.toggleExpand(id)}
				onEnter={(node) => controller.enterSubAgent(node)}
				onSubmit={(node, body) => controller.submit(node, body)}
				onResolveGate={(gate, resolution) => controller.resolveGate(gate, resolution)}
				activeId={active.id}
				readOnly={active.readOnly === true}
				busy={controller.busy}
			/>
		{:else if active?.kind === 'sub_agent'}
			{@const node = controller.subAgentById(active.id)}
			{#if node}
				<SubAgentElement
					{node}
					events={controller.eventsForSubAgent(active.id)}
					children={controller.childrenOf(active.id)}
					{workspaceId}
					expanded={true}
					isExpanded={(id) => controller.isExpanded(id)}
					onToggle={(id) => controller.toggleExpand(id)}
					onEnter={(child) => controller.enterSubAgent(child)}
					onSubmit={(child, body) => controller.submit(child, body)}
					onResolveGate={(gate, resolution) => controller.resolveGate(gate, resolution)}
					activeId={active.id}
					readOnly={active.readOnly === true}
					busy={controller.busy}
				/>
			{:else}
				<p class="text-xs text-slate-500">Sub-agent {active.id} is not loaded.</p>
			{/if}
		{:else}
			<WorkflowCard
				{execution}
				{phases}
				status={execution.state}
				summary={execution.phase ? `Current phase ${execution.phase}` : 'Awaiting phase transitions'}
				fullPageHref={fullHref}
				onEnterPhase={enterPhase}
				{onOpenFullPage}
			/>
		{/if}
	{:else}
		<p class="text-xs text-slate-500">No workflow execution is attached.</p>
	{/if}
</ScopeStack>
