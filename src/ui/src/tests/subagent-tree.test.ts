import { describe, expect, it } from 'vitest';
import {
	buildTranscriptItems,
	deriveGates,
	derivePhases,
	deriveSubAgentTree,
	mergeEvents
} from '$lib/subagent';
import type { ExecutionEvent } from '$lib/types';

function event(
	eventType: ExecutionEvent['eventType'],
	payload: Record<string, unknown>,
	cursor: number
): ExecutionEvent {
	return {
		actorId: 'thesis-agent',
		actorScope: 'workflow',
		correlationId: `corr-${cursor}`,
		cursor,
		eventId: `evt-${cursor}`,
		eventType,
		executionId: 'exec-1',
		intentEpoch: 0,
		lateResult: false,
		occurredAt: `2026-09-08T17:14:0${cursor}Z`,
		payload,
		workspaceId: 'alpha-lab'
	};
}

describe('deriveSubAgentTree', () => {
	const events: ExecutionEvent[] = [
		event(
			'sub_agent.spawned',
			{
				subAgentId: 'sub-1',
				parentSubAgentId: null,
				phase: 'research',
				role: 'Researcher',
				context: { isolated: true, knowledge_cutoff: '2024-01-01', mcp: { enabled: true, tools: ['search'] } },
				acceptsInput: true
			},
			1
		),
		event(
			'sub_agent.spawned',
			{
				subAgentId: 'sub-2',
				parentSubAgentId: 'sub-1',
				phase: 'research',
				role: 'Coder',
				acceptsInput: false
			},
			2
		),
		event('tool.result', { subAgentId: 'sub-1', toolResult: { tool: 'run_backtest', ok: true } }, 3)
	];

	it('nests children under their parent and attributes own events', () => {
		const roots = deriveSubAgentTree(events);
		expect(roots).toHaveLength(1);
		expect(roots[0].node.subAgentId).toBe('sub-1');
		expect(roots[0].node.context.isolated).toBe(true);
		expect(roots[0].node.context.mcp.tools).toEqual(['search']);
		expect(roots[0].children.map((child) => child.node.subAgentId)).toEqual(['sub-2']);
		expect(roots[0].events.map((e) => e.cursor)).toEqual([1, 3]);
		expect(roots[0].children[0].events.map((e) => e.cursor)).toEqual([2]);
	});

	it('seeds REST-known nodes before their events replay', () => {
		const roots = deriveSubAgentTree([], [
			{
				subAgentId: 'sub-9',
				parentSubAgentId: null,
				phase: 'audit',
				role: 'Auditor',
				status: 'spawned',
				context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
				acceptsInput: true
			}
		]);
		expect(roots).toHaveLength(1);
		expect(roots[0].node.role).toBe('Auditor');
	});
});

describe('deriveGates', () => {
	it('renders a blocked reason and folds in the resolution', () => {
		const blocked = event(
			'workflow.gate.blocked',
			{ gateId: 'gate-1', phase: 'activation', reason: 'Capital bound exceeds policy' },
			1
		);
		const [gate] = deriveGates([blocked]);
		expect(gate.status).toBe('blocked');
		expect(gate.reason).toBe('Capital bound exceeds policy');

		const resolved = event(
			'workflow.gate.resolved',
			{ gateId: 'gate-1', phase: 'activation', resolution: 'verdict', verdict: 'return', note: 'tighten' },
			2
		);
		const [after] = deriveGates([blocked, resolved]);
		expect(after.status).toBe('resolved');
		expect(after.resolution?.kind).toBe('verdict');
		expect(after.resolution?.verdict).toBe('return');
	});
});

describe('derivePhases', () => {
	it('marks earlier phases done and the latest active', () => {
		const phases = derivePhases([
			event('workflow.started', { phase: 'plan' }, 1),
			event('workflow.started', { phase: 'research' }, 2)
		]);
		expect(phases).toEqual([
			{ name: 'plan', status: 'done' },
			{ name: 'research', status: 'active' }
		]);
	});

	it('marks every phase done after completion', () => {
		const phases = derivePhases([
			event('workflow.started', { phase: 'plan' }, 1),
			event('workflow.completed', {}, 2)
		]);
		expect(phases.every((phase) => phase.status === 'done')).toBe(true);
	});
});

describe('buildTranscriptItems + mergeEvents', () => {
	it('routes events onto the kind discriminator and dedupes by id', () => {
		const tool = event('tool.result', { subAgentId: 'sub-1' }, 1);
		const gate = event('workflow.gate.blocked', { gateId: 'g' }, 2);
		const spawn = event('sub_agent.spawned', { subAgentId: 'sub-1' }, 3);
		const kinds = buildTranscriptItems([tool, gate, spawn]).map((item) => item.kind);
		expect(kinds).toEqual(['tool', 'gate', 'sub_agent']);
		expect(mergeEvents([tool], [tool, gate]).map((e) => e.eventId)).toEqual(['evt-1', 'evt-2']);
	});
});
