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
    const laidOut = new Set<string>();
    const layoutSplit = (id: string, baseY: number): { right: number; bottom: number } => {
        const split = definition?.splits?.[id];
        const from = byName.get(id);
        if (!split || !from) return { right: from?.x ?? 0, bottom: baseY };
        laidOut.add(id);
        let maxX = from.x;
        let nextY = baseY;
        Object.values(split.branches).forEach((entry) => {
            const branchY = nextY;
            let bottom = branchY;
            const seen = new Set<string>();
            const walk = (name: string, x: number): void => {
                if (name === split.join || seen.has(name)) return;
                seen.add(name);
                const node = byName.get(name);
                if (!node) return;
                node.x = x; node.y = branchY;
                if (definition?.splits?.[name]) {
                    const nested = layoutSplit(name, node.y);
                    maxX = Math.max(maxX, nested.right);
                    bottom = Math.max(bottom, nested.bottom);
                    for (const target of definition.transitions[definition.splits[name].join] ?? []) walk(target, nested.right + NODE_WIDTH + 36);
                } else {
                    maxX = Math.max(maxX, x);
                    for (const target of definition?.transitions[name] ?? []) walk(target, x + NODE_WIDTH + 36);
                }
            };
            walk(entry, from.x + NODE_WIDTH + 36);
            nextY = bottom + 88;
        });
        const join = byName.get(split.join);
        if (join) { join.x = maxX + NODE_WIDTH + 36; join.y = baseY; }
        return { right: join?.x ?? maxX, bottom: nextY - 88 };
    };
    for (const id of Object.keys(definition?.splits ?? {})) if (!laidOut.has(id)) layoutSplit(id, 56);
	const edges: GraphEdge[] = [];
	for (const [source, targets] of Object.entries(definition?.transitions ?? {})) {
		const from = byName.get(source);
		if (!from) continue;
		for (const target of targets) {
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
				id: `${source}:${target}`, from: from.name, to: to.name, kind: loop ? 'loopback' : 'forward',
				pathD: loop ? `M ${startX} ${startY} C ${startX} ${dip}, ${endX} ${dip}, ${endX} ${endY}` : `M ${startX} ${startY} L ${endX} ${endY}`,
				arrow: { x: endX, y: endY, angle: loop ? -Math.PI / 2 : Math.atan2(endY - startY, endX - startX) }, traversed, highlight: traversed
			});
		}
	}
	return { width: nodes.length ? Math.max(...nodes.map((node) => node.x)) + NODE_WIDTH + 24 : 0,
		height: nodes.length ? Math.max(...nodes.map((node) => node.y)) + 84 + edges.length * 12 : 0, nodes, edges, stages: [] };
}
