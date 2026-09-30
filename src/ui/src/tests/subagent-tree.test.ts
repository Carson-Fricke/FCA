import { describe, expect, it } from 'vitest';
import {
	buildTranscriptItems,
	derivePhaseSteps,
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
				acceptsInput: true,
				workflowExecutionId: 'exec-1'
			}
		]);
		expect(roots).toHaveLength(1);
		expect(roots[0].node.role).toBe('Auditor');
	});
});

describe('derivePhases', () => {
	it('marks earlier phases done and the latest active', () => {
		const phases = derivePhases([
			event('workflow.started', { phase: 'plan' }, 1),
			event('workflow.started', { phase: 'research' }, 2)
		]);
		expect(phases).toEqual([
			{ name: 'plan', status: 'done', attempts: 1 },
			{ name: 'research', status: 'active', attempts: 1 }
		]);
	});

	it('marks every phase done after completion', () => {
		const phases = derivePhases([
			event('workflow.started', { phase: 'plan' }, 1),
			event('workflow.completed', {}, 2)
		]);
		expect(phases.every((phase) => phase.status === 'done')).toBe(true);
	});

	it('keeps declared but unvisited phases pending', () => {
		const phases = derivePhases([
			event('workflow.launched', { workflow: { phases: ['requested', 'plan', 'optional_rca', 'report'] } }, 1),
			event('workflow.started', {}, 2),
			event('workflow.phase', { phase: 'plan' }, 3),
			event('workflow.phase', { phase: 'report' }, 4)
		]);
		expect(phases).toEqual([
			{ name: 'plan', status: 'done', attempts: 1 },
			{ name: 'optional_rca', status: 'pending', attempts: 0 },
			{ name: 'report', status: 'active', attempts: 1 }
		]);
	});

	it('counts repeated visits without duplicating a phase', () => {
		const phases = derivePhases([
			event('workflow.started', { phase: 'strategy_design' }, 1),
			event('workflow.phase', { phase: 'strategy_backtesting' }, 2),
			event('workflow.phase', { phase: 'strategy_rca' }, 3),
			event('workflow.phase', { phase: 'strategy_design' }, 4)
		]);
		expect(phases).toEqual([
			{ name: 'strategy_design', status: 'active', attempts: 2 },
			{ name: 'strategy_backtesting', status: 'done', attempts: 1 },
			{ name: 'strategy_rca', status: 'done', attempts: 1 }
		]);
	});

	it('does not count progress or result events as attempts', () => {
		const phases = derivePhases([
			event(
				'workflow.launched',
				{ workflow: { phases: ['requested', 'falsification_trials'] } },
				1
			),
			event('workflow.started', {}, 2),
			event('workflow.phase', { phase: 'falsification_trials' }, 3),
			event('workflow.progress', { phase: 'falsification_trials' }, 4),
			event('workflow.phase_result', { phase: 'falsification_trials' }, 5)
		]);
		expect(phases).toEqual([
			{ name: 'falsification_trials', status: 'active', attempts: 1 }
		]);
	});

	it('counts one attempt per phase-agent spawn and ignores the hidden workflow node', () => {
		const phases = derivePhases([
			event(
				'workflow.launched',
				{ workflow: { phases: ['requested', 'hypothesis_creation'] } },
				1
			),
			event('workflow.started', {}, 2),
			event('sub_agent.spawned', { phase: 'hypothesis_creation', role: 'workflow-node' }, 3),
			event(
				'sub_agent.spawned',
				{ phase: 'hypothesis_creation', role: 'Thesis hypothesis-creation orchestrator' },
				4
			),
			event(
				'sub_agent.spawned',
				{ phase: 'hypothesis_creation', role: 'Thesis hypothesis-creation orchestrator' },
				5
			),
			event('workflow.progress', { phase: 'hypothesis_creation' }, 6)
		]);
		expect(phases).toEqual([
			{ name: 'hypothesis_creation', status: 'active', attempts: 2 }
		]);
	});
});

describe('derivePhaseSteps', () => {
	it('lists top-level phase orchestrators in spawn order with completion state', () => {
		const events = [
			event(
				'sub_agent.spawned',
				{ subAgentId: 'sub-a', parentSubAgentId: null, phase: 'ideation', role: 'A', acceptsInput: true },
				1
			),
			event(
				'sub_agent.completed',
				{ subAgentId: 'sub-a', parentSubAgentId: null, phase: 'ideation', role: 'A', frozen: true, status: 'completed' },
				2
			),
			event(
				'sub_agent.spawned',
				{ subAgentId: 'sub-b', parentSubAgentId: null, phase: 'hypothesis', role: 'B' },
				3
			),
			event(
				'sub_agent.spawned',
				{ subAgentId: 'sub-c', parentSubAgentId: 'sub-b', phase: 'hypothesis', role: 'C' },
				4
			)
		];
		const steps = derivePhaseSteps(deriveSubAgentTree(events));
		expect(steps).toEqual([
			{ name: 'ideation', status: 'done' },
			{ name: 'hypothesis', status: 'active' }
		]);
	});
});

describe('workflow-node records', () => {
	function workflowEvents(): ExecutionEvent[] {
		return [
			event(
				'sub_agent.spawned',
				{ subAgentId: 'anchor-1', parentSubAgentId: null, phase: '', role: 'Anchor', acceptsInput: true },
				0
			),
			event(
				'sub_agent.spawned',
				{
					subAgentId: 'wf-1',
					parentSubAgentId: 'anchor-1',
					phase: 'requested',
					role: 'workflow-node',
					acceptsInput: false
				},
				1
			),
			event(
				'sub_agent.spawned',
				{ subAgentId: 'phase-1', parentSubAgentId: 'wf-1', phase: 'ideation', role: 'Ideation' },
				2
			),
			event(
				'sub_agent.spawned',
				{ subAgentId: 'helper-1', parentSubAgentId: 'phase-1', phase: 'ideation', role: 'Researcher' },
				3
			)
		];
	}

	it('hides the event-derived workflow node and re-parents its phase agent to the anchor', () => {
		const roots = deriveSubAgentTree(workflowEvents());
		expect(roots.map((node) => node.node.subAgentId)).toEqual(['anchor-1']);
		const phase = roots[0].children[0];
		expect(phase.node.subAgentId).toBe('phase-1');
		expect(phase.node.parentSubAgentId).toBe('anchor-1');
		expect(phase.children.map((node) => node.node.subAgentId)).toEqual(['helper-1']);
	});

	it('hides the durable workflow node and re-parents its phase agent to the anchor', () => {
		const roots = deriveSubAgentTree([], [
			{
				subAgentId: 'anchor-1',
				parentSubAgentId: null,
				phase: '',
				role: 'Anchor',
				status: 'running',
				context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
				acceptsInput: true,
				workflowExecutionId: 'exec-1'
			},
			{
				subAgentId: 'wf-1',
				parentSubAgentId: 'anchor-1',
				phase: 'requested',
				role: 'workflow-node',
				status: 'running',
				context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
				acceptsInput: false,
				workflowExecutionId: 'exec-1'
			},
			{
				subAgentId: 'phase-1',
				parentSubAgentId: 'wf-1',
				phase: 'ideation',
				role: 'Ideation',
				status: 'running',
				context: { isolated: false, knowledge_cutoff: null, mcp: { enabled: false, tools: [] } },
				acceptsInput: false,
				workflowExecutionId: 'exec-1'
			}
		]);
		expect(roots.map((node) => node.node.subAgentId)).toEqual(['anchor-1']);
		expect(roots[0].children.map((node) => node.node.subAgentId)).toEqual(['phase-1']);
		expect(roots[0].children[0].node.parentSubAgentId).toBe('anchor-1');
	});

	it('lists phase agents — never the workflow node or its requested phase', () => {
		const steps = derivePhaseSteps(deriveSubAgentTree(workflowEvents()), 'exec-1');
		expect(steps).toEqual([{ name: 'ideation', status: 'active' }]);
	});

	it('scopes phase steps to one execution when an execution id is given', () => {
		expect(derivePhaseSteps(deriveSubAgentTree(workflowEvents()), 'exec-2')).toEqual([]);
	});
});

describe('buildTranscriptItems + mergeEvents', () => {
	it('routes events onto the kind discriminator and dedupes by id', () => {
		const tool = event('tool.result', { subAgentId: 'sub-1' }, 1);
		const spawn = event('sub_agent.spawned', { subAgentId: 'sub-1' }, 2);
		const kinds = buildTranscriptItems([tool, spawn]).map((item) => item.kind);
		expect(kinds).toEqual(['tool', 'sub_agent']);
		expect(mergeEvents([tool], [tool, spawn]).map((e) => e.eventId)).toEqual(['evt-1', 'evt-2']);
	});
});
