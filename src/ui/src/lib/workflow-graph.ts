import type { WorkflowDefinition, WorkflowPhase } from '$lib/types';

export interface GraphNode {
	name: string;
	status: 'done' | 'active' | 'pending';
	attempts?: number;
	x: number;
	y: number;
	width: number;
	height: number;
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
	arrow?: { x: number; y: number; angle: number };
	traversed: boolean;
	highlight: boolean;
}

export interface WorkflowGraphModel {
	width: number;
	height: number;
	nodes: GraphNode[];
	edges: GraphEdge[];
	stages: Array<{ name: string; x: number; y: number; width: number; height: number; headerY: number }>;
}

/** Read compatibility for persisted retired phase names, not a topology table. */
export function canonicalPhaseName(name: string): string {
	return name === 'hypothesis_creation' ? 'model_creation' : name;
}

export const NODE_WIDTH = 168;
export const NODE_HEIGHT = 42;

/** Geometry is presentation-owned; only the fetched definition supplies legal edges. */
export function computeWorkflowGraph(phases: WorkflowPhase[], definition?: WorkflowDefinition | null): WorkflowGraphModel {
	const states = new Map(phases.map((phase) => [canonicalPhaseName(phase.name), phase]));
	const names = [...new Set([...(definition?.phases ?? []).map(canonicalPhaseName), ...states.keys()])];
	const nodes: GraphNode[] = names.map((name, index) => ({
		name, status: states.get(name)?.status ?? 'pending', attempts: states.get(name)?.attempts,
		x: 20 + index * (NODE_WIDTH + 36), y: 56, width: NODE_WIDTH, height: NODE_HEIGHT
	}));
	const byName = new Map(nodes.map((node) => [node.name, node]));
	const edges: GraphEdge[] = [];
	for (const [source, targets] of Object.entries(definition?.transitions ?? {})) {
		const from = byName.get(canonicalPhaseName(source));
		if (!from) continue;
		for (const target of targets) {
			const to = byName.get(canonicalPhaseName(target));
			if (!to) continue;
			const loop = to.x <= from.x;
			const startX = loop ? from.x + from.width / 2 : from.x + from.width;
			const startY = loop ? from.y + from.height : from.y + from.height / 2;
			const endX = loop ? to.x + to.width / 2 : to.x - 3;
			const endY = loop ? to.y + to.height + 3 : to.y + to.height / 2;
			const dip = 130 + edges.length * 12;
			const traversed = from.status === 'done' && (loop ? (to.attempts ?? 0) > 1 : to.status !== 'pending');
			edges.push({
				id: `${source}:${target}`, from: from.name, to: to.name, kind: loop ? 'loopback' : 'forward',
				pathD: loop ? `M ${startX} ${startY} C ${startX} ${dip}, ${endX} ${dip}, ${endX} ${endY}` : `M ${startX} ${startY} L ${endX} ${endY}`,
				arrow: { x: endX, y: endY, angle: loop ? -Math.PI / 2 : 0 }, traversed, highlight: traversed
			});
		}
	}
	return { width: nodes.at(-1) ? nodes.at(-1)!.x + NODE_WIDTH + 24 : 0,
		height: nodes.length ? 140 + edges.length * 12 : 0, nodes, edges, stages: [] };
}
