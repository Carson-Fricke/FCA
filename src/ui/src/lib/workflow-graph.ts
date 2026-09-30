import type { WorkflowPhase } from '$lib/types';

export interface GraphNode {
	name: string;
	status: 'done' | 'active' | 'pending';
	attempts?: number;
	x: number;
	y: number;
	width: number;
	height: number;
	stage?: string;
}

export interface GraphEdge {
	id: string;
	from: string;
	to: string;
	kind: 'forward' | 'loopback' | 'branch';
	label?: string;
	pathD: string;
	labelX?: number;
	labelY?: number;
	traversed: boolean;
	highlight: boolean;
}

export interface StageGroup {
	name: string;
	x: number;
	y: number;
	width: number;
	height: number;
	headerY: number;
}

export interface WorkflowGraphModel {
	width: number;
	height: number;
	nodes: GraphNode[];
	edges: GraphEdge[];
	stages: StageGroup[];
}

export const HYPOTHESIS_PHASES = [
	'hypothesis_creation',
	'falsification_trials',
	'verdict',
	'strategy_design',
	'strategy_backtesting',
	'strategy_rca',
	'optimization'
] as const;

export type HypothesisPhaseName = (typeof HYPOTHESIS_PHASES)[number];

export const NODE_WIDTH = 168;
export const NODE_HEIGHT = 42;
const COL_GAP = 36;
const STAGE_GAP = 54;
const ROW_GAP = 50;
const ROW_0_Y = 56;
const ROW_1_Y = ROW_0_Y + NODE_HEIGHT + ROW_GAP; // 56 + 42 + 50 = 148

function isHypothesisWorkflow(phases: WorkflowPhase[], workflowId?: string): boolean {
	if (workflowId === 'hypothesis-development' || workflowId === 'hypothesis_development') {
		return true;
	}
	if (!phases.length) return false;
	const matchCount = phases.filter((p) =>
		HYPOTHESIS_PHASES.includes(p.name as HypothesisPhaseName)
	).length;
	return matchCount >= 2 || (matchCount > 0 && matchCount === phases.length);
}

/**
 * Builds a structured graph layout for the hypothesis development workflow,
 * surfacing loopbacks and branches with generous spacing between stage headers and nodes.
 */
export function buildHypothesisWorkflowGraph(
	phases: WorkflowPhase[]
): WorkflowGraphModel {
	const phaseMap = new Map<string, WorkflowPhase>();
	for (const p of phases) {
		phaseMap.set(p.name, p);
	}

	const colX = (col: number) => {
		if (col <= 2) {
			return 20 + col * (NODE_WIDTH + COL_GAP);
		}
		const stage1End = 20 + 3 * NODE_WIDTH + 2 * COL_GAP;
		const stage2Start = stage1End + STAGE_GAP;
		return stage2Start + (col - 3) * (NODE_WIDTH + COL_GAP);
	};

	const nodePositions: Record<HypothesisPhaseName, { x: number; y: number; stage: string }> = {
		hypothesis_creation: { x: colX(0), y: ROW_0_Y, stage: 'Hypothesis Validation' },
		falsification_trials: { x: colX(1), y: ROW_0_Y, stage: 'Hypothesis Validation' },
		verdict: { x: colX(2), y: ROW_0_Y, stage: 'Hypothesis Validation' },
		strategy_design: { x: colX(3), y: ROW_0_Y, stage: 'Strategy Development' },
		strategy_backtesting: { x: colX(4), y: ROW_0_Y, stage: 'Strategy Development' },
		strategy_rca: { x: colX(4), y: ROW_1_Y, stage: 'Strategy Development' },
		optimization: { x: colX(5), y: ROW_0_Y, stage: 'Strategy Development' }
	};

	const nodes: GraphNode[] = HYPOTHESIS_PHASES.map((name) => {
		const existing = phaseMap.get(name);
		const pos = nodePositions[name];
		return {
			name,
			status: existing?.status ?? 'pending',
			attempts: existing?.attempts,
			x: pos.x,
			y: pos.y,
			width: NODE_WIDTH,
			height: NODE_HEIGHT,
			stage: pos.stage
		};
	});

	const nodeByName = new Map(nodes.map((n) => [n.name, n]));
	const getNode = (name: string) => nodeByName.get(name)!;

	const isTraversed = (fromName: string, toName: string, kind: 'forward' | 'loopback' | 'branch') => {
		const from = getNode(fromName);
		const to = getNode(toName);
		if (!from || !to) return false;
		if (kind === 'forward') {
			return from.status === 'done' && (to.status === 'active' || to.status === 'done');
		}
		if (kind === 'branch') {
			return to.status === 'active' || to.status === 'done';
		}
		if (toName === 'hypothesis_creation') {
			return (to.attempts ?? 0) > 1;
		}
		if (toName === 'strategy_design') {
			return (to.attempts ?? 0) > 1 || from.status === 'done';
		}
		if (toName === 'strategy_rca') {
			return to.status === 'active' || to.status === 'done';
		}
		return false;
	};

	const edges: GraphEdge[] = [];

	// 1. Forward: hypothesis_creation -> falsification_trials
	{
		const from = getNode('hypothesis_creation');
		const to = getNode('falsification_trials');
		const startX = from.x + from.width;
		const startY = from.y + from.height / 2;
		const endX = to.x - 3;
		const endY = to.y + to.height / 2;
		const traversed = isTraversed('hypothesis_creation', 'falsification_trials', 'forward');
		edges.push({
			id: 'hc-to-ft',
			from: from.name,
			to: to.name,
			kind: 'forward',
			pathD: `M ${startX} ${startY} L ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	// 2. Forward: falsification_trials -> verdict
	{
		const from = getNode('falsification_trials');
		const to = getNode('verdict');
		const startX = from.x + from.width;
		const startY = from.y + from.height / 2;
		const endX = to.x - 3;
		const endY = to.y + to.height / 2;
		const traversed = isTraversed('falsification_trials', 'verdict', 'forward');
		edges.push({
			id: 'ft-to-vd',
			from: from.name,
			to: to.name,
			kind: 'forward',
			pathD: `M ${startX} ${startY} L ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	// 3. Forward: verdict -> strategy_design (Bridge)
	{
		const from = getNode('verdict');
		const to = getNode('strategy_design');
		const startX = from.x + from.width;
		const startY = from.y + from.height / 2;
		const endX = to.x - 3;
		const endY = to.y + to.height / 2;
		const traversed = isTraversed('verdict', 'strategy_design', 'forward');
		edges.push({
			id: 'vd-to-sd',
			from: from.name,
			to: to.name,
			kind: 'forward',
			label: 'VALIDATED',
			labelX: (startX + endX) / 2,
			labelY: startY - 9,
			pathD: `M ${startX} ${startY} L ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	// 4. Forward: strategy_design -> strategy_backtesting
	{
		const from = getNode('strategy_design');
		const to = getNode('strategy_backtesting');
		const startX = from.x + from.width;
		const startY = from.y + from.height / 2;
		const endX = to.x - 3;
		const endY = to.y + to.height / 2;
		const traversed = isTraversed('strategy_design', 'strategy_backtesting', 'forward');
		edges.push({
			id: 'sd-to-sb',
			from: from.name,
			to: to.name,
			kind: 'forward',
			pathD: `M ${startX} ${startY} L ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	// 5. Forward: strategy_backtesting -> optimization
	{
		const from = getNode('strategy_backtesting');
		const to = getNode('optimization');
		const startX = from.x + from.width;
		const startY = from.y + from.height / 2;
		const endX = to.x - 3;
		const endY = to.y + to.height / 2;
		const traversed = isTraversed('strategy_backtesting', 'optimization', 'forward');
		edges.push({
			id: 'sb-to-opt',
			from: from.name,
			to: to.name,
			kind: 'forward',
			label: 'PASS',
			labelX: (startX + endX) / 2,
			labelY: startY - 9,
			pathD: `M ${startX} ${startY} L ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	// 6. Loopback: verdict -> hypothesis_creation
	{
		const from = getNode('verdict');
		const to = getNode('hypothesis_creation');
		const startX = from.x + from.width / 2;
		const startY = from.y + from.height;
		const endX = to.x + to.width / 2;
		const endY = to.y + to.height + 4;
		const dipY = ROW_1_Y + 16;
		const traversed = isTraversed('verdict', 'hypothesis_creation', 'loopback');
		edges.push({
			id: 'vd-to-hc-loop',
			from: from.name,
			to: to.name,
			kind: 'loopback',
			label: 'LOOPBACK',
			labelX: (startX + endX) / 2,
			labelY: dipY + 14,
			pathD: `M ${startX} ${startY} C ${startX} ${dipY}, ${endX} ${dipY}, ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	// 7. Branch: strategy_backtesting -> strategy_rca
	{
		const from = getNode('strategy_backtesting');
		const to = getNode('strategy_rca');
		const startX = from.x + from.width / 2;
		const startY = from.y + from.height;
		const endX = to.x + to.width / 2;
		const endY = to.y - 3;
		const traversed = isTraversed('strategy_backtesting', 'strategy_rca', 'branch');
		edges.push({
			id: 'sb-to-rca',
			from: from.name,
			to: to.name,
			kind: 'branch',
			label: 'FAIL / RCA',
			labelX: startX + 38,
			labelY: (startY + endY) / 2,
			pathD: `M ${startX} ${startY} L ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	// 8. Loopback: strategy_rca -> strategy_design
	{
		const from = getNode('strategy_rca');
		const to = getNode('strategy_design');
		const startX = from.x;
		const startY = from.y + from.height / 2;
		const endX = to.x + to.width / 2;
		const endY = to.y + to.height + 4;
		const traversed = isTraversed('strategy_rca', 'strategy_design', 'loopback');
		edges.push({
			id: 'rca-to-sd-loop',
			from: from.name,
			to: to.name,
			kind: 'loopback',
			label: 'REVISE',
			labelX: startX - 32,
			labelY: startY - 12,
			pathD: `M ${startX} ${startY} C ${startX - 56} ${startY}, ${endX} ${startY - 14}, ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	// 9. Loopback: optimization -> strategy_rca
	{
		const from = getNode('optimization');
		const to = getNode('strategy_rca');
		const startX = from.x + from.width / 2;
		const startY = from.y + from.height;
		const endX = to.x + to.width + 3;
		const endY = to.y + to.height / 2;
		const traversed = isTraversed('optimization', 'strategy_rca', 'loopback');
		edges.push({
			id: 'opt-to-rca-loop',
			from: from.name,
			to: to.name,
			kind: 'loopback',
			label: 'RETRY',
			labelX: (startX + endX) / 2 + 12,
			labelY: startY + 34,
			pathD: `M ${startX} ${startY} C ${startX} ${endY}, ${endX + 36} ${endY}, ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	const stage1X = colX(0) - 10;
	const stage1W = colX(2) + NODE_WIDTH + 10 - stage1X;
	const stage2X = colX(3) - 10;
	const stage2W = colX(5) + NODE_WIDTH + 10 - stage2X;
	const stageHeight = ROW_1_Y + NODE_HEIGHT + 20 - 10;

	const stages: StageGroup[] = [
		{
			name: 'Hypothesis Validation',
			x: stage1X,
			y: 10,
			width: stage1W,
			height: stageHeight,
			headerY: 26
		},
		{
			name: 'Strategy Development',
			x: stage2X,
			y: 10,
			width: stage2W,
			height: stageHeight,
			headerY: 26
		}
	];

	const totalWidth = colX(5) + NODE_WIDTH + 24;
	const totalHeight = ROW_1_Y + NODE_HEIGHT + 36;

	return {
		width: totalWidth,
		height: totalHeight,
		nodes,
		edges,
		stages
	};
}

/**
 * Fallback linear graph layout for custom or arbitrary phases.
 */
export function buildLinearWorkflowGraph(phases: WorkflowPhase[]): WorkflowGraphModel {
	if (!phases.length) {
		return { width: 0, height: 0, nodes: [], edges: [], stages: [] };
	}

	const nodes: GraphNode[] = phases.map((phase, idx) => ({
		name: phase.name,
		status: phase.status,
		attempts: phase.attempts,
		x: 20 + idx * (NODE_WIDTH + COL_GAP),
		y: ROW_0_Y,
		width: NODE_WIDTH,
		height: NODE_HEIGHT
	}));

	const edges: GraphEdge[] = [];
	for (let i = 0; i < nodes.length - 1; i++) {
		const from = nodes[i];
		const to = nodes[i + 1];
		const startX = from.x + from.width;
		const startY = from.y + from.height / 2;
		const endX = to.x - 3;
		const endY = to.y + to.height / 2;
		const traversed = from.status === 'done' && (to.status === 'active' || to.status === 'done');
		edges.push({
			id: `edge-${from.name}-${to.name}`,
			from: from.name,
			to: to.name,
			kind: 'forward',
			pathD: `M ${startX} ${startY} L ${endX} ${endY}`,
			traversed,
			highlight: traversed
		});
	}

	const totalWidth = nodes[nodes.length - 1].x + NODE_WIDTH + 24;
	const totalHeight = ROW_0_Y + NODE_HEIGHT + 32;

	return {
		width: totalWidth,
		height: totalHeight,
		nodes,
		edges,
		stages: []
	};
}

export function computeWorkflowGraph(
	phases: WorkflowPhase[],
	workflowId?: string
): WorkflowGraphModel {
	if (!phases.length) {
		return { width: 0, height: 0, nodes: [], edges: [], stages: [] };
	}
	if (isHypothesisWorkflow(phases, workflowId)) {
		return buildHypothesisWorkflowGraph(phases);
	}
	return buildLinearWorkflowGraph(phases);
}
