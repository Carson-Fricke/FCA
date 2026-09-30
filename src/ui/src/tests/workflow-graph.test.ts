import { describe, expect, it } from 'vitest';
import {
	buildHypothesisWorkflowGraph,
	buildLinearWorkflowGraph,
	computeWorkflowGraph,
	HYPOTHESIS_PHASES
} from '$lib/workflow-graph';
import type { WorkflowPhase } from '$lib/types';

describe('workflow-graph', () => {
	it('handles empty phases list', () => {
		const model = computeWorkflowGraph([]);
		expect(model.nodes).toHaveLength(0);
		expect(model.edges).toHaveLength(0);
		expect(model.width).toBe(0);
		expect(model.height).toBe(0);
	});

	it('builds linear graph for custom phases', () => {
		const phases: WorkflowPhase[] = [
			{ name: 'alpha', status: 'done' },
			{ name: 'beta', status: 'active' },
			{ name: 'gamma', status: 'pending' }
		];
		const model = computeWorkflowGraph(phases, 'custom-workflow');
		expect(model.nodes).toHaveLength(3);
		expect(model.edges).toHaveLength(2);
		expect(model.edges[0].traversed).toBe(true);
		expect(model.edges[1].traversed).toBe(false);
		expect(model.stages).toHaveLength(0);
	});

	it('detects hypothesis workflow and lays out 2-tier DAG with loopbacks', () => {
		const phases: WorkflowPhase[] = [
			{ name: 'hypothesis_creation', status: 'done' },
			{ name: 'falsification_trials', status: 'active' }
		];
		const model = computeWorkflowGraph(phases, 'hypothesis-development');
		expect(model.nodes).toHaveLength(HYPOTHESIS_PHASES.length);

		// Check all 7 phases are present
		const nodeNames = model.nodes.map((n) => n.name);
		expect(nodeNames).toEqual(expect.arrayContaining([...HYPOTHESIS_PHASES]));

		// Status preservation
		const hc = model.nodes.find((n) => n.name === 'hypothesis_creation')!;
		expect(hc.status).toBe('done');
		const ft = model.nodes.find((n) => n.name === 'falsification_trials')!;
		expect(ft.status).toBe('active');
		const vd = model.nodes.find((n) => n.name === 'verdict')!;
		expect(vd.status).toBe('pending');

		// Check stages
		expect(model.stages).toHaveLength(2);
		expect(model.stages[0].name).toBe('Hypothesis Validation');
		expect(model.stages[1].name).toBe('Strategy Development');

		// Check edges include loopbacks and branches
		const loopbacks = model.edges.filter((e) => e.kind === 'loopback');
		expect(loopbacks.length).toBeGreaterThanOrEqual(3);

		// Verdict -> hypothesis_creation loopback
		const vToHc = model.edges.find(
			(e) => e.from === 'verdict' && e.to === 'hypothesis_creation'
		);
		expect(vToHc).toBeDefined();
		expect(vToHc?.kind).toBe('loopback');

		// Strategy backtesting -> strategy_rca branch
		const sbToRca = model.edges.find(
			(e) => e.from === 'strategy_backtesting' && e.to === 'strategy_rca'
		);
		expect(sbToRca).toBeDefined();
		expect(sbToRca?.kind).toBe('branch');

		// Strategy rca -> strategy_design loopback
		const rcaToSd = model.edges.find(
			(e) => e.from === 'strategy_rca' && e.to === 'strategy_design'
		);
		expect(rcaToSd).toBeDefined();
		expect(rcaToSd?.kind).toBe('loopback');

		// Optimization -> strategy_rca loopback
		const optToRca = model.edges.find(
			(e) => e.from === 'optimization' && e.to === 'strategy_rca'
		);
		expect(optToRca).toBeDefined();
		expect(optToRca?.kind).toBe('loopback');
	});

	it('marks loopback traversed when attempt counter > 1', () => {
		const phases: WorkflowPhase[] = [
			{ name: 'hypothesis_creation', status: 'active', attempts: 2 },
			{ name: 'verdict', status: 'done' }
		];
		const model = computeWorkflowGraph(phases, 'hypothesis-development');
		const vToHc = model.edges.find(
			(e) => e.from === 'verdict' && e.to === 'hypothesis_creation'
		)!;
		expect(vToHc.traversed).toBe(true);
	});
});
