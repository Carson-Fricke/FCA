/**
 * Unified, chronologically ordered transcript for a nested workflow scope.
 *
 * The chat pane shows a normal transcript for the root agent, but drilling into
 * a phase or sub-agent must show *that* agent's conversation. A sub-agent's
 * history lives in two durable sources: user messages it received (persisted as
 * `kind=sub_agent` chat messages carrying its `subAgentId`) and its own
 * execution events (thinking, tool calls, outputs). This module merges
 * both into a single, time-ordered entry list so a message can never drift out
 * of order relative to the tool renderings around it.
 *
 * The merge is pure and network-free so the ordering contract is unit-testable.
 */

import { isWorkflowNodeRole, parseToolResult, type SubAgentTreeNode } from './subagent';
import type {
	ChatMessage,
	ExecutionEvent,
	SubAgentNode,
	ToolResult
} from './types';

/** The data payload carried by a falsification report artifact. */
export interface FalsificationTrialItem {
	testId: string;
	falsifierId: string;
	observable: string;
	condition: string;
	threshold: number;
	observed: number | null;
	falsified: boolean;
	description?: string;
	detail?: string;
	runId?: string;
	runState?: string;
}

export interface FalsificationControlItem {
	controlId: string;
	description: string;
	expectation?: string;
}

export interface FalsificationReportData {
	type?: string;
	content?: string;
	passedCount: number;
	falsifiedCount: number;
	totalCount: number;
	survived: boolean;
	hypothesis?: {
		claim?: string;
		disagreement?: string;
		universe?: string[];
		horizon?: string;
	};
	trials: FalsificationTrialItem[];
	controls?: FalsificationControlItem[];
	metrics?: Record<string, unknown>;
	distributions?: Record<string, number[]>;
	series?: Array<{ name: string; data: Array<{ t: number; v: number }> }>;
}

/** One renderable transcript entry on a scope's single timeline. */
export type ScopeEntry =
	| { kind: 'message'; id: string; at: string; order: number; message: ChatMessage }
	| { kind: 'tool'; id: string; at: string; order: number; result: ToolResult }
	| {
			kind: 'assistant';
			id: string;
			at: string;
			order: number;
			text: string;
			actorId: string;
	  }
	| { kind: 'sub-agent'; id: string; at: string; order: number; node: SubAgentNode }
	| { kind: 'spawn'; id: string; at: string; order: number; spawn: SpawnDetails }
	| { kind: 'event'; id: string; at: string; order: number; event: ExecutionEvent }
	| { kind: 'falsification'; id: string; at: string; order: number; data: FalsificationReportData };

/** The declared context carried by a sub-agent's initial spawn event. */
export interface SpawnDetails {
	role: string;
	phase: string;
	isolated: boolean;
	knowledgeCutoff: string | null;
	mcpTools: string[];
}

function spawnDetails(event: ExecutionEvent): SpawnDetails {
	const context = field(event, 'context');
	const record = typeof context === 'object' && context !== null ? (context as Record<string, unknown>) : {};
	const mcp = typeof record['mcp'] === 'object' && record['mcp'] !== null
		? (record['mcp'] as Record<string, unknown>)
		: {};
	const tools = Array.isArray(mcp['tools'])
		? mcp['tools'].filter((tool): tool is string => typeof tool === 'string')
		: [];
	const cutoff = record['knowledge_cutoff'];
	return {
		role: stringField(event, 'role') ?? 'sub-agent',
		phase: stringField(event, 'phase') ?? '',
		isolated: record['isolated'] === true,
		knowledgeCutoff: typeof cutoff === 'string' && cutoff.length > 0 ? cutoff : null,
		mcpTools: tools
	};
}

function field(event: ExecutionEvent, key: string): unknown {
	const payload = event.payload ?? {};
	if (key in payload) return payload[key];
	return (event as unknown as Record<string, unknown>)[key];
}

function stringField(event: ExecutionEvent, key: string): string | null {
	const value = field(event, key);
	return typeof value === 'string' && value.length > 0 ? value : null;
}

/** The best-effort human text carried by a sub-agent output/needs-input event. */
export function outputText(event: ExecutionEvent): string {
	const direct = stringField(event, 'text') ?? stringField(event, 'output');
	if (direct) return direct;
	const artifact = field(event, 'artifact');
	if (typeof artifact === 'object' && artifact !== null) {
		const content = (artifact as Record<string, unknown>)['content'];
		if (typeof content === 'string') return content;
		if (content != null) return '```json\n' + JSON.stringify(content, null, 2) + '\n```';
	}
	return stringField(event, 'outputRef') ?? '';
}

/** The spawn timestamp/cursor for one child node, when it has a spawn event. */
function spawnAnchor(node: SubAgentTreeNode): { at: string; order: number } {
	for (const event of node.events) {
		if (event.eventType === 'sub_agent.spawned') {
			return { at: event.occurredAt, order: event.cursor };
		}
	}
	return { at: '', order: Number.MAX_SAFE_INTEGER };
}

function compareEntries(a: ScopeEntry, b: ScopeEntry): number {
	if (a.at !== b.at) return a.at < b.at ? -1 : 1;
	return a.order - b.order;
}

export interface BuildNodeEntriesOptions {
	/** Node-scoped chat messages the agent received (already filtered). */
	messages: ChatMessage[];
	/** Node-scoped execution events (already filtered, global cursors kept). */
	events: ExecutionEvent[];
	/** Direct child sub-agents, rendered as drill-in items at their spawn time. */
	children?: SubAgentTreeNode[];
}

/**
 * Build the merged, chronological transcript for one sub-agent scope.
 *
 * Lifecycle spawn events are represented by the child card instead of a raw
 * event, so no item renders twice.
 */
export function buildNodeEntries({
	messages,
	events,
	children = []
}: BuildNodeEntriesOptions): ScopeEntry[] {
	const entries: ScopeEntry[] = [];

	messages.forEach((message, index) => {
		entries.push({
			kind: 'message',
			id: message.message_id,
			at: message.created_at,
			order: index,
			message
		});
	});

	for (const event of events) {
		const base = { id: event.eventId, at: event.occurredAt, order: event.cursor } as const;
		if (event.eventType === 'sub_agent.spawned') {
			entries.push({ kind: 'spawn', ...base, spawn: spawnDetails(event) });
			continue;
		}
		// Engine activity labels and the needs-input pause signal are
		// lifecycle wiring (node status, liveness), not conversation content:
		// they never render. Tool progress still falls through to the plain
		// event line.
		if (
			event.eventType === 'sub_agent.needs_input' ||
			(event.eventType === 'sub_agent.progress' &&
				stringField(event, 'kind') !== 'tool')
		) {
			continue;
		}
		const result = parseToolResult(event);
		if (result) {
			entries.push({ kind: 'tool', ...base, result });
			continue;
		}
		if (event.eventType === 'sub_agent.output') {
			const artifact = field(event, 'artifact') as Record<string, unknown> | null;
			if (
				artifact &&
				typeof artifact === 'object' &&
				(artifact['type'] === 'falsification_report' ||
					artifact['type'] === 'falsification_trials' ||
					(Array.isArray(artifact['trials']) && artifact['trials'].length > 0))
			) {
				entries.push({
					kind: 'falsification',
					...base,
					data: artifact as unknown as FalsificationReportData
				});
				continue;
			}
			const text = outputText(event);
			if (text) {
				entries.push({
					kind: 'assistant',
					...base,
					text,
					actorId: event.actorId
				});
				continue;
			}
		}
		entries.push({ kind: 'event', ...base, event });
	}

	for (const child of children) {
		if (isWorkflowNodeRole(child.node.role)) continue;
		const anchor = spawnAnchor(child);
		entries.push({
			kind: 'sub-agent',
			id: `sub-agent:${child.node.subAgentId}`,
			at: anchor.at,
			order: anchor.order,
			node: child.node
		});
	}

	return entries.sort(compareEntries);
}
