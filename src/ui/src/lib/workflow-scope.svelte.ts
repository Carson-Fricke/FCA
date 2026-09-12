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
 * The controller is intentionally host-neutral: the conversation pane and the
 * standalone `/w/{workspace}/workflows/{execution_id}` route both construct
 * one and render the same `WorkflowScope` shell.
 */
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
	ExecutionEvent,
	ExecutionNodeRef,
	GateResolution,
	SubAgentNode,
	WorkflowExecutionRecord,
	WorkflowGate
} from './types';

/** The synthetic node used to view the workflow orchestrator for a phase. */
export const ORCHESTRATOR_ID = 'orchestrator';

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

	constructor(workspaceId: string, hostId: string, cursorStore?: CursorStore) {
		this.#workspaceId = workspaceId;
		this.#hostId = hostId;
		this.#readonlyCursorStore = cursorStore ?? this.#cursorStore;
		this.scope = new ScopeController(scopeStorageKey(workspaceId, hostId));
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
		let segments = parseScope(options.scope);
		if (!segments.some((segment) => segment.kind === 'conversation')) segments = [root, ...segments];
		segments = applyHashTarget(segments, scopeFromHash(options.hash));
		this.scope.reset(segments);

		listNodeEvents(this.#workspaceId, executionId, executionNodeRef(executionId), {
			includeDescendants: false
		})
			.then((events) => {
				this.rootEvents = mergeEvents(this.rootEvents, events);
			})
			.catch((error: unknown) => {
				this.error = error instanceof Error ? error.message : 'Failed to load events';
			});

		listExecutionSubAgents(this.#workspaceId, executionId)
			.then((nodes) => {
				this.knownNodes = mergeNodes(this.knownNodes, nodes);
			})
			.catch(() => undefined);

		this.#subscribe(executionNodeRef(executionId));
		this.restoreActiveSubscription();
	}

	detach(): void {
		for (const stream of this.#subscriptions.values()) stream.stop();
		this.#subscriptions.clear();
		this.#executionId = '';
	}

	/** Enter the workflow root card's current orchestrator. */
	enterWorkflow(): void {
		if (!this.execution) return;
		this.scope.push({
			kind: 'workflow',
			id: this.execution.execution_id,
			label: this.execution.workflow_id || 'Workflow'
		});
	}

	/** Enter the orchestrator for a phase; finished phases are read-only. */
	enterOrchestrator(phase: string, readOnly = false): void {
		this.scope.push({
			kind: 'sub_agent',
			id: ORCHESTRATOR_ID,
			label: phase ? `Orchestrator · ${phase}` : 'Orchestrator',
			role: 'orchestrator',
			phase,
			readOnly
		});
	}

	/** Enter one sub-agent node, subscribing and lazily loading its events. */
	enterSubAgent(node: SubAgentNode): void {
		// Truncate to the node's parent when it is already in scope, so sibling
		// sub-agents switch in place instead of accumulating on the path.
		const parentIndex = node.parentSubAgentId
			? this.scope.segments.findIndex(
					(segment) => segment.kind === 'sub_agent' && segment.id === node.parentSubAgentId
				)
			: this.scope.segments.findIndex((segment) => segment.kind === 'workflow');
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
		if (active?.kind === 'sub_agent' && active.id !== ORCHESTRATOR_ID) {
			this.loadNode(active.id);
		}
	}

	/** Lazy-load a node's own events and direct children. */
	loadNode(subAgentId: string): void {
		if (subAgentId === ORCHESTRATOR_ID) return;
		this.#subscribe(subAgentNodeRef(subAgentId));
		if (this.nodeEventCache[subAgentId]) return;
		listNodeEvents(this.#workspaceId, this.#executionId, subAgentNodeRef(subAgentId), {
			includeDescendants: false
		})
			.then((events) => {
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
		listExecutionSubAgents(this.#workspaceId, this.#executionId, {
			parentSubAgentId: subAgentId
		})
			.then((nodes) => {
				this.knownNodes = mergeNodes(this.knownNodes, nodes);
			})
			.catch(() => undefined);
	}

	childrenOf(subAgentId: string): SubAgentTreeNode[] {
		const found = findNode(this.tree, subAgentId);
		return found?.children ?? [];
	}

	subAgentById(id: string): SubAgentNode | null {
		if (id === ORCHESTRATOR_ID) return this.orchestratorNode();
		return findNode(this.tree, id)?.node ?? this.knownNodes.find((n) => n.subAgentId === id) ?? null;
	}

	eventsForSubAgent(id: string): ExecutionEvent[] {
		if (id === ORCHESTRATOR_ID) return this.rootEvents;
		const cached = this.nodeEventCache[id];
		const attributed = nodeEvents(this.flatEvents, id);
		return cached ? mergeEvents(attributed, cached) : attributed;
	}

	/** The synthetic orchestrator node for the active phase. */
	orchestratorNode(): SubAgentNode {
		const active = this.scope.active;
		const phase = active?.kind === 'sub_agent' ? (active.phase ?? '') : '';
		return {
			subAgentId: ORCHESTRATOR_ID,
			parentSubAgentId: null,
			phase,
			role: 'orchestrator',
			status: this.execution?.state === 'failed' ? 'failed' : this.execution?.state === 'completed' ? 'completed' : 'running',
			context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
			acceptsInput: !active?.readOnly
		};
	}

	/** Submit operator input to a node that accepts it. */
	async submit(node: SubAgentNode, body: string): Promise<void> {
		if (!node.acceptsInput || !body.trim()) return;
		this.busy = true;
		try {
			await postNodeMessage(
				this.#workspaceId,
				this.#executionId,
				this.nodeRefFor(node.subAgentId),
				{ body: body.trim() }
			);
		} catch (error: unknown) {
			this.error = error instanceof Error ? error.message : 'Message failed';
		} finally {
			this.busy = false;
		}
	}

	/** Resume a blocked workflow by posting the structured gate resolution. */
	async resolveGate(gate: WorkflowGate, resolution: GateResolution): Promise<void> {
		this.busy = true;
		try {
			await postNodeMessage(
				this.#workspaceId,
				this.#executionId,
				executionNodeRef(this.#executionId),
				{
					body: resolution.note ?? `${resolution.kind}:${gate.gateId}`,
					metadata: {
						gate_id: gate.gateId,
						gate_resolution: resolution.kind,
						verdict: resolution.verdict ?? null,
						note: resolution.note ?? null
					}
				}
			);
		} catch (error: unknown) {
			this.error = error instanceof Error ? error.message : 'Gate resolution failed';
		} finally {
			this.busy = false;
		}
	}

	private nodeRefFor(subAgentId: string): ExecutionNodeRef {
		return subAgentId === ORCHESTRATOR_ID
			? executionNodeRef(this.#executionId)
			: subAgentNodeRef(subAgentId);
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
