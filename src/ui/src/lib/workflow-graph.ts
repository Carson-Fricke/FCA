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

export interface GraphJunction {
	id: string;
	destinations: string[];
	x: number;
	y: number;
	radius: number;
	highlight: boolean;
}

export interface WorkflowGraphModel {
	width: number;
	height: number;
	nodes: GraphNode[];
	edges: GraphEdge[];
	junctions: GraphJunction[];
	stages: Array<{ name: string; x: number; y: number; width: number; height: number; headerY: number }>;
}

export const NODE_WIDTH = 168;
export const NODE_HEIGHT = 42;
export const ARROW_HEAD_LENGTH = 7;
export const ARROW_HEAD_HALF_ANGLE = Math.PI / 6;
const ARROW_HEAD_DEPTH = ARROW_HEAD_LENGTH * Math.cos(ARROW_HEAD_HALF_ANGLE);
const ROW_TOP = 56;
const ROW_SPACING = 88;

/**
 * Owns phase and connector geometry derived only from the fetched definition.
 * Terminal states and their transitions are omitted from the displayed graph.
 * Shared fan-out junctions are drawing points, never phases or execution state.
 * Runtime routing and phase navigation remain outside this pure projection.
 */
export function computeWorkflowGraph(phases: WorkflowPhase[], definition?: WorkflowDefinition | null): WorkflowGraphModel {
	const states = new Map(phases.map((phase) => [phase.name, phase]));
	const terminals = new Set(definition?.terminal_phases ?? []);
	const names = [...new Set([...(definition?.phases ?? []), ...states.keys()])].filter((name) => !terminals.has(name));
	const nodes: GraphNode[] = names.map((name) => ({
		name, status: states.get(name)?.status ?? 'pending', attempts: states.get(name)?.attempts, waitingFor: states.get(name)?.waitingFor,
		x: 0, y: ROW_TOP, width: NODE_WIDTH, height: NODE_HEIGHT
	}));
	const byName = new Map(nodes.map((node) => [node.name, node]));
	const routes = Object.entries(definition?.routes ?? {}).flatMap(([source, routes]) =>
		Object.entries(routes).map(([route, destinations]) => ({ source, route,
			destinations: [...new Set(destinations)].filter((destination) => byName.has(destination.split('.')[0]))
		}))).filter(({ source, destinations }) => byName.has(source) && destinations.length > 0);
	const order = new Map(names.map((name, index) => [name, index]));
	const columns = new Map(names.map((name) => [name, 0]));
	for (const name of names) for (const route of routes.filter((route) => route.source === name)) {
		for (const destination of route.destinations) {
			const target = destination.split('.')[0];
			if ((order.get(target) ?? -1) > (order.get(name) ?? -1))
				columns.set(target, Math.max(columns.get(target) ?? 0, (columns.get(name) ?? 0) + 1));
		}
	}

	// A shared dot represents an exact set of simultaneous input destinations.
	// Different choices, subsets and input ports must retain separate connectors.
	const fanouts = new Map<string, { destinations: string[]; column: number }>();
	const fanoutKey = (destinations: string[]) => JSON.stringify([...destinations].sort());
	for (const { destinations } of routes) {
		if (new Set(destinations.map((destination) => destination.split('.')[0])).size < 2) continue;
		fanouts.set(fanoutKey(destinations), { destinations,
			column: Math.min(...destinations.map((destination) => columns.get(destination.split('.')[0])!)) });
	}
	const lanes = new Map<number, number>();
	for (const node of nodes) {
		const column = columns.get(node.name) ?? 0;
		const lane = lanes.get(column) ?? 0;
		node.y = ROW_TOP + lane * ROW_SPACING;
		lanes.set(column, lane + 1);
	}
	// Give vertical transitions enough horizontal travel to show their full Bézier shape.
	const shiftWidth = (rise: number) => Math.max(36, Math.ceil(Math.abs(rise) * 0.75) + 16);
	const columnGaps = new Map<number, number>();
	const junctions = new Map<string, GraphJunction>();
	const junctionLanes = new Map<number, number>();
	for (const [key, { destinations, column }] of fanouts) {
		const targets = destinations.map((destination) => byName.get(destination.split('.')[0])!);
		const lane = junctionLanes.get(column) ?? 0;
		junctionLanes.set(column, lane + 1);
		const y = (Math.min(...targets.map((node) => node.y)) + Math.max(...targets.map((node) => node.y)) + NODE_HEIGHT) / 2;
		const offset = shiftWidth(Math.max(...targets.map((node) => Math.abs(y - node.y - NODE_HEIGHT / 2)))) + 14 + lane * 20;
		junctions.set(key, { id: `junction:${key}`, destinations,
			x: -offset, y, radius: 5, highlight: false });
		columnGaps.set(column, Math.max(columnGaps.get(column) ?? 36, offset + 20));
	}
	for (const { source, destinations } of routes) {
		const from = byName.get(source)!;
		const junction = junctions.get(fanoutKey(destinations));
		for (const destination of destinations) {
			const to = byName.get(destination.split('.')[0])!;
			const column = junction ? fanouts.get(fanoutKey(destinations))!.column : columns.get(to.name)!;
			if (column <= columns.get(source)!) continue;
			const gap = shiftWidth((junction?.y ?? to.y + NODE_HEIGHT / 2) - from.y - NODE_HEIGHT / 2);
			columnGaps.set(column, Math.max(columnGaps.get(column) ?? 36, gap + (junction ? -junction.x + junction.radius : 0)));
		}
	}
	const columnX = new Map<number, number>();
	let nextX = 0;
	for (let column = 0; column <= Math.max(0, ...columns.values()); column++) {
		nextX += column === 0 ? Math.max(20, columnGaps.get(column) ?? 0) : columnGaps.get(column) ?? 36;
		columnX.set(column, nextX);
		nextX += NODE_WIDTH;
	}
	for (const node of nodes) node.x = columnX.get(columns.get(node.name)!)!;
	for (const [key, junction] of junctions) junction.x += columnX.get(fanouts.get(key)!.column)!;
	const bottom = nodes.length ? Math.max(...nodes.map((node) => node.y + node.height)) : 0;
	let graphBottom = bottom;
	const returnLanes = new Map<number, number>();
	const edges: GraphEdge[] = [];
	const traversedTo = (from: GraphNode, to: GraphNode) =>
		from.status === 'done' && (to.x <= from.x ? (to.attempts ?? 0) > 1 : to.status !== 'pending');
	function smoothShift(startX: number, startY: number, endX: number, endY: number, turnX?: number): string {
		// One cubic spreads the bend across the full rise, with horizontal endpoint tangents.
		// A return's controls sit beyond its endpoints to form a compact, continuous U-turn.
		const handleX = turnX ?? (startX + endX) / 2;
		return `C ${handleX} ${startY}, ${handleX} ${endY}, ${endX} ${endY}`;
	}
	function forwardPath(startX: number, startY: number, endX: number, endY: number): Pick<GraphEdge, 'pathD' | 'arrow'> {
		const arrow = { x: endX, y: endY, angle: 0 };
		const shaftEndX = endX - ARROW_HEAD_DEPTH;
		if (startY === endY) return { pathD: `M ${startX} ${startY} H ${shaftEndX}`, arrow };
		let pathD = `M ${startX} ${startY}`;
		let x = startX;
		let y = startY;
		const columns = [...new Set(nodes.filter((node) => node.x >= startX && node.x + node.width < endX)
			.map((node) => node.x))].sort((a, b) => a - b);
		for (const column of columns) {
			// A skipped column may occupy the source lane. Shift into the clear row gap before crossing it.
			if (nodes.some((node) => node.x === column && y > node.y && y < node.y + node.height)) {
				const firstGap = ROW_TOP + (ROW_SPACING + NODE_HEIGHT) / 2;
				const bridgeY = firstGap + Math.max(0, Math.round(((y + endY) / 2 - firstGap) / ROW_SPACING)) * ROW_SPACING;
				pathD += ` H ${x} ${smoothShift(x, y, column - 4, bridgeY)}`;
				y = bridgeY;
			}
			x = column + NODE_WIDTH + 4;
			pathD += ` H ${x}`;
		}
		pathD += ` ${smoothShift(x, y, shaftEndX, endY)}`;
		return { pathD, arrow };
	}
	function connect(from: GraphNode, to: GraphNode | GraphJunction, id: string, label: string, traversed: boolean): void {
		const junction = 'radius' in to;
		const loop = to.x <= from.x;
		const startX = from.x + from.width;
		const startY = from.y + from.height / 2;
		const endX = junction ? to.x - to.radius : to.x - 3;
		const endY = junction ? to.y : to.y + to.height / 2;
		const shaftEndX = endX - ARROW_HEAD_DEPTH;
		// Upward returns go over their source, including stacked phases in one column.
		// Every return approaches the same left input edge used by forward connectors.
		const rowBottom = from.y + from.height;
		const gapBottom = endY < startY ? from.y - ROW_SPACING + NODE_HEIGHT : rowBottom;
		const lane = returnLanes.get(gapBottom) ?? 0;
		const railY = gapBottom + 8 + Math.min(lane, 6) * 4;
		const returnX = Math.max(4, endX - 9);
		const returnBendX = Math.max(0, returnX - Math.min(28, Math.max(9, Math.abs(railY - endY) / 2)));
		if (loop) {
			returnLanes.set(gapBottom, lane + 1);
			graphBottom = Math.max(graphBottom, railY);
		}
		edges.push({ id, label, from: from.name, to: junction ? to.id : to.name,
			kind: loop ? 'loopback' : 'forward',
			...(loop ? {
				pathD: `M ${startX} ${startY} ${smoothShift(startX, startY, startX, railY, startX + 12)} H ${returnX} ${smoothShift(returnX, railY, shaftEndX, endY, returnBendX)}`,
				arrow: { x: endX, y: endY, angle: 0 }
			} : forwardPath(startX, startY, endX, endY)),
			traversed, highlight: traversed });
	}
	for (const { source, route, destinations } of routes) {
		const from = byName.get(source)!;
		const junction = junctions.get(fanoutKey(destinations));
		if (junction) {
			const traversed = destinations.some((destination) => traversedTo(from, byName.get(destination.split('.')[0])!));
			junction.highlight ||= traversed;
			connect(from, junction, `${source}:${route}:${junction.id}`, route, traversed);
		} else for (const destination of destinations) {
			const to = byName.get(destination.split('.')[0])!;
			connect(from, to, `${source}:${route}:${destination}`, route, traversedTo(from, to));
		}
	}
	for (const [key, junction] of junctions) for (const destination of junction.destinations) {
		const to = byName.get(destination.split('.')[0])!;
		const startX = junction.x + junction.radius;
		const startY = junction.y;
		const endX = to.x - 3;
		const endY = to.y + to.height / 2;
		const traversed = routes.some((route) => fanoutKey(route.destinations) === key && traversedTo(byName.get(route.source)!, to));
		edges.push({ id: `${junction.id}:${destination}`, from: junction.id, to: to.name, kind: 'forward',
			...forwardPath(startX, startY, endX, endY), traversed, highlight: traversed });
	}
	return { width: nodes.length ? Math.max(...nodes.map((node) => node.x)) + NODE_WIDTH + 24 : 0,
		height: nodes.length ? graphBottom + 24 : 0, nodes, edges, junctions: [...junctions.values()], stages: [] };
}
