import { receiptFor } from './transcript';
/** Pure, execution-indexed presentation of the single canonical chat timeline. */
import { derivePhases, isWorkflowPhase, projectExecution, type SubAgentTreeNode } from './subagent';
import type { ChatMessage, ExecutionEvent, WorkflowExecutionRecord, WorkflowPhase } from './types';

export interface WorkflowLiveState {
	execution: WorkflowExecutionRecord;
	phases: WorkflowPhase[];
	attentionPhases: string[];
	anchored: boolean;
}

function workflowAnchor(message: ChatMessage, data: Record<string, unknown>): WorkflowExecutionRecord | null {
	const id = data.executionId;
	if (typeof id !== 'string' || !id) return null;
	return {
		execution_id: id, chat_id: message.chat_id, conversation_id: message.conversation_id ?? message.chat_id, trigger_message_id: message.message_id,
		workflow_id: String(data.workflowId ?? ''),
		workflow_version: String(data.version ?? ''),
		requested_by: message.actor_id, state: (data.state ?? 'running') as WorkflowExecutionRecord['state'],
		active_phases: Array.isArray(data.activePhases) ? data.activePhases.filter((name): name is string => typeof name === 'string') : [],
		intent_epoch: message.intent_epoch, steering_message_ids: [], created_at: message.created_at
	};
}

export function deriveWorkflowLive(
	messages: ChatMessage[], eventsByExecution: Map<string, ExecutionEvent[]>,
	snapshots: Map<string, { execution: WorkflowExecutionRecord; seq: number }> = new Map(),
	metadata: Map<string, { execution: WorkflowExecutionRecord; seq: number }> = new Map()
): Map<string, WorkflowLiveState> {
	const anchors = new Map<string, { execution: WorkflowExecutionRecord; phases: WorkflowPhase[] }>();
	for (const message of messages) {
		const datums = receiptFor(message)?.result?.datums;
		const sources = Array.isArray(datums) ? datums.flatMap((datum) => datum && typeof datum === 'object' && datum.datumType === 'workflow_execution' ? [datum.structured] : []) :
			message.tool_result?.datumType === 'workflow_execution' ? [message.tool_result.structured] : [];
		for (const data of sources) {
			if (!data || typeof data !== 'object' || Array.isArray(data)) continue;
			const execution = workflowAnchor(message, data);
			if (!execution || anchors.has(execution.execution_id)) continue;
			const phases: WorkflowPhase[] = Array.isArray(data.phases) ? data.phases.flatMap((item: unknown) => {
				if (typeof item === 'string') return [{ name: item, status: 'pending' as const }];
				if (!item || typeof item !== 'object' || !('name' in item) || typeof item.name !== 'string') return [];
                const status = 'status' in item ? item.status : null;
				return [{ name: item.name, status: status === 'done' || status === 'active' ? status : 'pending' } satisfies WorkflowPhase];
			}) : [];
			anchors.set(execution.execution_id, { execution, phases });
		}
	}
	const result = new Map<string, WorkflowLiveState>();
	for (const id of new Set([...anchors.keys(), ...snapshots.keys(), ...metadata.keys()])) {
		const anchor = anchors.get(id);
		const current = metadata.get(id);
		const snapshot = current ?? snapshots.get(id);
		const execution = snapshot?.execution ?? anchor!.execution;
		const events = eventsByExecution.get(id) ?? [];
		// First-save snapshots may arrive late; every lifecycle event remains evidence.
		const projected = projectExecution(execution, current ? events.filter((event) => event.seq > current.seq) : events);
		const phases = new Map((anchor?.phases ?? []).filter((phase) => isWorkflowPhase(phase.name)).map((phase) => [phase.name, { ...phase }]));
		const phaseEvents = events.filter((event) => event.eventType !== 'sub_agent.spawned' ||
			event.payload.phaseExecutor === true);
		for (const phase of derivePhases(phaseEvents)) if (isWorkflowPhase(phase.name)) phases.set(phase.name, phase);
        for (const name of projected.active_phases) {
            if (isWorkflowPhase(name)) phases.set(name, { ...phases.get(name), name, status: 'active' });
        }
        const graphEvent = events.filter((event) => event.eventType === 'workflow.phase' && typeof event.payload.controlSequence === 'number')
            .sort((a, b) => Number(b.payload.controlSequence) - Number(a.payload.controlSequence))[0];
        const pendingInputs = graphEvent?.payload.controlSequence === projected.control_sequence ? graphEvent.payload.pendingInputs : null;
        if (pendingInputs && typeof pendingInputs === 'object' && !Array.isArray(pendingInputs)) {
            for (const [name, raw] of Object.entries(pendingInputs)) {
                if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
                const join = raw as { required?: unknown; arrived?: unknown };
                if (!Array.isArray(join.required) || !Array.isArray(join.arrived)) continue;
                const arrived = join.arrived;
                phases.set(name, { ...phases.get(name), name, status: 'pending',
                    waitingFor: join.required.filter((branch): branch is string => typeof branch === 'string' && !arrived.includes(branch)) });
            }
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
				const key = phase;
				const spawn = item.events.find((event) => event.eventType === 'sub_agent.spawned');
				const order = spawn?.seq ?? Number.MAX_SAFE_INTEGER;
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
		const phases = new Map(live.phases.map((phase) => [phase.name, phase]));
		for (const [key, { item }] of nodes.get(id) ?? []) {
			if (!item.node.phase) continue;
			const previous = phases.get(key);
			phases.set(key, { name: previous?.name ?? item.node.phase,
				status: item.node.status === 'completed' && !live.execution.control_sequence ? 'done' : live.execution.active_phases.includes(key) && !isTerminalWorkflow(live.execution) ? 'active' : item.node.status === 'completed' || previous?.status === 'done' ? 'done' : 'pending', attempts: previous?.attempts });
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
