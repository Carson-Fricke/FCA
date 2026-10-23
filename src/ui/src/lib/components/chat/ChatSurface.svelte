<script lang="ts">
	import { tick, untrack } from 'svelte';
	import ChatComposer from './ChatComposer.svelte';
	import ChatStarters from './ChatStarters.svelte';
	import ChatTranscript from './ChatTranscript.svelte';
	import PhaseRunView from './PhaseRunView.svelte';
	import ScopeStack from './ScopeStack.svelte';
	import ScopeTranscript from './ScopeTranscript.svelte';
	import SubAgentComposer from './SubAgentComposer.svelte';
	import WorkflowCard from './WorkflowCard.svelte';
	import { setSubAgentLiveView } from './sub-agent-live-view';
	import { setWorkflowLiveView } from './workflow-live-view';
	import { ThesisApiError } from '$lib/api';
	import { WorkflowScopeController } from '$lib/workflow-scope.svelte';
	import type { TimelineController } from '$lib/timeline.svelte';
	import { buildNodeEntries, threadActivity } from '$lib/transcript';
	import { derivePhaseAttempts } from '$lib/subagent';
	import { hasActiveWorkflow, isTerminalWorkflow, withWorkflowNodes } from '$lib/workflow-live';
	import type {
		ChatMessage,
		ConsentConfirmation,
		ConsentProposal,
		SteeringPercolationTarget,
		SubAgentNode
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
		drafting = chatId === null,
		terminal = false,
		timeline,
		percolations = {},
		proposal = null,
		onConfirmConsent,
		onRejectConsent,
		consentBusy = false,
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
		drafting?: boolean;
		terminal?: boolean;
		timeline: TimelineController;
		percolations?: Record<string, SteeringPercolationTarget[]>;
		proposal?: ConsentProposal | null;
		onConfirmConsent?: (confirmation: ConsentConfirmation) => void;
		onRejectConsent?: () => void;
		consentBusy?: boolean;
		busy?: boolean;
		onSend: (input: {
			body: string;
			attachmentIds: string[];
			steeringIntent: string | null;
		}) => void | Promise<void>;
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
		hostId,
		// svelte-ignore state_referenced_locally
		timeline
	);
	const messages = $derived(timeline.messages);
	let attachedChatId = $state('');
	let surface = $state<HTMLDivElement>();
	let nodeSendError = $state<string | null>(null);
	let nodeDrafts = $state<Record<string, string>>({});
	let queuedFeedback = $state<Record<string, string>>({});

	const workflows = $derived(withWorkflowNodes(timeline.workflows, controller.tree));
	const anyActiveWorkflow = $derived(hasActiveWorkflow(workflows));
	$effect(() => controller.resolvePendingPhase());

	// Scope belongs to the chat, never to whichever workflow launched most recently.
	$effect(() => {
		if (chatId && chatId !== attachedChatId) {
			attachedChatId = chatId;
			untrack(() => controller.attachChat(chatId!, { rootLabel, scope: initialScope, hash: initialHash }));
		} else if (!chatId && attachedChatId) {
			attachedChatId = '';
			controller.detach();
		}
	});

	// Capabilities are durable node metadata, refreshed as the shared timeline advances.
	let nodeRefreshSeeded = false;
	let nodeRefreshItem = '';
	let nodeRefreshChat = '';
	$effect(() => {
		const timelineMoved = timeline.nodeRevision;
		if (nodeRefreshChat !== (chatId ?? '')) {
			nodeRefreshChat = chatId ?? '';
			nodeRefreshSeeded = false;
			nodeRefreshItem = '';
		}
		if (!chatId) return;
		if (!nodeRefreshSeeded || !timelineMoved) {
			nodeRefreshSeeded = true;
			nodeRefreshItem = timelineMoved;
			return;
		}
		if (timelineMoved === nodeRefreshItem) return;
		nodeRefreshItem = timelineMoved;
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
	const historyThread = $derived(isNodeScope && active ? active.id : 'root');
	const unanchoredWorkflows = $derived([...workflows.values()].filter((live) => !live.anchored && !timeline.initialLoading && (live.execution.conversation_id ?? chatId) === (isNodeScope && active ? active.id : chatId)));

	/**
	 * The active phase's run view state (REQ-D2): every attempt's
	 * `workflow.progress` lines, then its `workflow.phase_result` — the error
	 * variant when the artifact is one. A loopback that re-enters the phase
	 * keeps every earlier attempt, so the view selects among them.
	 */
	const phaseAttempts = $derived.by(() => {
		if (!active || active.kind !== 'phase') return [];
		if (!active.executionId || !active.phase) return [];
		return derivePhaseAttempts(timeline.eventsForExecution(active.executionId), active.phase);
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

	const composerNode = $derived(isNodeScope ? controller.subAgentById(active!.id) : null);

	/** Whether one sub-agent owes a reply right now (its last scoped message is a prompt). */
	function subAgentPending(subAgentId: string): boolean {
		const scoped = timeline.messagesFor(subAgentId);
		if (!scoped.length) {
			return controller.subAgentById(subAgentId)?.status === 'spawned';
		}
		const node = controller.subAgentById(subAgentId);
		return threadActivity(scoped, timeline.eventsFor(subAgentId),
			!!node && ['completed', 'failed', 'cancelled'].includes(node.status)).pending;
	}

	// The node scope shows the typing indicator only while a reply is actually
	// owed: a running agent that has already answered is idle, not busy.
	const nodeTerminal = $derived(composerNode?.status === 'completed' || composerNode?.status === 'failed' || composerNode?.status === 'cancelled' ||
		isTerminalWorkflow(composerNode?.workflowExecutionId ? workflows.get(composerNode.workflowExecutionId)?.execution : null));
	const nodePending = $derived(!nodeTerminal && composerNode ? subAgentPending(composerNode.subAgentId) : false);
	const showNodeComposer = $derived(
		isNodeScope && composerNode?.acceptsInput === true && active?.readOnly !== true && !nodeTerminal
	);
	const showQueuedFeedback = $derived.by(() => {
		if (!composerNode || !showNodeComposer) return false;
		const feedback = queuedFeedback[composerNode.subAgentId];
		return threadActivity(timeline.messagesFor(composerNode.subAgentId), timeline.eventsFor(composerNode.subAgentId), nodeTerminal, feedback).queued;
	});

	const scopeEntries = $derived.by(() => {
		const segment = active;
		if (!segment || segment.kind !== 'sub_agent') return [];
		return buildNodeEntries({
			messages: timeline.messagesFor(segment.id),
			events: timeline.eventsFor(segment.id),
			children: controller.childrenOf(segment.id),
			terminal: nodeTerminal
		});
	});

	let scrollScope = '';
	let lastUserMessageId = '';
	$effect(() => {
		const scope = `${chatId ?? ''}:${active?.kind ?? 'root'}:${active?.id ?? ''}`;
		const visibleMessages = isNodeScope
			? scopeEntries.flatMap((entry) => entry.kind === 'message' ? [entry.message] : [])
			: timeline.messagesFor('root');
		const userMessageId = visibleMessages.findLast((message) => message.role === 'user')?.message_id ?? '';
		if (scope !== scrollScope) {
			scrollScope = scope;
			lastUserMessageId = userMessageId;
			return;
		}
		if (!userMessageId || userMessageId === lastUserMessageId) return;
		lastUserMessageId = userMessageId;
		// Wait for the sent message to render, without following agent/tool updates.
		void tick().then(() => {
			if (scrollScope !== scope) return;
			const transcript = surface?.querySelector<HTMLElement>(
				'[data-testid="transcript-scroll"], [data-testid="scope-transcript-scroll"]'
			);
			if (transcript) transcript.scrollTop = transcript.scrollHeight;
		});
	});

	function enterPhase(executionId: string, phase: string, options: { readOnly: boolean }): void {
		void controller.enterPhase(executionId, phase, options.readOnly || isTerminalWorkflow(workflows.get(executionId)?.execution));
	}
	setWorkflowLiveView((id) => {
		const live = workflows.get(id);
		return live ? { ...live, jobIds: derivePhaseAttempts(timeline.eventsForExecution(id), live.execution.phase ?? "").slice(-1)[0]?.jobs ?? [], onEnterPhase: (phase, options) => enterPhase(id, phase, options) } : null;
	});

	function enterSubAgent(node: SubAgentNode): void {
		controller.enterSubAgent(node);
	}

	/** Drill into a spawned sub-agent by id, awaiting the durable tree first. */
	async function enterSpawnedSubAgent(subAgentId: string): Promise<void> {
		if (!controller.subAgentById(subAgentId)) await controller.ensureLoaded();
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

	/** Turn a rejected node send into operator-facing text. */
	function nodeSendFailure(error: unknown): string {
		if (error instanceof ThesisApiError && error.status === 409) {
			return 'This sub-agent no longer accepts input: the run moved on, or the backend restarted and stopped it. Refresh and open the active phase, or start a new run.';
		}
		return error instanceof Error
			? `Message could not be sent: ${error.message}`
			: 'Message could not be sent.';
	}

	// A scope change clears the previous rejection: the operator is looking at
	// a different node now.
	$effect(() => {
		void active?.id;
		nodeSendError = null;
	});

	async function sendNode(node: SubAgentNode, body: string): Promise<void> {
		nodeSendError = null;
		const running = nodePending || node.status === 'running' || node.status === 'progress';
		try {
			const message = await controller.submit(node, body);
			if (running) queuedFeedback[node.subAgentId] = message.message_id;
			onNodeMessage?.(message);
		} catch (error) {
			nodeSendError = nodeSendFailure(error);
			// The tree the surface renders may be stale (for example after a
			// backend restart terminalized the node): refresh it so the
			// composer stops offering a closed node.
			controller.refreshNodes();
			throw error;
		}
	}
</script>

<div bind:this={surface} class="flex min-h-0 flex-1 flex-col" data-testid="chat-surface">
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
				{#each unanchoredWorkflows as live (live.execution.execution_id)}
					<WorkflowCard execution={live.execution} phases={live.phases} status={live.execution.state}
						summary={live.execution.phase ? `Current phase ${live.execution.phase}` : 'Awaiting phase transitions'}
						attentionPhases={live.attentionPhases}
						onEnterPhase={(phase, options) => enterPhase(live.execution.execution_id, phase, options)} />
				{/each}
			{/if}
		</ScopeStack>
	{:else}
		<div class="min-h-0 flex-1 overflow-y-auto p-3" data-testid="transcript-scroll">
			{#if drafting && !chatId && !messages.length}
				<div class="mb-3">
					<p class="mb-2 text-xs text-slate-500">Start with a suggestion, or ask anything.</p>
					<ChatStarters
						onSelect={(prompt) => {
							void Promise.resolve(onSend({ body: prompt, attachmentIds: [], steeringIntent: null }))
								.catch((error) => { nodeSendError = error instanceof Error ? error.message : 'Message could not be sent.'; });
						}}
						disabled={busy}
					/>
				</div>
			{/if}
			<ChatTranscript
				messages={timeline.messagesFor('root')}
				{terminal}
				events={timeline.eventsFor('root')}
				{workspaceId}
				{percolations}
				{proposal}
				{onConfirmConsent}
				{onRejectConsent}
				{consentBusy}
				{workflows}
				onEnterPhase={enterPhase}
			/>

			{#each unanchoredWorkflows as live (live.execution.execution_id)}
				<div class="mt-3">
					<WorkflowCard
						execution={live.execution}
						phases={live.phases}
						status={live.execution.state}
						summary={live.execution.phase
							? `Current phase ${live.execution.phase}`
							: 'Awaiting phase transitions'}
						attentionPhases={live.attentionPhases}
                        jobIds={derivePhaseAttempts(timeline.eventsForExecution(live.execution.execution_id), live.execution.phase ?? "").slice(-1)[0]?.jobs ?? []}
						onEnterPhase={(phase, options) => enterPhase(live.execution.execution_id, phase, options)}
					/>
				</div>
			{/each}
		</div>
	{/if}

	{#if timeline.streamError}
		<p class="px-3 py-2 text-xs text-warn" role="status" data-testid="timeline-stream-error">{timeline.streamError}</p>
	{/if}
	{#if controller.error}
		<p class="px-3 py-2 text-xs text-warn" role="status" data-testid="node-metadata-error">{controller.error}</p>
		<button class="px-3 py-2 text-xs text-accent" onclick={() => controller.refreshNodes()}>Retry Agent Metadata</button>
	{/if}
	{#if nodeSendError}
		<div
			class="border-t border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200"
			data-testid="node-send-error"
			role="status"
		>
			{nodeSendError}
		</div>
	{/if}
	{#if timeline.metadataError}
		<p class="px-3 py-2 text-xs text-warn" role="status">{timeline.metadataError}</p>
		<button class="px-3 py-2 text-xs text-accent" onclick={() => timeline.refreshMetadata()}>Retry Workflow Metadata</button>
	{/if}
	{#if timeline.history[historyThread]?.loaded && !timeline.history[historyThread]?.complete && !timeline.history[historyThread]?.error}
		<button class="px-3 py-2 text-xs text-accent" data-testid="history-older"
			disabled={timeline.history[historyThread]?.loading} onclick={() => timeline.loadHistory(historyThread, true)}>Load older history</button>
	{/if}
	{#if timeline.history[historyThread]?.error}
		<p class="px-3 py-2 text-xs text-warn" role="status">{timeline.history[historyThread].error}</p>
			<button class="px-3 py-2 text-xs text-accent" data-testid="history-retry"
				disabled={timeline.history[historyThread]?.loading} onclick={() => timeline.loadHistory(historyThread)}>Retry History</button>
	{/if}
	{#if timeline.history[historyThread]?.loading}
		<p class="px-3 py-2 text-xs text-slate-500" role="status" data-testid="history-loading">Loading conversation history...</p>
	{/if}
	{#if showQueuedFeedback}
		<p class="border-t border-surface-700 px-3 py-2 text-xs text-accent" role="status" data-testid="node-feedback-queued">
			Feedback accepted while this agent is running, queued for delivery.
		</p>
	{/if}

	{#if showNodeComposer && composerNode}
		{#each [composerNode] as node (node.subAgentId)}
			<SubAgentComposer
				acceptsInput={true}
				bind:draft={() => nodeDrafts[node.subAgentId] ?? '', (value) => { nodeDrafts[node.subAgentId] = value; }}
				phase={node.phase ?? undefined}
				busy={controller.busy}
				onSubmit={(body) => sendNode(node, body)}
			/>
		{/each}
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
			No conversational agent is available for this phase. See phase activity for status or errors.
		</div>
	{:else}
		<ChatComposer
			onSubmit={onSend}
			{anyActiveWorkflow}
			{busy}
		/>
	{/if}
</div>
