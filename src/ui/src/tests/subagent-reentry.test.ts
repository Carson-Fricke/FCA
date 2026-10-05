import { describe, expect, it } from 'vitest';
import { deriveSubAgentTree } from '$lib/subagent';
import type { ExecutionEvent, SubAgentNode } from '$lib/types';

const at = (second: number) => `2026-01-01T00:00:${String(second).padStart(2, '0')}Z`;
const node: SubAgentNode = { subAgentId: 'phase-node', parentSubAgentId: null, phase: 'model_creation',
	role: 'researcher', status: 'running', acceptsInput: true, workflowExecutionId: 'exec',
	createdAt: at(1), updatedAt: at(2), context: { isolated: true, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } } };
function event(seq: number, type: ExecutionEvent['eventType'], attempt?: string): ExecutionEvent {
	return { seq, cursor: seq, eventId: `event-${seq}`, eventType: type, occurredAt: at(seq),
		actorId: 'agent', actorScope: 'assistant', correlationId: 'exec', executionId: 'exec', intentEpoch: 0,
		lateResult: false, workspaceId: 'ws', payload: { subAgentId: node.subAgentId, phase: node.phase,
			role: node.role, acceptsInput: true, ...(attempt ? { phaseAttemptId: attempt } : {}) } };
}
const old = [event(1, 'sub_agent.spawned'), event(3, 'sub_agent.completed')];
const fold = (events: ExecutionEvent[], snapshot?: SubAgentNode) => deriveSubAgentTree(events, snapshot ? [snapshot] : [])[0].node;

describe('reused phase node lifecycle', () => {
	it.each(['spawned', 'running', 'needs_input'] as const)('prefers the newer %s attempt snapshot over old terminal replay', (status) => {
		const snapshot = { ...node, createdAt: at(5), updatedAt: at(6), status };
		expect(fold(old, snapshot)).toMatchObject({ status, acceptsInput: true, createdAt: at(5) });
	});
	it.each(['completed', 'failed', 'cancelled'] as const)('reopens %s only at a later spawn boundary', (status) => {
		const terminal = event(3, `sub_agent.${status}`);
		expect(fold([event(1, 'sub_agent.spawned'), terminal, event(5, 'sub_agent.spawned'), event(6, 'sub_agent.needs_input')]))
			.toMatchObject({ status: 'needs_input', acceptsInput: true, createdAt: at(5) });
	});
	it('ignores a stale completed REST snapshot when newer reentry events are present', () => {
		const snapshot = { ...node, status: 'completed' as const, acceptsInput: false, updatedAt: at(3) };
		expect(fold([...old, event(5, 'sub_agent.spawned'), event(6, 'sub_agent.progress')], snapshot))
			.toMatchObject({ status: 'progress', acceptsInput: true, createdAt: at(5) });
	});
	it('keeps cancellation absorbing for the same attempt, including a later running snapshot', () => {
		const events = [event(1, 'sub_agent.spawned', 'a'), event(3, 'sub_agent.cancelled', 'a'),
			event(4, 'sub_agent.spawned', 'a'), event(5, 'sub_agent.needs_input', 'a')];
		expect(fold(events, { ...node, updatedAt: at(6), phaseAttemptId: 'a' }))
			.toMatchObject({ status: 'cancelled', acceptsInput: false });
	});
	it('folds sequence-ordered events sharing a timestamp without freezing the new attempt', () => {
		const spawn = event(5, 'sub_agent.spawned', 'b');
		const needsInput = { ...event(6, 'sub_agent.needs_input', 'b'), occurredAt: spawn.occurredAt };
		expect(fold([...old, spawn, needsInput])).toMatchObject({ status: 'needs_input', acceptsInput: true });
	});
	it('keeps seq authoritative for same-attempt events with skewed clocks', () => {
		const output = { ...event(6, 'sub_agent.progress', 'b'), occurredAt: '2026-01-01T00:00:09Z' };
		const needsInput = { ...event(7, 'sub_agent.needs_input', 'b'), occurredAt: '2026-01-01T00:00:08Z' };
		expect(fold([...old, event(5, 'sub_agent.spawned', 'b'), output, needsInput]))
			.toMatchObject({ status: 'needs_input', acceptsInput: true });
	});
	it('does not let a delayed old-attempt terminal event close the new attempt', () => {
		expect(fold([event(1, 'sub_agent.spawned', 'a'), event(3, 'sub_agent.completed', 'a'),
			event(5, 'sub_agent.spawned', 'b'), event(6, 'sub_agent.completed', 'a')]))
			.toMatchObject({ status: 'spawned', acceptsInput: true, phaseAttemptId: 'b' });
	});
	it('preserves a current terminal snapshot against historical spawn replay', () => {
		expect(fold([event(1, 'sub_agent.spawned')], { ...node, updatedAt: at(3), status: 'cancelled', acceptsInput: false }))
			.toMatchObject({ status: 'cancelled', acceptsInput: false });
	});
});
