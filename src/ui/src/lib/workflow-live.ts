/** Pure, execution-indexed presentation of the single canonical chat timeline. */
import { derivePhases, isWorkflowPhase, projectExecution, type SubAgentTreeNode } from './subagent';
import { canonicalPhaseName } from './workflow-graph';
import type { ChatMessage, ExecutionEvent, WorkflowExecutionRecord, WorkflowPhase } from './types';

export interface WorkflowLiveState {
	execution: WorkflowExecutionRecord;
	phases: WorkflowPhase[];
	attentionPhases: string[];
	anchored: boolean;
}

function workflowAnchor(message: ChatMessage, data: Record<string, unknown>): WorkflowExecutionRecord | null {
	const id = data.executionId ?? data.execution_id ?? message.execution_id;
	if (typeof id !== 'string' || !id) return null;
	return {
		execution_id: id, chat_id: message.chat_id, trigger_message_id: message.message_id,
		workflow_id: String(data.workflowId ?? data.workflow_id ?? ''),
		workflow_version: String(data.version ?? data.workflowVersion ?? data.workflow_version ?? ''),
		requested_by: message.actor_id, state: (data.state ?? data.status ?? 'running') as WorkflowExecutionRecord['state'],
		phase: data.currentPhase == null && data.phase == null ? null : String(data.currentPhase ?? data.phase),
		intent_epoch: message.intent_epoch, steering_message_ids: [], created_at: message.created_at
	};
}

export function deriveWorkflowLive(
	messages: ChatMessage[], eventsByExecution: Map<string, ExecutionEvent[]>,
	snapshots: Map<string, { execution: WorkflowExecutionRecord; seq: number }> = new Map()
): Map<string, WorkflowLiveState> {
	const anchors = new Map<string, { execution: WorkflowExecutionRecord; phases: WorkflowPhase[] }>();
	for (const message of messages) {
		const datums = message.data?.sessionMessage?.content.result?.datums;
		const sources = message.kind === 'workflow' ? [message.data ?? {}] :
			Array.isArray(datums) ? datums.flatMap((datum) => datum && typeof datum === 'object' && datum.datumType === 'workflow_execution' ? [datum.structured] : []) :
			message.tool_result?.datumType === 'workflow_execution' ? [message.tool_result.structured] : [];
		for (const data of sources) {
			if (!data || typeof data !== 'object' || Array.isArray(data)) continue;
			const execution = workflowAnchor(message, data);
			if (!execution || anchors.has(execution.execution_id)) continue;
			const phases: WorkflowPhase[] = Array.isArray(data.phases) ? data.phases.flatMap((item) => {
				if (typeof item === 'string') return [{ name: item, status: 'pending' as const }];
				if (!item || typeof item !== 'object' || typeof item.name !== 'string') return [];
				return [{ name: item.name, status: item.status === 'done' || item.status === 'active' ? item.status : 'pending' } satisfies WorkflowPhase];
			}) : [];
			anchors.set(execution.execution_id, { execution, phases });
		}
	}
	const result = new Map<string, WorkflowLiveState>();
	for (const id of new Set([...anchors.keys(), ...snapshots.keys()])) {
		const anchor = anchors.get(id);
		const snapshot = snapshots.get(id);
		const execution = snapshot?.execution ?? anchor!.execution;
		const events = eventsByExecution.get(id) ?? [];
		// First-save snapshots may arrive late; every lifecycle event remains evidence.
		const projected = projectExecution(execution, events);
		const phases = new Map((anchor?.phases ?? []).filter((phase) => isWorkflowPhase(phase.name)).map((phase) => [canonicalPhaseName(phase.name), { ...phase }]));
		const phaseEvents = events.filter((event) => event.eventType !== 'sub_agent.spawned' ||
			(event.payload.phaseExecutor === true || event.payload.phase_executor === true));
		for (const phase of derivePhases(phaseEvents)) if (isWorkflowPhase(phase.name)) phases.set(canonicalPhaseName(phase.name), phase);
		if (projected.phase) {
			for (const [key, phase] of phases) {
				if (phase.status === 'active' && key !== canonicalPhaseName(projected.phase)) phase.status = 'done';
			}
			const key = canonicalPhaseName(projected.phase);
			phases.set(key, { ...phases.get(key), name: projected.phase, status: 'active' });
		}
		if (projected.state === 'completed') for (const phase of phases.values()) phase.status = 'done';
		result.set(id, { execution: projected, phases: [...phases.values()], attentionPhases: [], anchored: !!anchor });
	}
	return result;
}

/** Walk the tree once, never once per card. Helpers never own phase steps. */
export function withWorkflowNodes(workflows: Map<string, WorkflowLiveState>, tree: SubAgentTreeNode[]): Map<string, WorkflowLiveState> {
	const nodes = new Map<string, Map<string, { item: SubAgentTreeNode; order: number }>>();
	const attention = new Map<string, Set<string>>();
	const visit = (items: SubAgentTreeNode[]): void => {
		for (const item of items) {
			const { workflowExecutionId: id, phase, status } = item.node;
			if (id && isWorkflowPhase(phase) && item.node.phaseExecutor === true) {
				const bucket = nodes.get(id) ?? new Map();
				const key = canonicalPhaseName(phase);
				const spawn = item.events.find((event) => event.eventType === 'sub_agent.spawned');
				const order = spawn?.seq ?? spawn?.cursor ?? Number.MAX_SAFE_INTEGER;
				if (!bucket.has(key) || order < bucket.get(key)!.order) bucket.set(key, { item, order });
				nodes.set(id, bucket);
				if (status === 'needs_input') {
					const waiting = attention.get(id) ?? new Set();
					waiting.add(phase);
					attention.set(id, waiting);
				}
			}
			visit(item.children);
		}
	};
	visit(tree);
	const result = new Map<string, WorkflowLiveState>();
	for (const [id, live] of workflows) {
		const phases = new Map(live.phases.map((phase) => [canonicalPhaseName(phase.name), phase]));
		for (const [key, { item }] of nodes.get(id) ?? []) {
			const previous = phases.get(key);
			phases.set(key, { name: previous?.name ?? item.node.phase,
				status: live.execution.state === 'completed' || item.node.status === 'completed' ? 'done' : 'active', attempts: previous?.attempts });
		}
		result.set(id, { ...live, phases: [...phases.values()], attentionPhases: [...attention.get(id) ?? []] });
	}
	return result;
}

export function hasActiveWorkflow(workflows: Map<string, WorkflowLiveState>): boolean {
	return [...workflows.values()].some(({ execution }) => ['queued', 'running'].includes(execution.state));
}

export function isTerminalWorkflow(execution: WorkflowExecutionRecord | null | undefined): boolean {
	return !!execution && ['completed', 'failed', 'cancelled', 'stopped'].includes(execution.state);
}
