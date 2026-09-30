/**
 * Data + navigation controller for the nested workflow scope view.
 *
 * Responsibilities:
 *  - derive the sub-agent tree from the durable execution event stream;
 *  - lazily load children (REST) and subscribe per active node (SSE, own
 *    events by default) on enter, unsubscribing on leave;
 *  - persist scope and expansion in localStorage and project the scope path
 *    into the `?scope=` query (deep links enter through `#sub_agent=`).
 *
 * The controller is intentionally host-neutral: the shared `ChatSurface` uses
 * one instance for the copilot pane and another for the full conversations
 * page, so the drill-in behavior is identical in both.
 */
import { untrack } from 'svelte';
import {
	apiBaseUrl,
	fixtureMode,
	listChatSubAgents,
	listExecutionEvents,
	listNodeEvents,
	postNodeMessage
} from './api';
import {
	ExecutionEventStream,
	LocalCursorStore,
	executionEventOf,
	type CursorStore
} from './sse';
import {
	deriveSubAgentTree,
	executionNodeRef,
	isWorkflowNodeRole,
	mergeEvents,
	nodeEvents,
	subAgentNodeRef,
	type SubAgentTreeNode
} from './subagent';
import {
	applyHashTarget,
	parseScope,
	scopeFromHash,
	scopeStorageKey,
	serializeScope,
	ScopeController,
	type ScopeSegment
} from './scope.svelte';
import type {
	ChatMessage,
	ExecutionEvent,
	ExecutionNodeRef,
	SubAgentNode,
	WorkflowExecutionRecord
} from './types';

function mergeNodes(existing: SubAgentNode[], incoming: SubAgentNode[]): SubAgentNode[] {
	const byId = new Map<string, SubAgentNode>();
	for (const node of existing) byId.set(node.subAgentId, node);
	// Hidden workflow-node records are kept so the tree projection can
	// re-anchor their phase agents; the projection never surfaces them.
	for (const node of incoming) byId.set(node.subAgentId, node);
	return [...byId.values()];
}

function findNode(tree: SubAgentTreeNode[], id: string): SubAgentTreeNode | null {
	for (const node of tree) {
		if (node.node.subAgentId === id) return node;
		const nested = findNode(node.children, id);
		if (nested) return nested;
	}
	return null;
}

export interface AttachOptions {
	chatId?: string | null;
	rootLabel?: string;
	scope?: string | null;
	hash?: string | null;
	/** Events arrive from the shared timeline stream instead of this controller. */
	externalEvents?: boolean;
}

export class WorkflowScopeController {
	execution = $state<WorkflowExecutionRecord | null>(null);
	/** Root execution own events. */
	rootEvents = $state<ExecutionEvent[]>([]);
	/** Nodes discovered from the durable chat-scoped tree. */
	knownNodes = $state<SubAgentNode[]>([]);
	/** Own events per sub-agent node, loaded lazily. */
	nodeEventCache = $state<Record<string, ExecutionEvent[]>>({});
	/** Persisted expansion state per node id. */
	expanded = $state<Record<string, boolean>>({});
	busy = $state(false);
	error = $state('');
	readonly scope: ScopeController;

	#workspaceId: string;
	#executionId = '';
	#chatId: string | null = null;
	#hostId: string;
	#cursorStore = new LocalCursorStore();
	#subscriptions = new Map<string, ExecutionEventStream>();
	#readonlyCursorStore: CursorStore;
	/** Resolves once the initial root events and sub-agent list are loaded. */
	#initialLoad: Promise<void> | null = null;
	#refreshingNodes = false;

	constructor(workspaceId: string, hostId: string, cursorStore?: CursorStore) {
		this.#workspaceId = workspaceId;
		this.#hostId = hostId;
		this.#readonlyCursorStore = cursorStore ?? this.#cursorStore;
		this.scope = new ScopeController(scopeStorageKey(workspaceId, hostId));
	}

	/** Merge events delivered by the shared chat timeline stream. */
	ingestEvents(events: ExecutionEvent[]): void {
		if (!events.length) return;
		// Read the existing stream untracked: the caller runs inside an effect,
		// and reading the state we write here would re-trigger that effect.
		this.rootEvents = untrack(() => mergeEvents(this.rootEvents, events));
	}

	/** All events known to the client: root plus every lazily loaded node. */
	get flatEvents(): ExecutionEvent[] {
		let merged = this.rootEvents;
		for (const events of Object.values(this.nodeEventCache)) {
			merged = mergeEvents(merged, events);
		}
		return merged;
	}

	get tree(): SubAgentTreeNode[] {
		return deriveSubAgentTree(this.flatEvents, this.knownNodes);
	}

	get activeSegment(): ScopeSegment | null {
		return this.scope.active;
	}

	get expansionKey(): string {
		return `thesis.subagent.expanded.${this.#workspaceId}.${this.#executionId}`;
	}

	/** Attach to one execution, restoring scope/expansion and subscribing. */
	attach(executionId: string, execution: WorkflowExecutionRecord | null, options: AttachOptions = {}): void {
		this.detach();
		this.#executionId = executionId;
		const attachedExecutionId = executionId;
		this.#chatId = options.chatId ?? execution?.chat_id ?? null;
		// Chat-mode attaches share one empty execution id, so the fetch guards
		// also pin the chat: a listing that resolves after a chat switch is
		// discarded instead of merging into the new chat's tree.
		const attachedChatId = this.#chatId;
		this.execution = execution;
		this.rootEvents = [];
		this.knownNodes = [];
		this.nodeEventCache = {};
		this.error = '';
		this.expanded = this.readExpansion();

		const root: ScopeSegment = {
			kind: 'conversation',
			id: options.chatId ?? execution?.chat_id ?? executionId,
			label: options.rootLabel ?? 'Conversation'
		};
		let segments = parseScope(options.scope).filter(
			(segment) => segment.kind !== 'workflow'
		);
		// Conversation identity: a parsed root naming another chat is URL
		// residue from a previous conversation - drop it and everything after
		// it; this conversation's own root is substituted below.
		if (segments[0]?.kind === 'conversation' && segments[0].id !== root.id) {
			segments = [];
		}
		if (!segments.some((segment) => segment.kind === 'conversation')) segments = [root, ...segments];
		segments = applyHashTarget(segments, scopeFromHash(options.hash)).filter(
			(segment) => segment.kind !== 'workflow'
		);
		this.scope.reset(segments);

		let eventsReady = Promise.resolve();
		if (!options.externalEvents) {
			eventsReady = listExecutionEvents(this.#workspaceId, executionId, {})
				.then((events) => {
					if (this.#executionId !== attachedExecutionId) return;
					this.rootEvents = mergeEvents(this.rootEvents, events);
				})
				.catch((error: unknown) => {
					if (this.#executionId !== attachedExecutionId) return;
					this.error = error instanceof Error ? error.message : 'Failed to load events';
				});
		}

		let nodesLoaded = false;
		const nodesReady = this.#chatId
			? listChatSubAgents(this.#workspaceId, this.#chatId)
					.then((nodes) => {
						if (this.#executionId !== attachedExecutionId || this.#chatId !== attachedChatId) return;
						this.knownNodes = mergeNodes(this.knownNodes, nodes);
						nodesLoaded = true;
					})
					.catch(() => undefined)
			: Promise.resolve();

		// The scope is normalized only once the durable tree is known, so a
		// persisted segment that names a hidden workflow node (or a node this
		// chat does not own) is dropped instead of being restored and queried.
		this.#initialLoad = Promise.all([eventsReady, nodesReady]).then(() => {
			if (this.#executionId !== attachedExecutionId || this.#chatId !== attachedChatId) return;
			if (nodesLoaded) this.normalizeScope();
			this.restoreActiveSubscription();
		});

		if (!options.externalEvents) this.#subscribe(executionNodeRef(executionId));
	}

	detach(): void {
		for (const stream of this.#subscriptions.values()) stream.stop();
		this.#subscriptions.clear();
		this.#executionId = '';
		this.#initialLoad = null;
	}

	/**
	 * Attach to a chat's sub-agent tree without any workflow execution.
	 *
	 * Chat-spawned helpers have no activity log, so nothing is subscribed and
	 * no execution events are loaded: the durable tree alone drives
	 * navigation, and the scoped transcripts render from the shared chat
	 * timeline. The empty execution id keeps phase lookups unscoped and the
	 * execution subscription dormant.
	 */
	attachChat(chatId: string, options: AttachOptions = {}): void {
		this.attach('', null, { ...options, chatId, externalEvents: true });
	}

	/**
	 * Re-fetch the durable chat sub-agent tree.
	 *
	 * Chat-spawned agents have no activity log, so their durable status only
	 * advances on the record itself; the surface refreshes the tree whenever
	 * the chat timeline moves so the spawn cards stay current without an
	 * event stream. A refresh already in flight is skipped, not queued.
	 */
	refreshNodes(): void {
		const chatId = this.#chatId;
		if (!chatId || this.#refreshingNodes) return;
		this.#refreshingNodes = true;
		listChatSubAgents(this.#workspaceId, chatId)
			.then((nodes) => {
				if (this.#chatId !== chatId) return;
				this.knownNodes = mergeNodes(this.knownNodes, nodes);
			})
			.catch(() => undefined)
			.finally(() => {
				this.#refreshingNodes = false;
			});
	}

	/** Wait for the initial root events and sub-agent list to load. */
	async ensureLoaded(): Promise<void> {
		if (this.#initialLoad) await this.#initialLoad;
	}

	/**
	 * Enter the view that governs a phase.
	 *
	 * An agent phase is governed by a visible node: the operator drills into
	 * its orchestrator conversation.  A deterministic phase has no node and no
	 * conversation (REQ-D1): entering it pushes a `phase` scope segment, whose
	 * segment renders the run view (live progress lines, then the result).
	 */
	async enterPhase(phase: string, readOnly = false): Promise<void> {
		await this.ensureLoaded();
		const node = this.findPhaseNode(phase);
		if (node) {
			this.enterSubAgent(node);
			if (readOnly) this.scope.updateActive({ readOnly: true });
			return;
		}
		this.scope.push({
			kind: 'phase',
			id: phase,
			label: `Phase ${phase}`,
			// A deterministic phase is always a read-only run view.
			readOnly: true
		});
	}

	/** The execution-scoped events recorded for one phase (REQ-C2). */
	eventsForPhase(phase: string): ExecutionEvent[] {
		return this.flatEvents.filter(
			(event) =>
				(event.eventType === 'workflow.phase' ||
					event.eventType === 'workflow.progress' ||
					event.eventType === 'workflow.phase_result') &&
				event.payload['phase'] === phase
		);
	}

	/** The phase agent for one phase name, scoped to this execution. */
	private findPhaseNode(phase: string): SubAgentNode | null {
		const matches: SubAgentNode[] = [];
		const visit = (nodes: SubAgentTreeNode[]): void => {
			for (const item of nodes) {
				if (!isWorkflowNodeRole(item.node.role) && item.node.phase === phase) {
					matches.push(item.node);
				}
				visit(item.children);
			}
		};
		visit(this.tree);
		if (!this.#executionId) return matches[0] ?? null;
		return (
			matches.find((node) => node.workflowExecutionId === this.#executionId) ?? null
		);
	}

	/**
	 * Enter one sub-agent node, subscribing and lazily loading its events.
	 *
	 * The full visible ancestor chain is projected into the scope path, so a
	 * phase agent always has its anchor beneath it and Back returns there. When
	 * the chain already shares a prefix with the scope the path is truncated to
	 * that prefix, so sibling nodes switch in place instead of accumulating.
	 * Hidden workflow-node records are never entered.
	 */
	enterSubAgent(node: SubAgentNode): void {
		if (isWorkflowNodeRole(node.role)) return;
		const chain = [...this.ancestorsOf(node), node];
		let keepIndex = this.scope.segments.findIndex(
			(segment) => segment.kind === 'conversation'
		);
		if (keepIndex === -1) keepIndex = 0;
		let start = 0;
		for (let index = 0; index < chain.length - 1; index += 1) {
			const found = this.scope.segments.findIndex(
				(segment) => segment.kind === 'sub_agent' && segment.id === chain[index].subAgentId
			);
			if (found !== -1) {
				keepIndex = found;
				start = index + 1;
			}
		}
		if (keepIndex < this.scope.segments.length - 1) this.navigateTo(keepIndex);
		for (let index = start; index < chain.length; index += 1) {
			this.scope.push(this.segmentFor(chain[index]));
		}
		this.loadNode(node.subAgentId);
	}

	/** The visible ancestors of a node, outermost first. */
	private ancestorsOf(node: SubAgentNode): SubAgentNode[] {
		const chain: SubAgentNode[] = [];
		const seen = new Set<string>([node.subAgentId]);
		let parentId = node.parentSubAgentId;
		while (parentId && !seen.has(parentId)) {
			seen.add(parentId);
			const parent = this.subAgentById(parentId);
			if (!parent || isWorkflowNodeRole(parent.role)) break;
			chain.unshift(parent);
			parentId = parent.parentSubAgentId;
		}
		return chain;
	}

	private segmentFor(node: SubAgentNode): ScopeSegment {
		return {
			kind: 'sub_agent',
			id: node.subAgentId,
			label: node.role || node.subAgentId,
			role: node.role,
			phase: node.phase
		};
	}

	/**
	 * Drop scope segments that name a hidden workflow node or a node this chat
	 * does not own, and re-seat surviving segments on their full ancestor path.
	 * A stale deep link can never leave the operator inside an implementation
	 * record or orphaned under a node that no longer exists.
	 */
	private normalizeScope(): void {
		const segments = this.scope.segments;
		if (!segments.length) return;
		// The declared phase sequence this execution launched with; a phase
		// segment naming a phase outside it belongs to another execution.
		const declared = new Set(
			this.rootEvents
				.filter((event) => event.eventType === 'workflow.launched')
				.flatMap((event) => {
					const workflow = event.payload?.['workflow'];
					const phases =
						workflow && typeof workflow === 'object'
							? (workflow as Record<string, unknown>)['phases']
							: null;
					return Array.isArray(phases)
						? phases.filter((phase): phase is string => typeof phase === 'string')
						: [];
				})
		);
		const rebuilt: ScopeSegment[] = [segments[0]];
		for (const segment of segments.slice(1)) {
			// Phase segments are view state over the event stream, not node
			// records - but a stale one from a previous conversation's URL must
			// not replay onto this execution. Keep it only when this run
			// actually declared the phase.
			if (segment.kind === 'phase') {
				if (declared.size === 0 || declared.has(segment.id)) {
					rebuilt.push(segment);
				}
				continue;
			}
			if (segment.kind !== 'sub_agent') {
				rebuilt.push(segment);
				continue;
			}
			const node = this.subAgentById(segment.id);
			if (!node || isWorkflowNodeRole(node.role)) break;
			for (const item of [...this.ancestorsOf(node), node]) {
				if (
					rebuilt.some(
						(candidate) => candidate.kind === 'sub_agent' && candidate.id === item.subAgentId
					)
				) {
					continue;
				}
				rebuilt.push(this.segmentFor(item));
			}
		}
		if (serializeScope(rebuilt) !== serializeScope(segments)) this.scope.reset(rebuilt);
	}

	back(): void {
		const popped = this.scope.back();
		if (popped?.kind === 'sub_agent') this.#unsubscribe(this.#subscriptionKey(subAgentNodeRef(popped.id)));
	}

	navigateTo(index: number): void {
		const removed = this.scope.segments.slice(index + 1);
		this.scope.navigateTo(index);
		for (const segment of removed) {
			if (segment.kind === 'sub_agent') {
				this.#unsubscribe(this.#subscriptionKey(subAgentNodeRef(segment.id)));
			}
		}
	}

	/** Subscribe to whichever node the restored scope points at. */
	private restoreActiveSubscription(): void {
		const active = this.scope.active;
		if (active?.kind === 'sub_agent') {
			this.loadNode(active.id);
		}
	}

	/** Lazy-load a node's own events and direct children. */
	loadNode(subAgentId: string): void {
		// Live events arrive on the single execution subscription (it carries
		// every node's events), so a node needs only its history page here. This
		// keeps one SSE connection per execution instead of one per visited node,
		// which matters because browsers cap concurrent connections per origin.
		if (this.nodeEventCache[subAgentId]) return;
		// Never request activity for a hidden implementation record, a node the
		// rendered tree does not contain, or a node without a workflow execution
		// (chat-spawned helpers have no activity log): the backend answers 404
		// or 409 for those, and no such request may ever be issued.
		const node = this.subAgentById(subAgentId);
		if (!node || isWorkflowNodeRole(node.role) || !node.workflowExecutionId) return;
		const chatId = this.#chatId;
		if (!chatId) return;
		listNodeEvents(this.#workspaceId, chatId, subAgentNodeRef(subAgentId), {
			includeDescendants: false
		})
			.then((events) => {
				if (this.#chatId !== chatId) return;
				this.nodeEventCache = {
					...this.nodeEventCache,
					[subAgentId]: mergeEvents(this.nodeEventCache[subAgentId] ?? [], events)
				};
			})
			.catch(() => undefined);
	}

	/** Toggle a node's expansion; first expand lazily loads its children. */
	toggleExpand(subAgentId: string): void {
		const next = !this.expanded[subAgentId];
		this.expanded = { ...this.expanded, [subAgentId]: next };
		this.writeExpansion();
		if (next) this.loadChildren(subAgentId);
	}

	isExpanded(subAgentId: string): boolean {
		return this.expanded[subAgentId] === true;
	}

	/** The durable tree already carries every node; nothing to lazy-load. */
	loadChildren(subAgentId: string): void {
		this.loadNode(subAgentId);
	}

	childrenOf(subAgentId: string): SubAgentTreeNode[] {
		const found = findNode(this.tree, subAgentId);
		return found?.children ?? [];
	}

	subAgentById(id: string): SubAgentNode | null {
		const found =
			findNode(this.tree, id)?.node ??
			this.knownNodes.find((node) => node.subAgentId === id) ??
			null;
		return found && isWorkflowNodeRole(found.role) ? null : found;
	}

	eventsForSubAgent(id: string): ExecutionEvent[] {
		const cached = this.nodeEventCache[id];
		const attributed = nodeEvents(this.flatEvents, id);
		return cached ? mergeEvents(attributed, cached) : attributed;
	}

	/** Submit operator input to a node that accepts it. */
	async submit(node: SubAgentNode, body: string): Promise<ChatMessage | null> {
		const chatId = this.#chatId;
		if (
			isWorkflowNodeRole(node.role) ||
			!node.acceptsInput ||
			!body.trim() ||
			!chatId
		) {
			return null;
		}
		this.busy = true;
		try {
			return await postNodeMessage(
				this.#workspaceId,
				chatId,
				this.nodeRefFor(node.subAgentId),
				{ body: body.trim() }
			);
		} catch (error: unknown) {
			this.error = error instanceof Error ? error.message : 'Message failed';
			return null;
		} finally {
			this.busy = false;
		}
	}

	private nodeRefFor(subAgentId: string): ExecutionNodeRef {
		return subAgentNodeRef(subAgentId);
	}

	#subscriptionKey(node: ExecutionNodeRef): string {
		return `${node.kind}:${node.id}`;
	}

	#subscribe(node: ExecutionNodeRef): void {
		if (fixtureMode) return;
		const key = this.#subscriptionKey(node);
		if (this.#subscriptions.has(key)) return;
		const stream = new ExecutionEventStream({
			baseUrl: apiBaseUrl,
			workspaceId: this.#workspaceId,
			executionId: this.#executionId,
			chatId: this.#chatId,
			cursorStore: this.#readonlyCursorStore,
			cursorKey: `${this.#executionId}:${key}`,
			subAgentId: node.kind === 'sub_agent' ? node.id : null,
			includeDescendants: false
		});
		this.#subscriptions.set(key, stream);
		void stream.start((frame) => {
			const event = executionEventOf(frame);
			if (!event) return;
			if (node.kind === 'execution') {
				this.rootEvents = mergeEvents(this.rootEvents, [event]);
			} else {
				this.nodeEventCache = {
					...this.nodeEventCache,
					[node.id]: mergeEvents(this.nodeEventCache[node.id] ?? [], [event])
				};
			}
		});
	}

	#unsubscribe(key: string): void {
		const stream = this.#subscriptions.get(key);
		if (!stream) return;
		stream.stop();
		this.#subscriptions.delete(key);
	}

	private readExpansion(): Record<string, boolean> {
		try {
			if (typeof localStorage === 'undefined') return {};
			const raw = localStorage.getItem(this.expansionKey);
			if (!raw) return {};
			const parsed = JSON.parse(raw) as Record<string, unknown>;
			const out: Record<string, boolean> = {};
			for (const [id, value] of Object.entries(parsed)) {
				if (value === true) out[id] = true;
			}
			return out;
		} catch {
			return {};
		}
	}

	private writeExpansion(): void {
		try {
			if (typeof localStorage === 'undefined') return;
			localStorage.setItem(this.expansionKey, JSON.stringify(this.expanded));
		} catch {
			// Best-effort persistence.
		}
	}
}
