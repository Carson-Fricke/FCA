import type { WorkflowDefinition, WorkflowPhase } from '$lib/types';

export interface GraphNode {
	name: string;
	status: 'done' | 'active' | 'pending';
	attempts?: number;
	waitingFor?: string[];
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

export const NODE_WIDTH = 168;
export const NODE_HEIGHT = 42;

/** Geometry is presentation-owned; only the fetched definition supplies legal edges. */
export function computeWorkflowGraph(phases: WorkflowPhase[], definition?: WorkflowDefinition | null): WorkflowGraphModel {
	const states = new Map(phases.map((phase) => [phase.name, phase]));
	const names = [...new Set([...(definition?.phases ?? []), ...states.keys()])];
	const nodes: GraphNode[] = names.map((name, index) => ({
		name, status: states.get(name)?.status ?? 'pending', attempts: states.get(name)?.attempts, waitingFor: states.get(name)?.waitingFor,
		x: 20 + index * (NODE_WIDTH + 36), y: 56, width: NODE_WIDTH, height: NODE_HEIGHT
	}));
	const byName = new Map(nodes.map((node) => [node.name, node]));
    const routes = Object.entries(definition?.routes ?? {}).flatMap(([source, routes]) =>
        Object.entries(routes).flatMap(([route, destinations]) => destinations.map((destination) => ({ source, route, destination, target: destination.split('.')[0] }))));
    const order = new Map(names.map((name, index) => [name, index]));
    const columns = new Map(names.map((name) => [name, 0]));
    for (const name of names) for (const edge of routes.filter((edge) => edge.source === name)) {
        if ((order.get(edge.target) ?? -1) > (order.get(name) ?? -1))
            columns.set(edge.target, Math.max(columns.get(edge.target) ?? 0, (columns.get(name) ?? 0) + 1));
    }
    const lanes = new Map<number, number>();
    for (const node of nodes) {
        const column = columns.get(node.name) ?? 0;
        const lane = lanes.get(column) ?? 0;
        node.x = 20 + column * (NODE_WIDTH + 36);
        node.y = 56 + lane * 88;
        lanes.set(column, lane + 1);
    }
	const edges: GraphEdge[] = [];
	for (const { source, route, target, destination } of routes) {
		const from = byName.get(source);
		if (!from) continue;
			const to = byName.get(target);
			if (!to) continue;
			const loop = to.x <= from.x;
			const startX = loop ? from.x + from.width / 2 : from.x + from.width;
			const startY = loop ? from.y + from.height : from.y + from.height / 2;
			const endX = loop ? to.x + to.width / 2 : to.x - 3;
			const endY = loop ? to.y + to.height + 3 : to.y + to.height / 2;
			const dip = 130 + edges.length * 12;
			const traversed = from.status === 'done' && (loop ? (to.attempts ?? 0) > 1 : to.status !== 'pending');
			edges.push({
				id: `${source}:${route}:${destination}`, label: route, from: from.name, to: to.name, kind: loop ? 'loopback' : 'forward',
				pathD: loop ? `M ${startX} ${startY} C ${startX} ${dip}, ${endX} ${dip}, ${endX} ${endY}` : `M ${startX} ${startY} L ${endX} ${endY}`,
				arrow: { x: endX, y: endY, angle: loop ? -Math.PI / 2 : Math.atan2(endY - startY, endX - startX) }, traversed, highlight: traversed
			});
	}
	return { width: nodes.length ? Math.max(...nodes.map((node) => node.x)) + NODE_WIDTH + 24 : 0,
		height: nodes.length ? Math.max(...nodes.map((node) => node.y)) + 84 + edges.length * 12 : 0, nodes, edges, stages: [] };
}
