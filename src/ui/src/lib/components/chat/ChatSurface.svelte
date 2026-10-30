<script lang="ts">
	import { setTranscriptDetails } from './transcript-details';
	import { tick, untrack } from 'svelte';
	import ChatComposer from './ChatComposer.svelte';
	import ChatStarters from './ChatStarters.svelte';
	import ChatTranscript from './ChatTranscript.svelte';
	import PhaseRunView from './PhaseRunView.svelte';
	import ScopeStack from './ScopeStack.svelte';
	import TranscriptViewport from './TranscriptViewport.svelte';
	import ConversationOutline from './ConversationOutline.svelte';
	import { conversationOutline, type OutlineEntry } from '$lib/conversation-outline';
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
		initialHash = null,
		showOutline = false
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
		showOutline?: boolean;
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
	setTranscriptDetails((sequence) => timeline.loadDetails(sequence));
	let attachedChatId = $state('');
	let surface = $state<HTMLDivElement>();
	let nodeSendError = $state<string | null>(null);
	let nodeDrafts = $state<Record<string, string>>({});
	let queuedFeedback = $state<Record<string, string>>({});
	let outlineOpen = $state(true);
	let selectedOutlineId = $state('');
	let viewport = $state<{ reveal: (messageId: string, executionId?: string | null) => boolean }>();
	let outlineNavigation = 0;
	const outlineAvailable = $derived(showOutline && !!chatId && !drafting);
	$effect(() => {
		if (outlineAvailable && chatId) { void chatId; untrack(() => timeline.enableOutline()); }
	});

	const workflows = $derived(withWorkflowNodes(timeline.workflows, controller.tree));
	const anyActiveWorkflow = $derived(hasActiveWorkflow(workflows));
	const outlineGroups = $derived(conversationOutline(timeline.outlineEntries, workflows, controller.knownNodes, chatId ?? ''));
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
	const oldestVisible = $derived.by(() => isNodeScope ? scopeEntries[0]?.id ?? '' : timeline.messagesFor('root')[0]?.message_id ?? '');
	const hasOlder = $derived(!isPhaseScope && !!timeline.history[historyThread]?.loaded && !timeline.history[historyThread]?.complete && !timeline.history[historyThread]?.error);
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


	async function revealOutline(entry: OutlineEntry): Promise<void> {
		const token = ++outlineNavigation;
		const selectedChat = chatId;
		selectedOutlineId = entry.id;
		nodeSendError = null;
		try {
			await controller.ensureLoaded();
			if (token !== outlineNavigation || chatId !== selectedChat) return;
			if (entry.threadId === 'root' || entry.threadId === chatId) controller.navigateTo(0);
			else {
				const node = controller.subAgentById(entry.threadId);
				if (!node) throw new Error('The conversation for this item is unavailable. Refresh the outline and try again.');
				controller.enterSubAgent(node);
			}
			if (entry.kind === 'conversation') return;
			if (!await timeline.revealOutlineEntry(entry) || token !== outlineNavigation || chatId !== selectedChat) return;
			await tick();
			await tick();
			if (token !== outlineNavigation || chatId !== selectedChat) return;
			const id = entry.datumIndex === null ? entry.messageId : `${entry.messageId}:datum:${entry.datumIndex}`;
			if (!viewport?.reveal(id, entry.kind === 'workflow' ? entry.entityId : null)) {
				throw new Error('This item has no available transcript anchor. Its retained metadata is still shown in the outline.');
			}
		} catch (error) {
			if (token === outlineNavigation) nodeSendError = error instanceof Error ? error.message : 'Could not reveal this item.';
		}
	}

	function enterPhase(executionId: string, phase: string, options: { readOnly: boolean }): void {
		outlineNavigation += 1;
		void controller.enterPhase(executionId, phase, options.readOnly || isTerminalWorkflow(workflows.get(executionId)?.execution));
	}
	setWorkflowLiveView((id) => {
		const live = workflows.get(id);
		return live ? { ...live, jobIds: derivePhaseAttempts(timeline.eventsForExecution(id), live.execution.phase ?? "").slice(-1)[0]?.jobs ?? [], onEnterPhase: (phase, options) => enterPhase(id, phase, options) } : null;
	});

	function enterSubAgent(node: SubAgentNode): void {
		outlineNavigation += 1;
		controller.enterSubAgent(node);
	}

	/** Drill into a spawned sub-agent by id, awaiting the durable tree first. */
	async function enterSpawnedSubAgent(subAgentId: string): Promise<void> {
		outlineNavigation += 1;
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
	<ScopeStack segments={controller.scope.segments} title={scoped ? 'Sub-agent' : 'Conversation'} showHeader={scoped || outlineAvailable}
		onBack={() => { outlineNavigation += 1; controller.back(); }}
		onNavigate={(index) => { outlineNavigation += 1; controller.navigateTo(index); }}>
		{#snippet controls()}
			{#if outlineAvailable}<button type="button" class="outline-toggle" onclick={() => outlineOpen = !outlineOpen} aria-label="Toggle conversation outline" aria-expanded={outlineOpen} title="Conversation outline" data-testid="outline-toggle"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></svg></button>{/if}
		{/snippet}
		<div class="chat-body">
			<div class="chat-column">
				<TranscriptViewport bind:this={viewport} scopeKey={`${chatId ?? ''}:${active?.kind ?? 'root'}:${active?.id ?? ''}`}
					oldestKey={oldestVisible} {hasOlder} loadingOlder={timeline.history[historyThread]?.loading ?? false} onOlder={() => timeline.loadHistory(historyThread, true)}
					testId={isPhaseScope ? 'phase-run-scroll' : scoped ? 'scope-transcript-scroll' : 'transcript-scroll'}>
					{#if isPhaseScope}
						<PhaseRunView attempts={phaseAttempts} selectedAttempt={selectedPhaseRun?.attempt ?? null} onSelectAttempt={selectAttempt}/>
					{:else if isNodeScope}
						<ScopeTranscript entries={scopeEntries} {workspaceId} busy={controller.busy} readOnly={active?.readOnly === true} pending={nodePending} onEnterSubAgent={enterSubAgent}/>
						{#each unanchoredWorkflows as live (live.execution.execution_id)}
							<WorkflowCard execution={live.execution} phases={live.phases} status={live.execution.state}
								summary={live.execution.phase ? `Current phase ${live.execution.phase}` : 'Awaiting phase transitions'}
								attentionPhases={live.attentionPhases} onEnterPhase={(phase, options) => enterPhase(live.execution.execution_id, phase, options)}/>
						{/each}
					{:else}
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
					{/if}
				</TranscriptViewport>

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
				agentLabel={node.role}
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
			{#if outlineAvailable && outlineOpen}
				<ConversationOutline groups={outlineGroups} loading={timeline.outlineLoading} hasMore={timeline.outlineHasMore} error={timeline.outlineError}
					selectedId={selectedOutlineId} onSelect={revealOutline} onRetry={() => { controller.refreshNodes(); void timeline.refreshOutline(); }}/>
			{/if}
		</div>
	</ScopeStack>
</div>

<style>
	.chat-body{display:flex;min-height:0;flex:1;min-width:0}.chat-column{display:flex;flex-direction:column;min-height:0;min-width:0;flex:1}
	.outline-toggle{display:flex;align-items:center;justify-content:center;width:30px;height:30px;flex-shrink:0;border:1px solid var(--color-surface-600);border-radius:5px;color:var(--color-slate-300);cursor:pointer}.outline-toggle svg{width:14px;height:14px}
	@media(max-width:480px){.chat-body{flex-direction:column}}
	@media(pointer:coarse){.outline-toggle{min-height:44px;min-width:44px}}
</style>
