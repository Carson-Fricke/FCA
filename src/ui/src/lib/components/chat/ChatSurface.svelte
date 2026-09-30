<script lang="ts">
	import ChatComposer from './ChatComposer.svelte';
	import ChatStarters from './ChatStarters.svelte';
	import ChatTranscript from './ChatTranscript.svelte';
	import PhaseRunView from './PhaseRunView.svelte';
	import ScopeStack from './ScopeStack.svelte';
	import ScopeTranscript from './ScopeTranscript.svelte';
	import StreamingReply from './StreamingReply.svelte';
	import SubAgentComposer from './SubAgentComposer.svelte';
	import WorkflowCard from './WorkflowCard.svelte';
	import { setSubAgentLiveView } from './sub-agent-live-view';
	import { WorkflowScopeController } from '$lib/workflow-scope.svelte';
	import { buildNodeEntries } from '$lib/transcript';
	import {
		derivePhaseAttempts,
		derivePhaseSteps,
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
	let attachedChatId = $state('');

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
	// resets the scope to the conversation root. Without an execution the
	// controller still attaches to the chat itself, so sub-agent cards can
	// drill into chat-spawned agents that have no activity log at all.
	$effect(() => {
		const current = effectiveExecution;
		if (!current) {
			if (chatId && chatId !== attachedChatId) {
				attachedChatId = chatId;
				controller.attachChat(chatId, {
					rootLabel,
					scope: initialScope,
					hash: initialHash
				});
			} else if (!chatId && attachedChatId) {
				attachedChatId = '';
				controller.detach();
			}
			attachedExecutionId = '';
			return;
		}
		if (current.execution_id === attachedExecutionId) return;
		attachedExecutionId = current.execution_id;
		attachedChatId = '';
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

	// Chat-spawned agents have no activity log, so their durable status only
	// advances on the record itself: refresh the tree whenever the chat
	// timeline moves while no execution is attached. The first run after an
	// attach seeds without refreshing; the attach itself just loaded the
	// tree.
	let nodeRefreshSeeded = false;
	$effect(() => {
		const timelineMoved = messages[messages.length - 1]?.message_id ?? '';
		if (effectiveExecution || !attachedChatId) {
			nodeRefreshSeeded = false;
			return;
		}
		if (!nodeRefreshSeeded || !timelineMoved) {
			nodeRefreshSeeded = true;
			return;
		}
		controller.refreshNodes();
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
		controller.scope.segments.some(
			(segment) => segment.kind === 'sub_agent' || segment.kind === 'phase'
		)
	);
	const isNodeScope = $derived(active?.kind === 'sub_agent');
	const isPhaseScope = $derived(active?.kind === 'phase');

	/**
	 * The active phase's run view state (REQ-D2): every attempt's
	 * `workflow.progress` lines, then its `workflow.phase_result` — the error
	 * variant when the artifact is one. A loopback that re-enters the phase
	 * keeps every earlier attempt, so the view selects among them.
	 */
	const phaseAttempts = $derived.by(() => {
		if (!active || active.kind !== 'phase') return [];
		return derivePhaseAttempts(controller.eventsForPhase(active.id), active.id);
	});

	// The selection is scoped to the phase it was made on, so navigating to
	// another phase always shows that phase's latest attempt.
	let selectedAttempt = $state<number | null>(null);
	let selectedAttemptPhase = $state<string | null>(null);

	function selectAttempt(attempt: number): void {
		selectedAttempt = attempt;
		selectedAttemptPhase = active?.kind === 'phase' ? active.id : null;
	}

	const selectedPhaseRun = $derived.by(() => {
		if (!phaseAttempts.length) return null;
		const chosen =
			selectedAttempt !== null && selectedAttemptPhase === active?.id
				? phaseAttempts.find((run) => run.attempt === selectedAttempt)
				: undefined;
		return chosen ?? phaseAttempts[phaseAttempts.length - 1];
	});

	const phases = $derived.by(() => {
		// The launch contract declares the complete phase sequence; lifecycle
		// events mark earlier phases done without hiding work that has not
		// started. Durable phase nodes are authoritative for their own status,
		// so the stepper still reflects a restart with no events loaded.
		const derived = derivePhases(controller.rootEvents);
		const byName = new Map(derived.map((phase) => [phase.name, phase]));
		for (const step of derivePhaseSteps(controller.tree, effectiveExecution?.execution_id)) {
			const existing = byName.get(step.name);
			byName.set(step.name, {
				name: step.name,
				status: step.status === 'pending' ? (existing?.status ?? 'pending') : step.status,
				attempts: existing?.attempts
			});
		}
		const merged = [...byName.values()];
		if (merged.length || !effectiveExecution?.phase) return merged;
		return [{ name: effectiveExecution.phase, status: 'active' as const }];
	});

	// The launch renders as the workflow card in the transcript, either as the
	// legacy `kind=workflow` anchor or as the `start_workflow` tool result whose
	// datum type is `workflow_execution`; the fallback live card renders only
	// when neither representation is present.
	const hasWorkflowAnchor = $derived(
		messages.some(
			(message) =>
				message.kind === 'workflow' ||
				(message.role === 'tool' &&
					message.tool_result?.datumType === 'workflow_execution')
		)
	);

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

	/** Whether one sub-agent owes a reply right now (its last scoped message is a prompt). */
	function subAgentPending(subAgentId: string): boolean {
		const scoped = messages.filter(
			(message) =>
				message.kind === 'sub_agent' &&
				String(message.data?.subAgentId ?? '') === subAgentId
		);
		if (!scoped.length) {
			return controller.subAgentById(subAgentId)?.status === 'spawned';
		}
		return scoped[scoped.length - 1].role === 'user';
	}

	// The node scope shows the typing indicator only while a reply is actually
	// owed: a running agent that has already answered is idle, not busy.
	const nodePending = $derived(composerNode ? subAgentPending(composerNode.subAgentId) : false);
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
					String(message.data?.subAgentId ?? '') === segment.id &&
					(message.role === 'user' ||
						message.role === 'system' ||
						message.role === 'tool' ||
						// Agent-authored replies are durable assistant messages on
						// the node's own transcript; only the tool-call envelope
						// placeholder is hidden (its tool result already renders).
						(message.role === 'assistant' && !isToolCallPlaceholder(message)))
			),
			events,
			children: controller.childrenOf(segment.id)
		});
	});

	function isToolCallPlaceholder(message: ChatMessage): boolean {
		const data = message.data ?? {};
		return (
			Array.isArray(data.toolCalls) &&
			message.body.startsWith('Requested tool call(s):')
		);
	}

	function enterPhase(phase: string, options: { readOnly: boolean }): void {
		void controller.enterPhase(phase, options.readOnly);
	}

	function enterSubAgent(node: SubAgentNode): void {
		controller.enterSubAgent(node);
	}

	/** Drill into a spawned sub-agent by id, awaiting the durable tree first. */
	async function enterSpawnedSubAgent(subAgentId: string): Promise<void> {
		await controller.ensureLoaded();
		const node = controller.subAgentById(subAgentId);
		if (node) controller.enterSubAgent(node);
	}

	// The registry-selected sub-agent renderer reads this context, so a spawn
	// card upgrades to the live node status and drills into the nested
	// conversation from any transcript that renders it.
	setSubAgentLiveView(() => ({
		nodeFor: (subAgentId: string) => controller.subAgentById(subAgentId),
		pendingFor: subAgentPending,
		onEnterSubAgent: (subAgentId: string) => {
			void enterSpawnedSubAgent(subAgentId);
		}
	}));

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
			{#if isPhaseScope}
				<div class="min-h-0 flex-1 overflow-y-auto p-3" data-testid="phase-run-scroll">
					<PhaseRunView
						attempts={phaseAttempts}
						selectedAttempt={selectedPhaseRun?.attempt ?? null}
						onSelectAttempt={selectAttempt}
					/>
				</div>
			{:else}
				<ScopeTranscript
					entries={scopeEntries}
					{workspaceId}
					busy={controller.busy}
					readOnly={active?.readOnly === true}
					pending={nodePending}
					onEnterSubAgent={enterSubAgent}
				/>
			{/if}
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
	{:else if isPhaseScope}
		<div
			class="border-t border-surface-700 bg-surface-900 px-3 py-2 text-center text-[11px] text-slate-500"
			data-testid="scope-readonly"
		>
			This phase is a deterministic run and does not accept input.
		</div>
	{:else}
		<ChatComposer
			onSubmit={onSend}
			activeExecutionId={effectiveExecution?.execution_id ?? null}
			{busy}
		/>
	{/if}
</div>
