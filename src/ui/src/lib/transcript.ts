/**
 * Shared root/scoped activity projection. Unified seq is retained throughout;
 * array position/cursor supports historical callers without unified envelopes.
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
	isolated: boolean | null;
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
		isolated: typeof record['isolated'] === 'boolean' ? record['isolated'] : null,
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
			return { at: event.occurredAt, order: event.seq ?? event.cursor };
		}
	}
	return { at: '', order: Number.MAX_SAFE_INTEGER };
}

function compareEntries(a: ScopeEntry, b: ScopeEntry): number {
	return a.order - b.order;
}

/** The canonical result payload of one durable session message. */
function canonicalResult(message: ChatMessage): Record<string, unknown> | null {
	const result = message.data?.sessionMessage?.content.result;
	return result && typeof result === 'object' && !Array.isArray(result) ? result : null;
}

/** The canonical call identity: model correlation first, runtime result second. */
function canonicalCallId(message: ChatMessage): string | null {
	const correlation = message.data?.sessionMessage?.content.correlation;
	if (correlation?.request_id) return correlation.request_id;
	const runtime = canonicalResult(message)?.toolCallId;
	return typeof runtime === 'string' ? runtime : null;
}

export interface BuildNodeEntriesOptions {
	/** Node-scoped chat messages the agent received (already filtered). */
	messages: ChatMessage[];
	/** Node-scoped execution events (already filtered, global cursors kept). */
	events: ExecutionEvent[];
	/** Direct child sub-agents, rendered as drill-in items at their spawn time. */
	children?: SubAgentTreeNode[];
	terminal?: boolean;
}

/** Project durable envelopes and runtime starts into activity, retaining assistant bodies. */
export function projectToolMessages(messages: ChatMessage[], terminal = false): ChatMessage[] {
	const unique = [...new Map(messages.map((message) => [message.message_id, message])).values()]
		.sort((a, b) => (a.seq ?? messages.indexOf(a)) - (b.seq ?? messages.indexOf(b)));
	const order = new Map(unique.map((message, index) => [message.message_id, message.seq ?? index]));
	const calls = new Map<string, { message: ChatMessage; tool: string; arguments: unknown }>();
	for (const message of unique) {
		if (message.role === 'tool' && message.data?.toolActivity === 'started' &&
			typeof message.data.toolCallId === 'string' && typeof message.data.tool === 'string') {
			calls.set(message.data.toolCallId, { message, tool: message.data.tool, arguments: message.data.arguments });
			continue;
		}
		if (message.role !== 'assistant') continue;
		for (const request of message.data?.sessionMessage?.content.requests ?? []) {
			calls.set(request.id, { message, tool: request.name, arguments: request.arguments });
		}
	}
	const completed = new Set<string>();
	const projected: ChatMessage[] = [];
	for (const [index, message] of unique.entries()) {
		if (message.role === 'tool' && message.data?.toolActivity === 'started') continue;
		const canonical = canonicalResult(message);
		const correlation = message.data?.sessionMessage?.content.correlation;
		const callId = message.tool_result?.callId ?? canonicalCallId(message);
		const datums = canonical?.datums;
		if (Array.isArray(datums)) {
			if (callId) completed.add(callId);
			const call = callId ? calls.get(callId) : undefined;
			// These identities belong only to rendering; the aggregate remains one durable arrival.
			for (const [datumIndex, datum] of datums.entries()) {
				const card = datum as ToolResult;
				projected.push({ ...message, seq: order.get(message.message_id),
					message_id: `${message.message_id}:datum:${datumIndex}`, role: 'tool',
					tool_result: { ...card, callId,
						tool: message.tool_result?.tool ?? correlation?.tool_name ?? call?.tool ?? null,
						arguments: canonical?.arguments ?? call?.arguments,
						startedAt: typeof canonical?.startedAt === 'string' ? canonical.startedAt :
							typeof call?.message.data?.startedAt === 'string' ? call.message.data.startedAt : undefined,
						durationMs: typeof canonical?.durationMs === 'number' ? canonical.durationMs : undefined,
						requestState: card.datumType === 'operator_question'
							? unique.slice(index + 1).some((later) => later.role === 'user') ? 'answered' : terminal ? 'resolved' : 'waiting'
							: undefined } });
			}
			continue;
		}
		if (message.tool_result && callId) completed.add(callId);
		if (message.role !== 'tool') {
			const failure = message.data?.failure;
			if (message.role === 'system' && failure && typeof failure === 'object' && !Array.isArray(failure)) {
				const detail = failure as Record<string, unknown>;
				projected.push({ ...message, tool_result: message.tool_result ?? {
					tool: 'runtime', callId: null, ok: false, summary: message.body,
					structured: detail, text: '', failure: {
						kind: typeof detail.kind === 'string' ? detail.kind : 'runtime',
						message: typeof detail.message === 'string' ? detail.message : message.body
					}
				} });
			} else {
				projected.push(message);
			}
			continue;
		}
		if (callId) completed.add(callId);
		let result = message.tool_result;
		if (result) {
			const startedAt = callId ? calls.get(callId)?.message.data?.startedAt : undefined;
			result = {
				...result,
				callId: callId ?? null,
				arguments: canonical?.arguments ?? result.arguments ?? (callId ? calls.get(callId)?.arguments : undefined),
				durationMs: typeof canonical?.durationMs === 'number' ? canonical.durationMs : result.durationMs,
				startedAt: typeof canonical?.startedAt === 'string' ? canonical.startedAt :
					result.startedAt ?? (typeof startedAt === 'string' ? startedAt : undefined)
			};
			if (result.datumType === 'operator_question') {
				const answered = unique.slice(index + 1).some((later) => later.role === 'user');
				result.requestState = result.requestState ?? (answered ? 'answered' : terminal ? 'resolved' : 'waiting');
			}
		}
		projected.push({ ...message, tool_result: result });
	}
	for (const [callId, call] of calls) {
		if (completed.has(callId)) continue;
		const callIndex = unique.indexOf(call.message);
		const notice = unique.slice(callIndex + 1).find((message) => message.role === 'system' &&
			['interrupted', 'cancelled', 'failed', 'completed'].includes(String(message.data?.status)));
		const outcome = notice?.data?.status === 'interrupted' ? 'interrupted' : terminal || notice ? 'unknown_outcome' : undefined;
		projected.push({
			...call.message, seq: order.get(call.message.message_id), message_id: call.message.role === 'tool' ? call.message.message_id : `pending:${callId}`, role: 'tool', body: '',
			tool_result: { tool: call.tool, callId, pending: !outcome, outcome, arguments: call.arguments,
				startedAt: typeof call.message.data?.startedAt === 'string' ? call.message.data.startedAt : undefined,
				ok: false, summary: '', structured: {}, text: '', failure: outcome ? {
					kind: outcome,
					message: 'No final tool result was recorded. The execution outcome is unknown.'
				} : null }
		});
	}
	return projected.sort((a, b) => (a.seq ?? order.get(a.message_id) ?? 0) - (b.seq ?? order.get(b.message_id) ?? 0));
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
	children = [],
	terminal = false
}: BuildNodeEntriesOptions): ScopeEntry[] {
	const entries: ScopeEntry[] = [];
	const resultIds = new Set(messages.flatMap((message) => {
		const id = message.tool_result?.callId ?? canonicalCallId(message);
		return (message.tool_result || Array.isArray(canonicalResult(message)?.datums) || message.role === 'tool') &&
			message.data?.toolActivity !== 'started' && typeof id === 'string' ? [id] : [];
	}));
	const uniqueEvents = [...new Map(events.map((event) => [event.eventId, event])).values()];
	const projectedMessages = projectToolMessages(messages, terminal);
	const eventResults = uniqueEvents.flatMap((event) => {
		const result = parseToolResult(event);
		const messageId = field(event, 'messageId') ?? field(event, 'message_id');
		if (!result || messages.some((message) => message.message_id === messageId) || projectedMessages.some((message) =>
			(message.tool_result && result.callId && message.tool_result.callId === result.callId &&
				message.tool_result.ok === result.ok && JSON.stringify(message.tool_result.failure) === JSON.stringify(result.failure) &&
				message.tool_result.datumType === result.datumType && message.tool_result.summary === result.summary &&
				JSON.stringify(message.tool_result.structured) === JSON.stringify(result.structured) && message.tool_result.text === result.text))) return [];
		return [{ event, result }];
	});
	const eventCallIds = new Set(eventResults.flatMap(({ result }) => result.callId ? [result.callId] : []));
	for (const id of eventCallIds) resultIds.add(id);
	const pendingArguments = new Map(projectedMessages.flatMap((message) => (message.tool_result?.pending || message.tool_result?.outcome) && message.tool_result
		? [[message.tool_result.callId, message.tool_result.arguments] as const] : []));

	projectedMessages.forEach((message, index) => {
		if ((message.tool_result?.pending || message.tool_result?.outcome) && eventCallIds.has(message.tool_result.callId!)) return;
		entries.push({
			kind: 'message',
			id: message.message_id,
			at: message.created_at,
		order: message.seq ?? index,
			message
		});
	});

	for (const event of uniqueEvents) {
		const messageId = field(event, 'messageId') ?? field(event, 'message_id');
		if (typeof messageId === 'string' && messages.some((message) => message.message_id === messageId)) continue;
		const base = { id: event.eventId, at: event.occurredAt, order: event.seq ?? event.cursor } as const;
		if (event.eventType === 'sub_agent.spawned') {
			if (children.some((child) => child.node.subAgentId === field(event, 'subAgentId'))) continue;
			entries.push({ kind: 'spawn', ...base, spawn: spawnDetails(event) });
			continue;
		}
		const result = parseToolResult(event);
		if (result) {
			if (eventResults.some((item) => item.event === event)) {
				const answered = messages.some((message) => message.role === 'user' && (message.seq ?? -1) > (event.seq ?? event.cursor));
				entries.push({ kind: 'tool', ...base, result: {
					...result,
					arguments: result.arguments ?? pendingArguments.get(result.callId),
					requestState: result.requestState ?? (result.datumType === 'operator_question'
						? answered ? 'answered' : terminal ? 'resolved' : 'waiting' : undefined)
				} });
			}
			continue;
		}
		// The request card owns the pause signal; retain all other activity.
		if (event.eventType === 'sub_agent.needs_input' && !outputText(event)) continue;
		if (event.eventType === 'tool.started' && typeof field(event, 'toolCallId') === 'string' &&
			(resultIds.has(field(event, 'toolCallId') as string) ||
				pendingArguments.has(field(event, 'toolCallId') as string))) continue;
		if (event.eventType === 'sub_agent.output' || event.eventType === 'sub_agent.needs_input') {
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

/** Pending and delivery feedback use the same historical/live activity as rendering. */
export function threadActivity(messages: ChatMessage[], events: ExecutionEvent[], terminal = false, acceptedId?: string) {
	const entries = buildNodeEntries({ messages, events, terminal });
	const latest = entries.findLast((entry) => entry.kind === 'assistant' ||
		(entry.kind === 'message' && (entry.message.role === 'user' ||
			(entry.message.role === 'assistant' && !!entry.message.body &&
				!entry.message.data?.sessionMessage?.content.requests?.length))));
	const accepted = entries.find((entry) => entry.id === acceptedId);
	const replied = accepted && entries.some((entry) => entry.order > accepted.order &&
		(entry.kind === 'assistant' || (entry.kind === 'message' && entry.message.role === 'assistant' &&
			!!entry.message.body && !entry.message.data?.sessionMessage?.content.requests?.length)));
	return {
		pending: !terminal && ((latest?.kind === 'message' && latest.message.role === 'user') ||
			entries.some((entry) => entry.kind === 'message' && entry.message.tool_result?.pending)),
		queued: !terminal && !!acceptedId && !replied
	};
}
