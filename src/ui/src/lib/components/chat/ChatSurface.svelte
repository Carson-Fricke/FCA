<script lang="ts">
	import ChatComposer from './ChatComposer.svelte';
	import ChatStarters from './ChatStarters.svelte';
	import ChatTranscript from './ChatTranscript.svelte';
	import ScopeStack from './ScopeStack.svelte';
	import ScopeTranscript from './ScopeTranscript.svelte';
	import StreamingReply from './StreamingReply.svelte';
	import SubAgentComposer from './SubAgentComposer.svelte';
	import WorkflowCard from './WorkflowCard.svelte';
	import { WorkflowScopeController } from '$lib/workflow-scope.svelte';
	import { buildNodeEntries } from '$lib/transcript';
	import {
		derivePhases,
		executionEvents,
		projectExecution
	} from '$lib/subagent';
	import type {
		ChatMessage,
		ConsentConfirmation,
		ConsentProposal,
		ExecutionEvent,
		SteeringPercolationTarget,
		SubAgentNode,
		WorkflowExecutionRecord
	} from '$lib/types';

	/**
	 * The one chat surface used by the copilot pane and the full conversations
	 * page.
	 *
	 * It renders the root transcript, and when the operator drills into a phase
	 * or sub-agent it renders that node's unified chronological conversation in
	 * place, with a breadcrumb/back stack and a composer that targets whichever
	 * agent is active. Escaping back to the conversation root always restores
	 * the ordinary root transcript and root composer, so chat is never captured
	 * by a workflow view.
	 */
	let {
		workspaceId,
		chatId = null,
		messages,
		events = null,
		execution = null,
		percolations = {},
		proposal = null,
		onConfirmConsent,
		onRejectConsent,
		consentBusy = false,
		streaming = { active: false, text: '', reasoning: '' },
		busy = false,
		onSend,
		onNodeMessage,
		onScopeChange,
		hostId = 'conversation',
		rootLabel = 'Conversation',
		initialScope = null,
		initialHash = null
	}: {
		workspaceId: string;
		chatId?: string | null;
		messages: ChatMessage[];
		/** Execution events from the shared timeline stream (external feed). */
		events?: ExecutionEvent[] | null;
		execution?: WorkflowExecutionRecord | null;
		percolations?: Record<string, SteeringPercolationTarget[]>;
		proposal?: ConsentProposal | null;
		onConfirmConsent?: (confirmation: ConsentConfirmation) => void;
		onRejectConsent?: () => void;
		consentBusy?: boolean;
		streaming?: { active: boolean; text: string; reasoning: string };
		busy?: boolean;
		onSend: (input: {
			body: string;
			attachmentIds: string[];
			steeringIntent: string | null;
		}) => void;
		onNodeMessage?: (message: ChatMessage) => void;
		onScopeChange?: (scope: string) => void;
		hostId?: string;
		rootLabel?: string;
		initialScope?: string | null;
		initialHash?: string | null;
	} = $props();

	// The host id and workspace scope the controller's persisted navigation; they
	// are fixed for the lifetime of one mounted surface.
	const controller = new WorkflowScopeController(
		// svelte-ignore state_referenced_locally
		workspaceId,
		// svelte-ignore state_referenced_locally
		hostId
	);
	let attachedExecutionId = $state('');

	/**
	 * The execution the surface navigates. A fresh workflow launched in this
	 * chat arrives first as a streamed `kind=workflow` anchor, so the surface
	 * falls back to the anchor's execution id instead of waiting for a reload
	 * before the phase drill-in works.
	 */
	function executionFromAnchor(message: ChatMessage): WorkflowExecutionRecord | null {
		const data = message.data ?? {};
		const executionId = String(data.executionId ?? message.execution_id ?? '');
		if (!executionId) return null;
		return {
			execution_id: executionId,
			chat_id: message.chat_id,
			trigger_message_id: message.message_id,
			workflow_id: String(data.workflowId ?? 'workflow'),
			workflow_version: String(data.version ?? ''),
			requested_by: message.actor_id,
			state: String(data.status ?? 'running'),
			phase: data.currentPhase == null ? null : String(data.currentPhase),
			intent_epoch: message.intent_epoch,
			steering_message_ids: [],
			created_at: message.created_at
		};
	}

	const effectiveExecution = $derived.by(() => {
		// Conversation switches can leave the previous fetch result in the host
		// briefly. Never bind this surface to another chat's execution.
		let latest = execution && (!chatId || execution.chat_id === chatId) ? execution : null;
		for (let index = messages.length - 1; index >= 0; index -= 1) {
			const candidate = executionFromAnchor(messages[index]);
			if (!candidate) continue;
			if (!latest || candidate.created_at > latest.created_at) latest = candidate;
			break;
		}
		if (!latest) return null;
		return projectExecution(latest, executionEvents(events ?? [], latest.execution_id));
	});

	const effectiveEvents = $derived(
		effectiveExecution
			? executionEvents(events ?? [], effectiveExecution.execution_id)
			: []
	);

	// Attach to the active chat's execution; a different execution (or none)
	// resets the scope to the conversation root.
	$effect(() => {
		const current = effectiveExecution;
		if (!current) {
			controller.detach();
			attachedExecutionId = '';
			return;
		}
		if (current.execution_id === attachedExecutionId) return;
		attachedExecutionId = current.execution_id;
		controller.attach(current.execution_id, current, {
			chatId: chatId ?? current.chat_id,
			rootLabel,
			scope: initialScope,
			hash: initialHash,
			externalEvents: events !== null
		});
	});

	// Feed the scope controller from the single shared timeline stream.
	$effect(() => {
		if (effectiveEvents.length) controller.ingestEvents(effectiveEvents);
	});

	$effect(() => {
		return () => controller.detach();
	});

	// Reflect scope changes back to the host URL. The first run seeds the
	// current path (already derived from the URL) without emitting, so mounting
	// never calls the host's `replaceState` before SvelteKit has initialized its
	// router.
	let scopeEmitted = false;
	$effect(() => {
		const serialized = controller.scope.toQuery();
		if (!scopeEmitted) {
			scopeEmitted = true;
			return;
		}
		onScopeChange?.(serialized);
	});

	const active = $derived(controller.activeSegment);
	const scoped = $derived(
		controller.scope.segments.some((segment) => segment.kind === 'sub_agent')
	);
	const isNodeScope = $derived(active?.kind === 'sub_agent');

	const phases = $derived.by(() => {
		// The launch contract declares the complete phase sequence. Lifecycle
		// events mark earlier phases done without hiding work that has not started.
		const derived = derivePhases(controller.rootEvents);
		if (derived.length || !effectiveExecution?.phase) return derived;
		return [{ name: effectiveExecution.phase, status: 'active' as const }];
	});

	const hasWorkflowAnchor = $derived(messages.some((message) => message.kind === 'workflow'));

	/** Phases whose agent is blocked on operator input, surfaced on the card. */
	const attentionPhases = $derived.by(() => {
		const phases = new Set<string>();
		const collect = (nodes: typeof controller.tree): void => {
			for (const node of nodes) {
				if (node.node.status === 'needs_input' && node.node.phase) {
					phases.add(node.node.phase);
				}
				collect(node.children);
			}
		};
		collect(controller.tree);
		return [...phases];
	});

	const composerNode = $derived(isNodeScope ? controller.subAgentById(active!.id) : null);
	const nodePending = $derived(
		composerNode?.status === 'spawned' ||
			composerNode?.status === 'running' ||
			composerNode?.status === 'progress'
	);
	const showNodeComposer = $derived(
		isNodeScope && composerNode?.acceptsInput === true && active?.readOnly !== true
	);

	const scopeEntries = $derived.by(() => {
		const segment = active;
		if (!segment || segment.kind !== 'sub_agent') return [];
		const events = controller.eventsForSubAgent(segment.id);
		return buildNodeEntries({
			messages: messages.filter(
				(message) =>
					message.kind === 'sub_agent' &&
					(message.role === 'user' || message.role === 'system') &&
					String(message.data?.subAgentId ?? '') === segment.id
			),
			events,
			children: controller.childrenOf(segment.id)
		});
	});

	function enterPhase(phase: string, options: { readOnly: boolean }): void {
		void controller.enterPhase(phase, options.readOnly);
	}

	function enterSubAgent(node: SubAgentNode): void {
		controller.enterSubAgent(node);
	}

	async function sendNode(_node: SubAgentNode, body: string): Promise<void> {
		if (!composerNode) return;
		const message = await controller.submit(composerNode, body);
		if (message) onNodeMessage?.(message);
	}
</script>

<div class="flex min-h-0 flex-1 flex-col" data-testid="chat-surface">
	{#if scoped}
		<ScopeStack
			segments={controller.scope.segments}
			title="Sub-agent"
			onBack={() => controller.back()}
			onNavigate={(index) => controller.navigateTo(index)}
		>
			<ScopeTranscript
				entries={scopeEntries}
				{workspaceId}
				busy={controller.busy}
				readOnly={active?.readOnly === true}
				pending={nodePending}
				onEnterSubAgent={enterSubAgent}
			/>
		</ScopeStack>
	{:else}
		<div class="min-h-0 flex-1 overflow-y-auto p-3" data-testid="transcript-scroll">
			{#if !messages.length && !streaming.active}
				<div class="mb-3">
					<p class="mb-2 text-xs text-slate-500">Start with a suggestion, or ask anything.</p>
					<ChatStarters
						onSelect={(prompt) =>
							onSend({ body: prompt, attachmentIds: [], steeringIntent: null })}
						disabled={busy}
					/>
				</div>
			{/if}
			<ChatTranscript
				{messages}
				events={events ?? []}
				{workspaceId}
				{percolations}
				{proposal}
				{onConfirmConsent}
				{onRejectConsent}
				{consentBusy}
				execution={effectiveExecution}
				{phases}
				{attentionPhases}
				onEnterPhase={enterPhase}
			/>
			<StreamingReply
				active={streaming.active}
				text={streaming.text}
				reasoning={streaming.reasoning}
				testId="streaming-reply"
			/>
			{#if effectiveExecution && !hasWorkflowAnchor}
				<div class="mt-3">
					<WorkflowCard
						execution={effectiveExecution}
						{phases}
						status={effectiveExecution.state}
						summary={effectiveExecution.phase
							? `Current phase ${effectiveExecution.phase}`
							: 'Awaiting phase transitions'}
						{attentionPhases}
						onEnterPhase={enterPhase}
					/>
				</div>
			{/if}
		</div>
	{/if}

	{#if showNodeComposer && composerNode}
		<SubAgentComposer
			acceptsInput={true}
			phase={composerNode.phase}
			busy={controller.busy || nodePending}
			onSubmit={(body) => sendNode(composerNode, body)}
		/>
	{:else if isNodeScope}
		<div
			class="border-t border-surface-700 bg-surface-900 px-3 py-2 text-center text-[11px] text-slate-500"
			data-testid="scope-readonly"
		>
			This sub-agent does not accept input.
		</div>
		{:else}
			<ChatComposer
				onSubmit={onSend}
				activeExecutionId={effectiveExecution?.execution_id ?? null}
				{busy}
			/>
		{/if}
</div>
