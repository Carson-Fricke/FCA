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
	listExecutionSubAgents,
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
	/** Nodes discovered from REST listings, merged with stream-derived nodes. */
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
	#hostId: string;
	#cursorStore = new LocalCursorStore();
	#subscriptions = new Map<string, ExecutionEventStream>();
	#readonlyCursorStore: CursorStore;
	/** Resolves once the initial root events and sub-agent list are loaded. */
	#initialLoad: Promise<void> | null = null;

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
		if (!segments.some((segment) => segment.kind === 'conversation')) segments = [root, ...segments];
		segments = applyHashTarget(segments, scopeFromHash(options.hash)).filter(
			(segment) => segment.kind !== 'workflow'
		);
		this.scope.reset(segments);

		let eventsReady = Promise.resolve();
		if (!options.externalEvents) {
			eventsReady = listNodeEvents(this.#workspaceId, executionId, executionNodeRef(executionId), {
				includeDescendants: false
			})
				.then((events) => {
					if (this.#executionId !== attachedExecutionId) return;
					this.rootEvents = mergeEvents(this.rootEvents, events);
				})
				.catch((error: unknown) => {
					if (this.#executionId !== attachedExecutionId) return;
					this.error = error instanceof Error ? error.message : 'Failed to load events';
				});
		}

		const nodesReady = listExecutionSubAgents(this.#workspaceId, executionId)
			.then((nodes) => {
				if (this.#executionId !== attachedExecutionId) return;
				this.knownNodes = mergeNodes(this.knownNodes, nodes);
			})
			.catch(() => undefined);

		this.#initialLoad = Promise.all([eventsReady, nodesReady]).then(() => undefined);

		if (!options.externalEvents) this.#subscribe(executionNodeRef(executionId));
		this.restoreActiveSubscription();
	}

	detach(): void {
		for (const stream of this.#subscriptions.values()) stream.stop();
		this.#subscriptions.clear();
		this.#executionId = '';
		this.#initialLoad = null;
	}

	/** Wait for the initial root events and sub-agent list to load. */
	async ensureLoaded(): Promise<void> {
		if (this.#initialLoad) await this.#initialLoad;
	}

	/**
	 * Enter the sub-agent that governs a phase.
	 *
	 * The real phase orchestrator is located in the projected tree so the
	 * operator drills straight into the actual node (its messages, events, and
	 * composer target). The tree is awaited first so a click that races the
	 * initial load still resolves to the real node instead of a placeholder.
	 */
	async enterPhase(phase: string, readOnly = false): Promise<void> {
		await this.ensureLoaded();
		const node =
			this.tree.find((candidate) => candidate.node.phase === phase)?.node ??
			this.latestOrchestrator();
		if (!node) return;
		this.enterSubAgent(node);
		if (readOnly) this.scope.updateActive({ readOnly: true });
	}

	/**
	 * The most recently spawned top-level phase orchestrator, used when the
	 * requested phase has no sub-agent of its own (for example the
	 * pre-orchestration `requested` phase).
	 */
	private latestOrchestrator(): SubAgentNode | null {
		let best: { node: SubAgentNode; cursor: number } | null = null;
		for (const treeNode of this.tree) {
			if (treeNode.node.parentSubAgentId !== null) continue;
			const spawn = treeNode.events.find((event) => event.eventType === 'sub_agent.spawned');
			const cursor = spawn?.cursor ?? 0;
			if (best === null || cursor >= best.cursor) {
				best = { node: treeNode.node, cursor };
			}
		}
		return best?.node ?? null;
	}

	/** Enter one sub-agent node, subscribing and lazily loading its events. */
	enterSubAgent(node: SubAgentNode): void {
		// Truncate to the node's parent when it is already in scope, so sibling
		// sub-agents switch in place instead of accumulating on the path. A
		// root-level phase agent truncates to the conversation root.
		const parentIndex = node.parentSubAgentId
			? this.scope.segments.findIndex(
					(segment) => segment.kind === 'sub_agent' && segment.id === node.parentSubAgentId
				)
			: this.scope.segments.findIndex((segment) => segment.kind === 'conversation');
		if (parentIndex !== -1 && parentIndex < this.scope.segments.length - 1) {
			const removed = this.scope.segments.slice(parentIndex + 1);
			this.scope.navigateTo(parentIndex);
			for (const segment of removed) {
				if (segment.kind === 'sub_agent') {
					this.#unsubscribe(this.#subscriptionKey(subAgentNodeRef(segment.id)));
				}
			}
		}
		this.scope.push({
			kind: 'sub_agent',
			id: node.subAgentId,
			label: node.role || node.subAgentId,
			role: node.role,
			phase: node.phase
		});
		this.loadNode(node.subAgentId);
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
		const executionId = this.#executionId;
		listNodeEvents(this.#workspaceId, executionId, subAgentNodeRef(subAgentId), {
			includeDescendants: false
		})
			.then((events) => {
				if (this.#executionId !== executionId) return;
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

	/** Lazy-load a node's direct children from REST. */
	loadChildren(subAgentId: string): void {
		this.loadNode(subAgentId);
		const executionId = this.#executionId;
		listExecutionSubAgents(this.#workspaceId, executionId, {
			parentSubAgentId: subAgentId
		})
			.then((nodes) => {
				if (this.#executionId !== executionId) return;
				this.knownNodes = mergeNodes(this.knownNodes, nodes);
			})
			.catch(() => undefined);
	}

	childrenOf(subAgentId: string): SubAgentTreeNode[] {
		const found = findNode(this.tree, subAgentId);
		return found?.children ?? [];
	}

	subAgentById(id: string): SubAgentNode | null {
		return findNode(this.tree, id)?.node ?? this.knownNodes.find((n) => n.subAgentId === id) ?? null;
	}

	eventsForSubAgent(id: string): ExecutionEvent[] {
		const cached = this.nodeEventCache[id];
		const attributed = nodeEvents(this.flatEvents, id);
		return cached ? mergeEvents(attributed, cached) : attributed;
	}

	/** Submit operator input to a node that accepts it. */
	async submit(node: SubAgentNode, body: string): Promise<ChatMessage | null> {
		if (!node.acceptsInput || !body.trim()) return null;
		this.busy = true;
		try {
			return await postNodeMessage(
				this.#workspaceId,
				this.#executionId,
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
