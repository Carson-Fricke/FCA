/** Pure navigation grouping from receipt metadata and the current durable hierarchy.
 * Unattributed evidence stays under Other activity; titles never grant authority.
 */
import type { ConversationOutlineEntryWire } from './generated';
import type { SubAgentNode } from './types';
import type { WorkflowLiveState } from './workflow-live';
import { isWorkflowNodeRole } from './subagent';

type OutlineSource = Omit<ConversationOutlineEntryWire, 'kind'> & { kind: ConversationOutlineEntryWire['kind'] | 'conversation' };
export type OutlineEntry = OutlineSource & { depth: number; parentId: string | null };
export interface OutlineGroup { id: string; title: string; entries: OutlineEntry[] }

export function conversationOutline(entries: ConversationOutlineEntryWire[], workflows: Map<string, WorkflowLiveState>, nodes: SubAgentNode[], chatId: string): OutlineGroup[] {
	const ordered = [...entries].sort((a, b) => a.seq - b.seq);
	const byMessage = new Map(ordered.map((entry) => [entry.messageId, entry]));
	const byNode = new Map(nodes.map((node) => [node.subAgentId, node]));
	const turns = ordered.filter((entry) => entry.kind === 'turn');
	const requestTurn = new Map<string, string>();
	let lastTurn: string | null = null;
	for (const entry of ordered) {
		if (entry.kind === 'turn') lastTurn = entry.id;
		if (entry.kind === 'request' && entry.threadId === 'root' && lastTurn) requestTurn.set(entry.messageId, lastTurn);
	}
	function ownerExecution(thread: string): string | null {
		if (thread === 'root' || thread === chatId) return null;
		return byNode.get(thread)?.workflowExecutionId ?? null;
	}
	function executionTurn(id: string, seen: Set<string>): string | null {
		if (seen.has(`execution:${id}`)) return null;
		seen.add(`execution:${id}`);
		const execution = workflows.get(id)?.execution;
		if (!execution) return null;
		const trigger = byMessage.get(execution.trigger_message_id);
		if (trigger) { const turn = turnFor(trigger, seen); if (turn) return turn; }
		if (requestTurn.has(execution.trigger_message_id)) return requestTurn.get(execution.trigger_message_id)!;
		const owner = ownerExecution(execution.conversation_id ?? 'root');
		if (owner && owner !== id) { const turn = executionTurn(owner, seen); if (turn) return turn; }
		const launch = ordered.find((row) => row.kind === 'workflow' && row.entityId === id);
		return launch ? turnFor(launch, seen) : null;
	}
	function turnFor(entry: OutlineSource, seen = new Set<string>()): string | null {
		if (entry.kind === 'turn') return entry.id;
		if (seen.has(entry.id)) return null;
		seen.add(entry.id);
		const request = entry.requestMessageId;
		if (request && requestTurn.has(request)) return requestTurn.get(request)!;
		if (request && byMessage.has(request)) {
			const turn = turnFor(byMessage.get(request)!, seen);
			if (turn) return turn;
		}
		if (requestTurn.has(entry.messageId)) return requestTurn.get(entry.messageId)!;
		return executionTurn(entry.kind === 'workflow' ? entry.entityId! : ownerExecution(entry.threadId) ?? entry.executionId ?? '', seen);
	}
	const visible = new Map<string, OutlineSource>();
	for (const entry of ordered) {
		if (entry.kind === 'turn' || entry.kind === 'request') continue;
		const key = `${entry.kind}:${entry.entityId ?? entry.id}`;
		const previous = visible.get(key);
		visible.set(key, previous ? { ...previous, status: entry.status ?? previous.status } : entry);
	}
	for (const live of workflows.values()) {
		const execution = live.execution;
		const key = `workflow:${execution.execution_id}`;
		const previous = visible.get(key);
		if (previous) { visible.set(key, { ...previous, status: execution.state }); continue; }
		const trigger = byMessage.get(execution.trigger_message_id);
		visible.set(key, { id: key, kind: 'workflow', title: execution.workflow_id.replace(/[-_]/g, ' '),
			seq: trigger?.seq ?? 0, threadId: execution.conversation_id && execution.conversation_id !== chatId ? execution.conversation_id : 'root',
			messageId: execution.trigger_message_id, datumIndex: null, entityId: execution.execution_id,
			executionId: execution.execution_id, requestMessageId: execution.trigger_message_id, status: execution.state });
	}
	for (const node of nodes) {
		if (isWorkflowNodeRole(node.role)) continue;
		const source = ordered.find((entry) => entry.threadId === node.subAgentId);
		visible.set(`conversation:${node.subAgentId}`, {
			id: `conversation:${node.subAgentId}`, kind: 'conversation', title: node.role.replace(/_/g, ' '),
			seq: source?.seq ?? 0, threadId: node.subAgentId, messageId: '', datumIndex: null,
			entityId: node.subAgentId, executionId: node.workflowExecutionId, requestMessageId: null, status: node.status
		});
	}
	const groups = new Map<string, OutlineGroup>(turns.map((turn) => [turn.id, { id: turn.id, title: turn.title, entries: [] }]));
	for (const entry of visible.values()) {
		const turn = turnFor(entry) ?? 'other';
		if (!groups.has(turn)) groups.set(turn, { id: turn, title: 'Other activity', entries: [] });
		groups.get(turn)!.entries.push({ ...entry, depth: 0, parentId: null });
	}
	for (const group of groups.values()) {
		const workflowEntry = (id: string | null) => group.entries.find((row) => row.kind === 'workflow' && row.entityId === id)?.id ?? null;
		const conversationEntry = (id: string | null) => group.entries.find((row) => row.kind === 'conversation' && row.entityId === id)?.id ?? null;
		function parentOf(entry: OutlineEntry): string | null {
			if (entry.kind === 'conversation') {
				const node = byNode.get(entry.entityId!);
				const parent = node?.parentSubAgentId ? byNode.get(node.parentSubAgentId) : null;
				return parent && isWorkflowNodeRole(parent.role) ? workflowEntry(parent.workflowExecutionId) : conversationEntry(parent?.subAgentId ?? null);
			}
			return conversationEntry(entry.threadId) ?? workflowEntry(ownerExecution(entry.threadId));
		}
		const children = new Map<string | null, OutlineEntry[]>();
		for (const entry of group.entries) {
			const candidate = parentOf(entry);
			const parentId = candidate === entry.id ? null : candidate;
			children.set(parentId, [...(children.get(parentId) ?? []), { ...entry, parentId }]);
		}
		const flattened: OutlineEntry[] = [];
		const visit = (parent: string | null, depth: number) => {
			for (const entry of (children.get(parent) ?? []).sort((a, b) => a.seq - b.seq)) {
				if (flattened.some((row) => row.id === entry.id)) continue;
				flattened.push({ ...entry, depth });
				visit(entry.id, depth + 1);
			}
		};
		visit(null, 0);
		group.entries = flattened;
	}
	return [...groups.values()].filter((group) => group.entries.length);
}
