import { describe, expect, it } from 'vitest';
import { computeWorkflowGraph } from '$lib/workflow-graph';
import type { WorkflowDefinition } from '$lib/types';

const definition: WorkflowDefinition = {
	workflow_id: 'custom', version: '2', title: 'Custom research',

	phases: ['new_phase', 'model_creation', 'completed'], terminal_phases: ['completed'],
	inputs: {"new_phase":["input"],"model_creation":["input"],"completed":["input"]}, routes: {"new_phase":{"model_creation":["model_creation.input"]},"model_creation":{"new_phase":["new_phase.input"],"completed":["completed.input"]},"completed":{}}
};

describe('backend-owned workflow topology', () => {
	it('does not invent phases or edges without a definition', () => {
		expect(computeWorkflowGraph([]).nodes).toEqual([]);
		const model = computeWorkflowGraph([{ name: 'alpha', status: 'active' }, { name: 'beta', status: 'pending' }]);
		expect(model.nodes).toHaveLength(2);
		expect(model.edges).toEqual([]);
	});
	it('lays out working phases and back edges without terminal nodes or their transitions', () => {
		const model = computeWorkflowGraph([{ name: 'new_phase', status: 'active', attempts: 2 },
			{ name: 'model_creation', status: 'done' }], definition);
		expect(model.nodes.map((node) => node.name)).toEqual(['new_phase', 'model_creation']);
		expect(model.edges.map((edge) => [edge.from, edge.to])).toEqual([
			['new_phase', 'model_creation'], ['model_creation', 'new_phase']
		]);
		expect(model.edges[1].kind).toBe('loopback');
		expect(model.edges[1].traversed).toBe(true);
		expect(model.edges.every((edge) => !!edge.arrow)).toBe(true);
	});

});
