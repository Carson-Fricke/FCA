/**
 * Client-side derivation of the nested sub-agent tree from the durable
 * execution event stream.
 *
 * The backend publishes `sub_agent.*` and `workflow.gate.*` events whose
 * payloads carry the node identity (`subAgentId`, `parentSubAgentId`,
 * `phase`, `role`), the sandbox `context`, and whether the node
 * `acceptsInput`. The UI never invents nodes: every node here is sourced from
 * at least one event (or an explicit REST `listChatSubAgents` row).
 *
 * These functions are pure so the transcript and tree rendering are testable
 * with mocked event streams, independent of the network.
 */
import type {
	ExecutionEvent,
	ExecutionNodeRef,
	SubAgentContext,
	SubAgentNode,
	SubAgentStatus,
	ToolResult,
	TranscriptItem,
	WorkflowExecutionRecord,
	WorkflowGate,
	WorkflowPhase
} from './types';

/** A node with its own events and its derived children. */
export interface SubAgentTreeNode {
	node: SubAgentNode;
	events: ExecutionEvent[];
	children: SubAgentTreeNode[];
}

/**
 * Reserved durable role of the workflow execution's own node record. The node
 * exists to own the execution and apply guards; it is an implementation record,
 * never a user-facing agent, so the UI hides it everywhere.
 */
export const WORKFLOW_NODE_ROLE = 'workflow-node';

/** Whether a node role names the ephemeral workflow-node implementation record. */
export function isWorkflowNodeRole(role: string | null | undefined): boolean {
	return role === WORKFLOW_NODE_ROLE;
}

/** Read a payload field, falling back to the event's top level. */
function field(event: ExecutionEvent, key: string): unknown {
	const payload = event.payload ?? {};
	if (key in payload) return payload[key];
	return (event as unknown as Record<string, unknown>)[key];
}

function stringField(event: ExecutionEvent, key: string): string | null {
	const value = field(event, key);
	return typeof value === 'string' && value.length > 0 ? value : null;
}

function booleanField(event: ExecutionEvent, key: string): boolean | null {
	const value = field(event, key);
	return typeof value === 'boolean' ? value : null;
}

function recordField(event: ExecutionEvent, key: string): Record<string, unknown> {
	const value = field(event, key);
	return typeof value === 'object' && value !== null && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: {};
}

/** Whether an event type belongs to the nested sub-agent lifecycle. */
export function isSubAgentEvent(type: string): boolean {
	return type.startsWith('sub_agent.');
}

/** The sub-agent that produced an event, if any. */
export function eventSubAgentId(event: ExecutionEvent): string | null {
	return stringField(event, 'subAgentId');
}

function statusFor(eventType: string): SubAgentStatus {
	switch (eventType) {
		case 'sub_agent.spawned':
			return 'spawned';
		case 'sub_agent.needs_input':
			return 'needs_input';
		case 'sub_agent.completed':
			return 'completed';
		case 'sub_agent.failed':
			return 'failed';
		case 'sub_agent.progress':
		case 'sub_agent.output':
			return 'progress';
		default:
			return 'running';
	}
}

function contextFor(event: ExecutionEvent): SubAgentContext {
	const raw = recordField(event, 'context');
	const mcp = typeof raw['mcp'] === 'object' && raw['mcp'] !== null
		? (raw['mcp'] as Record<string, unknown>)
		: {};
	const tools = Array.isArray(mcp['tools'])
		? (mcp['tools'] as unknown[]).filter((tool): tool is string => typeof tool === 'string')
		: [];
	const cutoff = raw['knowledge_cutoff'];
	return {
		isolated: raw['isolated'] === true,
		knowledge_cutoff: typeof cutoff === 'string' && cutoff.length > 0 ? cutoff : null,
		mcp: { enabled: mcp['enabled'] === true, tools }
	};
}

function nodeFrom(
	event: ExecutionEvent,
	previous?: SubAgentNode
): SubAgentNode | null {
	const subAgentId = stringField(event, 'subAgentId');
	if (!subAgentId) return null;
	const status = statusFor(event.eventType);
	const context = contextFor(event);
	return {
		subAgentId,
		parentSubAgentId: stringField(event, 'parentSubAgentId') ?? previous?.parentSubAgentId ?? null,
		phase: stringField(event, 'phase') ?? previous?.phase ?? '',
		role: stringField(event, 'role') ?? previous?.role ?? 'sub-agent',
		status: status === 'running' && previous ? previous.status : status,
		context: hasContext(event) ? context : (previous?.context ?? context),
		acceptsInput:
			booleanField(event, 'acceptsInput') ?? previous?.acceptsInput ?? false,
		workflowExecutionId: previous?.workflowExecutionId ?? event.executionId
	};
}

function hasContext(event: ExecutionEvent): boolean {
	const value = field(event, 'context');
	return typeof value === 'object' && value !== null;
}

/**
 * Derive the sub-agent tree from a flat event stream. Nodes are created on
 * their first `sub_agent.*` event and updated by later events. Events that
 * carry a `subAgentId` are attributed to that node even when they are not
 * `sub_agent.*` (for example a tool result produced inside the node).
 *
 * `known` seeds nodes from `listChatSubAgents` so a freshly listed node
 * renders before its spawn event replays.
 *
 * A `role: "workflow-node"` record is an execution implementation detail, not
 * a user-facing agent. It is dropped from the tree, and its children (the
 * workflow's phase agents) are re-parented to the node's own parent — the
 * anchor that launched the workflow — so phase navigation and back always land
 * on a real agent.
 */
export function deriveSubAgentTree(
	events: ExecutionEvent[],
	known: SubAgentNode[] = []
): SubAgentTreeNode[] {
	const nodes = new Map<string, SubAgentNode>();
	const attributed = new Map<string, ExecutionEvent[]>();
	const hidden = new Set<string>();

	for (const node of known) {
		if (isWorkflowNodeRole(node.role)) hidden.add(node.subAgentId);
		nodes.set(node.subAgentId, { ...node });
	}

	const ordered = [...events].sort((a, b) => a.cursor - b.cursor);
	for (const event of ordered) {
		const subAgentId = eventSubAgentId(event);
		if (!subAgentId) continue;
		if (isWorkflowNodeRole(stringField(event, 'role'))) hidden.add(subAgentId);
		if (isSubAgentEvent(event.eventType)) {
			const next = nodeFrom(event, nodes.get(subAgentId));
			if (next) nodes.set(subAgentId, next);
		} else if (!nodes.has(subAgentId)) {
			// A non-lifecycle event referencing an unknown node still proves it
			// exists; synthesize a minimal node so the event is not orphaned.
			nodes.set(subAgentId, {
				subAgentId,
				parentSubAgentId: stringField(event, 'parentSubAgentId'),
				phase: stringField(event, 'phase') ?? '',
				role: stringField(event, 'role') ?? 'sub-agent',
				status: 'running',
				context: contextFor(event),
				acceptsInput: booleanField(event, 'acceptsInput') ?? false,
				workflowExecutionId: event.executionId
			});
		}
		const bucket = attributed.get(subAgentId) ?? [];
		bucket.push(event);
		attributed.set(subAgentId, bucket);
	}

	// Re-anchor every node whose parent is a hidden implementation record to the
	// nearest visible ancestor, then remove the hidden records entirely.
	for (const [id, node] of nodes) {
		let parent = node.parentSubAgentId;
		const seen = new Set<string>([id]);
		while (parent && hidden.has(parent) && !seen.has(parent)) {
			seen.add(parent);
			parent = nodes.get(parent)?.parentSubAgentId ?? null;
		}
		if (parent !== node.parentSubAgentId) {
			nodes.set(id, { ...node, parentSubAgentId: parent });
		}
	}
	for (const id of hidden) nodes.delete(id);

	const treeNodes = new Map<string, SubAgentTreeNode>();
	for (const node of nodes.values()) {
		treeNodes.set(node.subAgentId, {
			node,
			events: attributed.get(node.subAgentId) ?? [],
			children: []
		});
	}
	const roots: SubAgentTreeNode[] = [];
	for (const treeNode of treeNodes.values()) {
		const parentId = treeNode.node.parentSubAgentId;
		const parent = parentId ? treeNodes.get(parentId) : undefined;
		if (parent) parent.children.push(treeNode);
		else roots.push(treeNode);
	}
	return roots;
}

/** The events attributed to one node from a flat stream. */
export function nodeEvents(events: ExecutionEvent[], subAgentId: string): ExecutionEvent[] {
	return events.filter((event) => eventSubAgentId(event) === subAgentId);
}

/** Merge two event pages, deduping by event id and ordering by cursor. */
export function mergeEvents(
	existing: ExecutionEvent[],
	incoming: ExecutionEvent[]
): ExecutionEvent[] {
	const byId = new Map<string, ExecutionEvent>();
	for (const event of existing) byId.set(event.eventId, event);
	for (const event of incoming) byId.set(event.eventId, event);
	return [...byId.values()].sort((a, b) => a.cursor - b.cursor);
}

/** Events belonging to one execution in a chat-wide timeline. */
export function executionEvents(events: ExecutionEvent[], executionId: string): ExecutionEvent[] {
	return events.filter((event) => event.executionId === executionId);
}

/** Project the latest durable lifecycle event onto an execution snapshot. */
export function projectExecution(
	execution: WorkflowExecutionRecord,
	events: ExecutionEvent[]
): WorkflowExecutionRecord {
	let state = execution.state;
	let phase = execution.phase;
	for (const event of [...events].sort((a, b) => a.cursor - b.cursor)) {
		const eventPhase = event.payload?.phase;
		if (typeof eventPhase === 'string' && eventPhase.length > 0) phase = eventPhase;
		switch (event.eventType) {
			case 'workflow.started':
				state = 'running';
				break;
			case 'workflow.paused':
				state = 'paused';
				break;
			case 'workflow.stopped':
				state = 'stopped';
				break;
			case 'workflow.cancelled':
				state = 'cancelled';
				break;
			case 'workflow.completed':
				state = 'completed';
				break;
			case 'workflow.failed':
				state = 'failed';
				break;
		}
	}
	return { ...execution, state, phase };
}

/** Decode a `tool.result` payload into a `ToolResult`, when present. */
export function parseToolResult(event: ExecutionEvent): ToolResult | null {
	if (!event.eventType.startsWith('tool.') && !event.eventType.startsWith('sub_agent.')) {
		return null;
	}
	const raw = field(event, 'toolResult') ?? field(event, 'tool_result');
	if (typeof raw !== 'object' || raw === null) return null;
	return raw as ToolResult;
}

/** Decode the model reasoning trace carried by an event, when present. */
export function parseReasoning(event: ExecutionEvent): string | null {
	const value = field(event, 'reasoning') ?? field(event, 'thinking');
	return typeof value === 'string' && value.length > 0 ? value : null;
}

interface GateDraft {
	gateId: string;
	executionId: string;
	phase: string;
	status: 'blocked' | 'resolved';
	reason: string;
	resolution: WorkflowGate['resolution'];
}

/** Derive gate cards (blocked and resolved) from the event stream. Deprecated: gates are replaced by conversational needs_input. */
export function deriveGates(_events: ExecutionEvent[]): WorkflowGate[] {
	return [];
}

/**
 * Derive the phase stepper from workflow lifecycle events. Phases appear in
 * declared order when available; the current phase is active, earlier phases
 * are done, and future phases remain pending. Completion marks every phase done.
 */
export function derivePhases(events: ExecutionEvent[]): WorkflowPhase[] {
	let order: string[] = [];
	let current: string | null = null;
	let completed = false;
	let started = false;
	const visited = new Set<string>();
	const visits = new Map<string, number>();
	for (const event of [...events].sort((a, b) => a.cursor - b.cursor)) {
		if (event.eventType === 'workflow.launched') {
			const workflow = recordField(event, 'workflow');
			const declared = workflow['phases'];
			if (Array.isArray(declared)) {
				order = declared
					.filter((phase): phase is string => typeof phase === 'string')
					.filter((phase) => !['requested', 'completed', 'failed', 'paused'].includes(phase));
			}
		}
		if (event.eventType === 'workflow.completed') {
			completed = true;
			continue;
		}
		if (event.eventType === 'sub_agent.spawned') {
			const phase = stringField(event, 'phase');
			if (phase && !order.includes(phase)) order.push(phase);
			if (phase) {
				visited.add(phase);
				if (!visits.has(phase)) visits.set(phase, 1);
				current = phase;
			}
			continue;
		}
		if (!event.eventType.startsWith('workflow.')) continue;
		const phase = stringField(event, 'phase');
		if (phase && !['requested', 'completed', 'failed', 'paused'].includes(phase)) {
			if (!order.includes(phase)) order.push(phase);
			visited.add(phase);
			visits.set(phase, (visits.get(phase) ?? 0) + 1);
			current = phase;
		}
		if (event.eventType === 'workflow.started') started = true;
	}
	if (!started && current === null) return [];
	if (completed) current = null;
	return order.map((name) => ({
		name,
		attempts: visits.get(name) ?? 0,
		status: name === current
				? 'active'
				: visited.has(name)
					? 'done'
					: 'pending'
	}));
}

/**
 * Derive the phase stepper from the phase orchestrator sub-agents.
 *
 * A workflow's phases are the sub-agents the hidden workflow node spawns. The
 * derivation walks the whole tree (phase agents may be re-parented under the
 * launching anchor) and gives each phase the status of its earliest agent, so a
 * helper spawned inside a phase can never shadow the phase agent's own node.
 * Passing an `executionId` keeps the stepper scoped to one execution's phases.
 */
export function derivePhaseSteps(
	nodes: SubAgentTreeNode[],
	executionId?: string | null
): WorkflowPhase[] {
	const collected: { phase: string; status: SubAgentStatus; cursor: number }[] = [];
	const visit = (list: SubAgentTreeNode[]): void => {
		for (const node of list) {
			if (
				!isWorkflowNodeRole(node.node.role) &&
				node.node.phase.length > 0 &&
				(!executionId || node.node.workflowExecutionId === executionId)
			) {
				const spawn = node.events.find((event) => event.eventType === 'sub_agent.spawned');
				collected.push({
					phase: node.node.phase,
					status: node.node.status,
					cursor: spawn?.cursor ?? Number.MAX_SAFE_INTEGER
				});
			}
			visit(node.children);
		}
	};
	visit(nodes);
	collected.sort((a, b) => a.cursor - b.cursor);
	const byPhase = new Map<string, WorkflowPhase>();
	for (const item of collected) {
		if (byPhase.has(item.phase)) continue;
		byPhase.set(item.phase, {
			name: item.phase,
			status: item.status === 'completed' ? 'done' : 'active'
		});
	}
	return [...byPhase.values()];
}

/**
 * Project a flat event stream into transcript items on the `kind`
 * discriminator. Used by the scoped node transcript; unknown lifecycle
 * events fall back to `workflow` and plain records to `message`.
 */
export function buildTranscriptItems(events: ExecutionEvent[]): TranscriptItem[] {
	return [...events]
		.sort((a, b) => a.cursor - b.cursor)
		.map((event) => {
			if (event.eventType.startsWith('tool.')) {
				return { id: event.eventId, kind: 'tool', data: { event } } satisfies TranscriptItem;
			}
			if (event.eventType.startsWith('sub_agent.')) {
				return { id: event.eventId, kind: 'sub_agent', data: { event } } satisfies TranscriptItem;
			}
			if (event.eventType.startsWith('workflow.')) {
				return { id: event.eventId, kind: 'workflow', data: { event } } satisfies TranscriptItem;
			}
			return { id: event.eventId, kind: 'message', data: { event } } satisfies TranscriptItem;
		});
}

/** The node reference for a sub-agent id. */
export function subAgentNodeRef(id: string): ExecutionNodeRef {
	return { kind: 'sub_agent', id };
}

/** The root execution node reference. */
export function executionNodeRef(id: string): ExecutionNodeRef {
	return { kind: 'execution', id };
}
