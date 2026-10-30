import { transcriptItems, nodeSummaries } from './read-fixtures';
import { expect, test } from '@playwright/test';
import chats from '../src/lib/fixtures/chats.json' with { type: 'json' };
import executions from '../src/lib/fixtures/executions.json' with { type: 'json' };
import workspaces from '../src/lib/fixtures/workspaces.json' with { type: 'json' };
import snapshot from '../src/lib/fixtures/workspace-snapshot.json' with { type: 'json' };
import backendTree from '../src/tests/backend-tree.json' with { type: 'json' };

const critics = ['implementation_critique', 'experimental_critique', 'alignment_critique'];
const ports = critics.map((phase) => `${phase}.input`);

for (const scenario of [
	{ name: 'shared full review', proposal: ports, investigation: [...ports].reverse(), dots: 1 },
	{ name: 'different review subsets', proposal: ports, investigation: ports.slice(1), dots: 2 },
	{ name: 'single destination routes', proposal: ports.slice(0, 1), investigation: ports.slice(1, 2), dots: 0 },
	{ name: 'strategy RCA and optimization', proposal: [], investigation: [], dots: 0, liveWorkflow: 'hypothesis-development' }
]) {
	test(`workflow junctions: ${scenario.name}`, async ({ page, request }) => {
		test.setTimeout(30_000);
		const chat = chats['alpha-lab'].items[0];
		const workspace = workspaces.items.find((item) => item.id === 'alpha-lab')!;
		const reviewDefinition = { workflow_id: 'junction-example', version: '1', title: 'Research review routes',
			phases: ['proposal', ...critics, 'collect_critiques', 'focused_critique', 'adjudicate', 'investigation', 'completed'], terminal_phases: ['completed'], inputs: {}, routes: {
				proposal: { critique: scenario.proposal, focused_critique: ['focused_critique.input'] },
				...Object.fromEntries(critics.map((phase) => [phase, { collect: [`collect_critiques.${phase}`] }])),
				collect_critiques: { adjudicate: ['adjudicate.input'] },
				focused_critique: { adjudicate: ['adjudicate.input'] },
				adjudicate: { proposal: ['proposal.input'], investigation: ['investigation.input'], completed: ['completed.input'] },
				investigation: { critique: scenario.investigation, focused_critique: ['focused_critique.input'], retry: ['investigation.input'] }, completed: {}
			} };
		const topology = 'liveWorkflow' in scenario ? await request.get(`/api/v1/workflows/${scenario.liveWorkflow}`) : null;
		if (topology) expect(topology.ok()).toBe(true);
		const definition = topology ? await topology.json() : reviewDefinition;
		const phases: string[] = definition.phases;
		const active = topology ? 'optimization' : 'investigation';
		const execution = { ...executions['chat-1'], workflow_id: definition.workflow_id,
			workflow_version: definition.version, active_phases: [active] };
		const node = (phase: string | null) => ({
			...backendTree.items[0], subAgentId: phase ?? 'workflow-record', chatId: chat.chat_id,
			parentSubAgentId: phase ? 'workflow-record' : null, returnsToSubAgentId: phase ? 'workflow-record' : null,
			role: phase?.replace(/_/g, ' ') ?? 'workflow-node', workflowExecutionId: execution.execution_id,
			phase, phaseExecutor: !!phase, phaseAttemptId: phase ? `${execution.execution_id}:${phase}:1` : null,
			status: phase === active ? 'running' : 'completed', acceptsInput: phase === active, children: []
		});
		await page.addInitScript(() => {
			localStorage.setItem('thesis.backend', 'local');
			const target = window as typeof window & { connectorPaths: Set<string> };
			target.connectorPaths = new Set();
			const NativePath = window.Path2D;
			window.Path2D = class extends NativePath {
				constructor(path?: string | Path2D) {
					super(path);
					if (typeof path === 'string') target.connectorPaths.add(path);
				}
			};
		});
		// Only external HTTP responses are scripted; rendering and navigation are real.
		await page.route('**/api/**', async (route) => {
			const path = new URL(route.request().url()).pathname;
			if (path.endsWith('/timeline/stream')) return route.fulfill({ contentType: 'text/event-stream', body: ': idle\n\n' });
			if (path.endsWith('/timeline')) return route.fulfill({ json: { items: [], nextBefore: null, after: 1 } });
			if (path.endsWith('/executions')) return route.fulfill({ json: { items: [execution], after: 1 } });
			if (path.endsWith('/outline')) return route.fulfill({ json: { items: [], after: 1, nextAfter: null } });
			if (path.endsWith('/sub_agents')) return route.fulfill({ json: { chatId: chat.chat_id, items: nodeSummaries([node(null), ...phases.filter((phase) => !definition.terminal_phases.includes(phase)).map(node)]) } });
			if (path.endsWith(`/workflows/${definition.workflow_id}`)) return route.fulfill({ json: definition });
			if (path.endsWith('/chats')) return route.fulfill({ json: { items: [chat] } });
			if (path.endsWith(`/chats/${chat.chat_id}`)) return route.fulfill({ json: chat });
			if (path.endsWith('/workspaces')) return route.fulfill({ json: { items: [workspace] } });
			if (path.endsWith(`/workspaces/${workspace.id}`)) return route.fulfill({ json: workspace });
			if (path.endsWith('/snapshot')) return route.fulfill({ json: snapshot['alpha-lab'] });
			return route.fulfill({ status: 404, json: { detail: 'Not part of this scenario' } });
		});
		await page.goto(`/w/${workspace.id}/conversations?chat=${chat.chat_id}`);
		const card = page.getByTestId('workflow-card');
		await expect(card.getByRole('heading', { name: definition.title })).toBeVisible();
		await expect(card.getByTestId('phase-step')).toHaveCount(phases.length - definition.terminal_phases.length);
		await expect(card.locator('[data-phase="completed"]')).toHaveCount(0);
		await expect(card.getByTestId('workflow-junction')).toHaveCount(scenario.dots);
		// Level arrows stay straight; vertical shifts curve within their lanes; returns stay shallow.
		await expect.poll(() => page.evaluate(() => {
			const paths = [...(window as typeof window & { connectorPaths: Set<string> }).connectorPaths];
			const boxes = [...document.querySelectorAll<HTMLElement>('[data-testid="phase-step"]')].map((node) => ({
				name: node.dataset.phase,
				x: node.offsetLeft + 1, y: node.offsetTop + 1,
				right: node.offsetLeft + node.offsetWidth - 1, bottom: node.offsetTop + node.offsetHeight - 1
			}));
			const junctions = [...document.querySelectorAll<HTMLElement>('[data-testid="workflow-junction"]')].map((node) => ({
				x: node.offsetLeft, y: node.offsetTop + node.offsetHeight / 2
			}));
			if (!paths.length) return ['No connectors painted'];
			const issues: string[] = [];
			for (const d of paths) {
				// Check the painted geometry: smooth tangents and a bend throughout each vertical transition.
				let cursor = { x: 0, y: 0 };
				let previous: { dx: number; dy: number } | null = null;
				let bendStart = cursor;
				let bend = '';
				const checkBend = () => {
					if (!bend || bendStart.y === cursor.y) { bend = ''; return; }
					const curve = document.createElementNS('http://www.w3.org/2000/svg', 'path');
					curve.setAttribute('d', `M ${bendStart.x} ${bendStart.y} ${bend}`);
					const length = curve.getTotalLength();
					const atHeight = (fraction: number) => {
						let low = 0, high = length;
						for (let step = 0; step < 24; step++) {
							const middle = (low + high) / 2;
							const point = curve.getPointAtLength(middle);
							if ((point.y - bendStart.y) / (cursor.y - bendStart.y) < fraction) low = middle;
							else high = middle;
						}
						return curve.getPointAtLength((low + high) / 2).x;
					};
					const quarter = atHeight(0.25), middle = atHeight(0.5), threeQuarters = atHeight(0.75);
					const extent = Math.max(bendStart.x, cursor.x, middle) - Math.min(bendStart.x, cursor.x, middle);
					const turn = middle < Math.min(bendStart.x, cursor.x) || middle > Math.max(bendStart.x, cursor.x);
					const centralWidth = Math.max(quarter, middle, threeQuarters) - Math.min(quarter, middle, threeQuarters);
					if (centralWidth < extent * (turn ? 0.04 : 0.24)) issues.push(`Vertical transition has a straight middle: ${d}`);
					bend = '';
				};
				for (const segment of d.matchAll(/([A-Z])([^A-Z]*)/g)) {
					const values = (segment[2].match(/-?\d*\.?\d+(?:e[+-]?\d+)?/gi) ?? []).map(Number);
					if (segment[1] === 'M') { cursor = { x: values[0], y: values[1] }; previous = null; continue; }
					let start: { dx: number; dy: number };
					let end: typeof start;
					if (segment[1] === 'H') {
						checkBend();
						if (Math.abs(values[0] - cursor.x) < 0.000001) continue;
						start = end = { dx: values[0] - cursor.x, dy: 0 };
						cursor = { x: values[0], y: cursor.y };
					} else if (segment[1] === 'C') {
						if (!bend) bendStart = cursor;
						bend += segment[0];
						const dx = 3 * (values[0] - cursor.x), dy = 3 * (values[1] - cursor.y);
						const ex = 3 * (values[4] - values[2]), ey = 3 * (values[5] - values[3]);
						start = { dx, dy };
						end = { dx: ex, dy: ey };
						cursor = { x: values[4], y: values[5] };
					} else { issues.push(`Unsupported connector geometry: ${d}`); break; }
					const alignment = previous ? (previous.dx * start.dx + previous.dy * start.dy) / (Math.hypot(previous.dx, previous.dy) * Math.hypot(start.dx, start.dy)) : 1;
					if (!Number.isFinite(alignment) || alignment < 0.999999) {
						issues.push(`Connector bends abruptly at a join: ${d}`); break;
					}
					previous = end;
				}
				checkBend();
				if (previous && (previous.dx <= 0 || Math.abs(previous.dy) > 0.000001)) issues.push(`Connector must approach its arrowhead horizontally: ${d}`);
				const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
				path.setAttribute('d', d);
				const length = path.getTotalLength();
				const start = path.getPointAtLength(0);
				const shaftEnd = path.getPointAtLength(length);
				// Arrowhead sides are 7px at 30 degrees; the shaft ends at the base center.
				const end = { x: shaftEnd.x + 7 * Math.cos(Math.PI / 6), y: shaftEnd.y };
				const forward = end.x > start.x;
				const source = boxes.find((box) => Math.abs(start.x - box.right - 1) < 0.1 && start.y > box.y && start.y < box.bottom);
				const target = boxes.find((box) => Math.abs(end.x - box.x + 4) < 0.1 && Math.abs(end.y - (box.y + box.bottom) / 2) < 0.1);
				const junction = junctions.some((dot) => Math.abs(end.x - dot.x) < 0.1 && Math.abs(end.y - dot.y) < 0.1);
				if (!target && !junction) issues.push(`Connector shaft must stop at the arrowhead base: ${d}`);
				if (!forward) {
					const departure = path.getPointAtLength(Math.min(1, length));
					if (!source || departure.x <= start.x) issues.push(`Loopback must leave the right output edge: ${d}`);
					const arrival = path.getPointAtLength(Math.max(0, length - 1));
					if ((!target && !junction) || arrival.x >= shaftEnd.x) issues.push(`Loopback must enter the left input edge: ${d}`);
				}
				for (let step = 1; step < 128; step++) {
					const point = path.getPointAtLength(length * step / 128);
					if (forward && (point.y < Math.min(start.y, end.y) - 0.1 || point.y > Math.max(start.y, end.y) + 0.1)) {
						issues.push(`Forward arrow leaves its lanes: ${d}`); break;
					}
					if (!forward && point.y > Math.max((source?.bottom ?? start.y) + 33, end.y) + 0.1) {
						issues.push(`Tall loopback: ${d}`); break;
					}
					if (!forward && point.x > start.x + 12.1) { issues.push(`Loopback overshoots the source column: ${d}`); break; }
					if (!forward && end.y < start.y && point.y > start.y + 0.1) { issues.push(`Upward loopback detours down: ${d}`); break; }
					const hit = boxes.find((box) => point.x > box.x && point.x < box.right && point.y > box.y && point.y < box.bottom);
					if (hit) { issues.push(`Connector crosses ${hit.name}: ${d}`); break; }
				}
				if (forward && start.y !== end.y) {
					const quarter = path.getPointAtLength(length / 4);
					const chord = Math.hypot(end.x - start.x, end.y - start.y);
					const curve = Math.abs((end.x - start.x) * (quarter.y - start.y) - (end.y - start.y) * (quarter.x - start.x)) / chord;
					if (curve < 0.5) issues.push(`Vertical shift is straight: ${d}`);
				}
			}
			return issues;
		})).toEqual([]);
		// The late topology must refit the initially observed execution frontier.
		const graph = card.getByTestId('workflow-phase-stepper');
		await expect.poll(async () => {
			const viewport = (await graph.boundingBox())!;
			const boxes = await card.getByTestId('phase-step').evaluateAll((nodes) => nodes.map((node) => {
				const bounds = node.getBoundingClientRect();
				return { x: bounds.x, y: bounds.y, right: bounds.right, bottom: bounds.bottom };
			}));
			return boxes.every((bounds) => bounds.x >= viewport.x && bounds.y >= viewport.y &&
				bounds.right <= viewport.x + viewport.width && bounds.bottom <= viewport.y + viewport.height);
		}).toBe(true);
		if (topology) {
			await card.screenshot({ path: test.info().outputPath('strategy-repair-loopbacks.png') });
			await graph.focus();
			for (let step = 0; step < 5; step++) await page.keyboard.press('+');
			const rca = (await card.locator('[data-phase="strategy_rca"]').boundingBox())!;
			const optimization = (await card.locator('[data-phase="optimization"]').boundingBox())!;
			const viewport = (await graph.boundingBox())!;
			const x = Math.max(viewport.x, Math.min(rca.x, optimization.x) - 24);
			const y = Math.max(viewport.y, Math.min(rca.y, optimization.y) - 16);
			const right = Math.min(viewport.x + viewport.width, Math.max(rca.x + rca.width, optimization.x + optimization.width) + 24);
			const bottom = Math.min(viewport.y + viewport.height, Math.max(rca.y + rca.height, optimization.y + optimization.height) + 16);
			await page.screenshot({ path: test.info().outputPath('strategy-repair-detail.png'), clip: { x, y, width: right - x, height: bottom - y } });
		}
		if (scenario.dots) {
			const dot = card.getByRole('img', { name: /Parallel route to/ }).first();
			await expect(dot).toBeVisible();
			await card.screenshot({ path: test.info().outputPath('review-junctions.png') });
			const proposal = card.locator('[data-phase="proposal"]');
			const critique = card.locator('[data-phase="implementation_critique"]');
			const dotBounds = (await dot.boundingBox())!;
			const proposalBounds = (await proposal.boundingBox())!;
			const critiqueBounds = (await critique.boundingBox())!;
			expect(dotBounds.x).toBeGreaterThan(proposalBounds.x + proposalBounds.width);
			expect(dotBounds.x + dotBounds.width).toBeLessThan(critiqueBounds.x);
			expect(dotBounds.width).toBeGreaterThan(0);
			await graph.focus();
			await page.keyboard.press('+');
			await expect.poll(async () => (await dot.boundingBox())!.width).toBeGreaterThan(dotBounds.width);
			const zoomed = (await dot.boundingBox())!;
			await page.keyboard.press('ArrowRight');
			await expect.poll(async () => (await dot.boundingBox())!.x).toBeLessThan(zoomed.x);
			const url = page.url();
			await dot.click({ force: true });
			await expect(page).toHaveURL(url);
		}
		// Junctions never replace real phase controls or acquire drill-in behavior.
		const enterPhase = topology ? 'strategy_rca' : 'implementation_critique';
		await card.locator(`[data-phase="${enterPhase}"]`).focus();
		await page.keyboard.press('Enter');
		await expect(page.getByTestId('scope-crumb').last()).toHaveText(enterPhase.replace(/_/g, ' '));
	});
}
