/**
 * Typed Thesis API client.
 *
 * Two modes, selected by environment:
 *  - fixture mode (default): every request resolves from per-route JSON
 *    fixtures under `src/lib/fixtures/`, so the UI renders fully before
 *    every backend route exists;
 *  - real-fetch mode: the same typed calls hit the Thesis API (bearer
 *    `THESIS_API__BEARER_TOKEN` mode today; the UI holds only a session
 *    credential, never broker or gatekeeper credentials).
 *
 * Contracts preserved (see .github/architecture/thesis-ui-design.md):
 *  - POST /v1/workspaces/{ws}/chats/{chat}/messages returns the persisted
 *    message plus the percolation result (steering is a chat message);
 *  - GET /v1/workspaces/{ws}/executions/{id}/events/stream is consumed by
 *    the SSE client in `sse.ts` with Last-Event-ID resume;
 *  - status surfaces map to available/unavailable, never fabricated.
 */
import { browser } from '$app/environment';
import { fixtures } from './fixtures';
import type {
	BacktestDetail,
	BacktestSummaryRow,
	Capabilities,
	ChatMessage,
	ChatThread,
	ConsentProposal,
	DeploymentCard,
	ExecutionEvent,
	KnowledgeHit,
	OrderRecord,
	PortfolioSummary,
	PostMessageResult,
	RiskDecisionRecord,
	ScreenerDefinition,
	ScreenerResultRow,
	StatusSurface,
	Strategy,
	WidgetDefinition,
	WorkflowExecutionRecord,
	WorkspaceSnapshot,
	WorkspaceStateData,
	WorkspaceSummary
} from './types';

const base: string = (import.meta.env.PUBLIC_THESIS_API_BASE as string | undefined) ?? '';
const forceFixtures = (import.meta.env.PUBLIC_THESIS_UI_FIXTURES as string | undefined) === 'true';
const bearerToken: string | null =
	(import.meta.env.PUBLIC_THESIS_API_BEARER_TOKEN as string | undefined) ?? null;

/** True when a real Thesis API base URL is configured at build time. */
export const canUseRealApi: boolean = base.trim() !== '';

/**
 * Runtime fixture override persisted across reloads (localStorage
 * `thesis.fixtures`). It only takes effect client-side: 'true' forces
 * fixture mode even when a base URL is configured, 'false' restores the
 * default choice, and an absent value keeps the build-time default.
 */
function storedFixtureOverride(): string | null {
	// Node 26 exposes a non-functional localStorage without
	// --localstorage-file; guard the read so module import never throws.
	try {
		if (!browser || typeof localStorage === 'undefined') return null;
		return localStorage.getItem('thesis.fixtures');
	} catch {
		return null;
	}
}

/** True when the UI renders from fixtures instead of the Thesis API. */
export const fixtureMode: boolean =
	storedFixtureOverride() === 'true'
		? true
		: storedFixtureOverride() === 'false'
			? canUseRealApi
			: forceFixtures || !canUseRealApi;

/** The API base URL in real mode ('' in fixture mode). */
export const apiBaseUrl: string = fixtureMode ? '' : base.replace(/\/+$/, '');

/** Persist the runtime data-mode choice and reload so every caller sees it. */
export function setFixtureMode(enabled: boolean): void {
	if (!browser) return;
	localStorage.setItem('thesis.fixtures', enabled ? 'true' : 'false');
	location.reload();
}

export const uiEnvironment: string =
	(import.meta.env.PUBLIC_THESIS_UI_ENV as string | undefined) ?? 'dev';

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
	const headers = new Headers(init?.headers);
	if (bearerToken) headers.set('Authorization', `Bearer ${bearerToken}`);
	if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
	const response = await fetch(`${apiBaseUrl}${path}`, { ...init, headers });
	if (!response.ok) {
		throw new Error(`Thesis API ${response.status} on ${path}`);
	}
	return (await response.json()) as T;
}

/** Clone plain JSON-safe data without tripping over Svelte reactive proxies. */
function plain<T>(value: T): T {
	return JSON.parse(JSON.stringify(value)) as T;
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * In-memory working copy of the fixtures. Mutations performed in fixture
 * mode (posting a message, saving a screener, removing a widget) live only
 * for this page-load â€” the client never pretends to be durable state.
 */
function itemsOf<T>(doc: Record<string, { items: T[] }>): Record<string, T[]> {
	return Object.fromEntries(Object.entries(doc).map(([key, value]) => [key, plain(value.items)]));
}

const store: {
	widgets: Record<string, WidgetDefinition[]>;
	screeners: Record<string, ScreenerDefinition[]>;
	messages: Record<string, ChatMessage[]>;
	nextId: number;
} = {
	widgets: itemsOf(fixtures.widgets),
	screeners: itemsOf(fixtures.screeners),
	messages: itemsOf(fixtures.chatMessages),
	nextId: 1
};

export function listWorkspaces(): Promise<WorkspaceSummary[]> {
	if (fixtureMode) return wait(5).then(() => plain(fixtures.workspaces.items));
	return apiFetch<{ items: WorkspaceSummary[] }>('/v1/workspaces').then((r) => r.items);
}

function slugifyWorkspaceId(name: string): string {
	const slug = name
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/^-+|-+$/g, '');
	return slug || `workspace-${Date.now()}`;
}

function initialsFor(name: string): string {
	return name
		.split(/\s+/)
		.slice(0, 2)
		.map((word) => word[0]?.toUpperCase() ?? '')
		.join('');
}

export function createWorkspace(input: {
	name: string;
	description: string;
}): Promise<WorkspaceSummary> {
	if (fixtureMode) {
		return wait(15).then(() => {
			const created: WorkspaceSummary = {
				id: slugifyWorkspaceId(input.name),
				name: input.name,
				description: input.description,
				initials: initialsFor(input.name),
				created_at: new Date().toISOString()
			};
			fixtures.workspaces.items.push(plain(created));
			return created;
		});
	}
	return apiFetch<WorkspaceSummary>('/v1/workspaces', {
		method: 'POST',
		body: JSON.stringify(input)
	});
}

export function getSnapshot(workspaceId: string): Promise<WorkspaceSnapshot> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.workspaceSnapshot[workspaceId];
			if (doc) {
				return {
					workspaces: plain(fixtures.workspaces.items),
					overview: plain(doc.overview),
					counts: { ...doc.counts }
				};
			}
			// Locally created workspaces have no fixture document: synthesize
			// an empty overview so creation flows reach a working shell.
			const ws = fixtures.workspaces.items.find((w) => w.id === workspaceId);
			if (ws) {
				return {
					workspaces: plain(fixtures.workspaces.items),
					overview: {
						workspace: plain(ws),
						health: 'operational',
						activity: [],
						openChats: []
					},
					counts: { strategies: 0, chats: 0, backtests: 0, deployments: 0, dashboards: 0, screeners: 0 }
				};
			}
			throw new Error(`unknown workspace ${workspaceId}`);
		});
	}
	return apiFetch<WorkspaceSnapshot>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/snapshot`
	);
}

export function listChats(workspaceId: string): Promise<ChatThread[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.chats[workspaceId];
			return doc ? plain(doc.items) : [];
		});
	}
	return apiFetch<{ items: ChatThread[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats`
	).then((r) => r.items);
}

export function listMessages(workspaceId: string, chatId: string): Promise<ChatMessage[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = store.messages[chatId];
			if (!doc) throw new Error(`unknown chat ${chatId}`);
			return plain(doc);
		});
	}
	return apiFetch<{ items: ChatMessage[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/messages`
	).then((r) => r.items);
}

export interface PostMessageInput {
	body: string;
	attachmentIds?: string[];
	steeringIntent?: string | null;
	targetsExecutionId?: string | null;
}

export function postMessage(
	workspaceId: string,
	chatId: string,
	input: PostMessageInput
): Promise<PostMessageResult> {
	if (fixtureMode) {
		return wait(20).then(() => {
			const doc = store.messages[chatId];
			if (!doc) throw new Error(`unknown chat ${chatId}`);
			const message: ChatMessage = {
				message_id: `msg-local-${store.nextId++}`,
				chat_id: chatId,
				workspace_id: workspaceId,
				actor_id: 'user-alex',
				role: 'user',
				body: input.body,
				state: 'accepted',
				attachment_ids: input.attachmentIds ?? [],
				created_at: new Date().toISOString(),
				updated_at: null,
				execution_id: input.targetsExecutionId ?? null,
				intent_epoch: input.steeringIntent ? 1 : 0
			};
			doc.push(message);
			// Fixture percolation mirrors the API: steering bumps the active
			// execution's intent epoch once and records the ack.
			const percolated =
				input.steeringIntent || input.targetsExecutionId
					? [
							{
								execution_id: input.targetsExecutionId ?? activeExecutionId(chatId),
								intent_epoch: message.intent_epoch,
								steering_intent: input.steeringIntent ?? null
							}
						]
					: [];
			return { message, percolated };
		});
	}
	return apiFetch<PostMessageResult>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/messages`,
		{
			method: 'POST',
			body: JSON.stringify({
				body: input.body,
				attachment_ids: input.attachmentIds ?? [],
				steering_intent: input.steeringIntent ?? null,
				targets_execution_id: input.targetsExecutionId ?? null
			})
		}
	);
}

function activeExecutionId(chatId: string): string {
	const doc = fixtures.executions[chatId];
	return doc?.execution_id ?? '';
}

export function getExecution(
	workspaceId: string,
	chatId: string
): Promise<WorkflowExecutionRecord | null> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.executions[chatId];
			if (!doc?.execution_id) return null;
			return plain(doc);
		});
	}
	return apiFetch<WorkflowExecutionRecord | null>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/execution`
	);
}

/** One page of the durable event store (exclusive `afterCursor`). */
export function replayExecutionEvents(
	workspaceId: string,
	executionId: string,
	afterCursor: number
): Promise<ExecutionEvent[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.executionEvents[executionId];
			const items = doc ? doc.items : [];
			return plain(items).filter((e) => e.cursor > afterCursor);
		});
	}
	return apiFetch<{ items: ExecutionEvent[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/executions/${encodeURIComponent(executionId)}/events?after_cursor=${afterCursor}`
	).then((r) => r.items);
}

export function listStrategies(workspaceId: string): Promise<Strategy[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.strategies[workspaceId];
			return doc ? plain(doc.items) : [];
		});
	}
	return apiFetch<{ items: Strategy[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/strategies`
	).then((r) => r.items);
}

export function getStrategy(workspaceId: string, strategyId: string): Promise<Strategy | null> {
	return listStrategies(workspaceId).then((items) => items.find((s) => s.id === strategyId) ?? null);
}

export function listBacktests(workspaceId: string): Promise<BacktestSummaryRow[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.backtests[workspaceId];
			return doc ? plain(doc.items) : [];
		});
	}
	return apiFetch<{ items: BacktestSummaryRow[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/backtests`
	).then((r) => r.items);
}

export function getBacktest(workspaceId: string, backtestId: string): Promise<BacktestDetail> {
	if (fixtureMode) {
		return wait(10).then(() => {
			const doc =
				fixtures.backtestDetail[backtestId];
			const series =
				fixtures.backtestSeries[backtestId];
			if (!doc || !series) throw new Error(`unknown backtest ${backtestId}`);
			return { ...plain(doc), equity: plain(series.equity), trades: plain(series.trades) };
		});
	}
	return apiFetch<BacktestDetail>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/backtests/${encodeURIComponent(backtestId)}`
	);
}

export function listDeployments(workspaceId: string): Promise<DeploymentCard[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.deployments[workspaceId];
			return doc ? plain(doc.items) : [];
		});
	}
	return apiFetch<{ items: DeploymentCard[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/deployments`
	).then((r) => r.items);
}

export function listOrders(workspaceId: string): Promise<OrderRecord[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.orders[workspaceId];
			return doc ? plain(doc.items) : [];
		});
	}
	return apiFetch<{ items: OrderRecord[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/orders`
	).then((r) => r.items);
}

/** Harness-published portfolio snapshot: available or explicitly unavailable. */
export function getPortfolio(workspaceId: string): Promise<StatusSurface<PortfolioSummary>> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.portfolio[workspaceId];
			if (!doc) return { state: 'unavailable', value: null, reason: 'not_found' };
			return plain(doc);
		});
	}
	return apiFetch<StatusSurface<PortfolioSummary>>('/v1/runtime/portfolio');
}

export function getRiskDecisions(requestRef: string): Promise<StatusSurface<RiskDecisionRecord[]>> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc =
				fixtures.riskDecisions[requestRef];
			if (!doc) return { state: 'unavailable', value: null, reason: 'not_found' };
			return { state: 'available', value: plain(doc.items), reason: null };
		});
	}
	return apiFetch<StatusSurface<RiskDecisionRecord[]>>(
		`/v1/runtime/risk-decisions/${encodeURIComponent(requestRef)}`
	);
}

export function listWidgets(workspaceId: string): Promise<WidgetDefinition[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = store.widgets[workspaceId];
			return doc ? plain(doc) : [];
		});
	}
	return apiFetch<{ items: WidgetDefinition[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/widgets`
	).then((r) => r.items);
}

export function removeWidget(workspaceId: string, widgetId: string): Promise<void> {
	if (fixtureMode) {
		return wait(10).then(() => {
			const doc = store.widgets[workspaceId];
			if (doc) store.widgets[workspaceId] = doc.filter((w) => w.id !== widgetId);
		});
	}
	return apiFetch<void>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/widgets/${encodeURIComponent(widgetId)}`,
		{ method: 'DELETE' }
	);
}

export function listScreeners(workspaceId: string): Promise<ScreenerDefinition[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = store.screeners[workspaceId];
			return doc ? plain(doc) : [];
		});
	}
	return apiFetch<{ items: ScreenerDefinition[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/screeners`
	).then((r) => r.items);
}

export function saveScreener(
	workspaceId: string,
	definition: { name: string; filters: ScreenerDefinition['filters'] }
): Promise<ScreenerDefinition> {
	if (fixtureMode) {
		return wait(15).then(() => {
			const created: ScreenerDefinition = {
				id: `scr-local-${store.nextId++}`,
				workspace_id: workspaceId,
				name: definition.name,
				filters: plain(definition.filters),
				created_at: new Date().toISOString(),
				artifact_path: `screeners/${definition.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}.json`
			};
			store.screeners[workspaceId] = [...(store.screeners[workspaceId] ?? []), created];
			return created;
		});
	}
	return apiFetch<ScreenerDefinition>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/screeners`,
		{ method: 'POST', body: JSON.stringify(definition) }
	);
}

export function runScreener(): Promise<ScreenerResultRow[]> {
	if (fixtureMode) {
		return wait(15).then(() => plain(fixtures.screenerResults.items));
	}
	return apiFetch<{ items: ScreenerResultRow[] }>(
		'/v1/screener/run',
		{ method: 'POST', body: JSON.stringify({ universe: 'us-equity' }) }
	).then((r) => r.items);
}

export function getWorkspaceState(workspaceId: string): Promise<WorkspaceStateData> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.workspaceState[workspaceId];
			if (!doc) throw new Error(`unknown workspace ${workspaceId}`);
			return plain(doc);
		});
	}
	return apiFetch<WorkspaceStateData>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/state`
	);
}

export function searchKnowledge(query: string): Promise<KnowledgeHit[]> {
	if (fixtureMode) {
		return wait(10).then(() =>
			plain(fixtures.knowledgeSearch.items).filter((hit) =>
				`${hit.title} ${hit.excerpt}`.toLowerCase().includes(query.toLowerCase())
			)
		);
	}
	return apiFetch<{ items: KnowledgeHit[] }>(
		`/v1/knowledge/search?q=${encodeURIComponent(query)}`
	).then((r) => r.items);
}

export function getConsentProposal(workspaceId: string): Promise<ConsentProposal> {
	if (fixtureMode) {
		return wait(5).then(() => plain(fixtures.consentProposal.proposal));
	}
	return apiFetch<ConsentProposal>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/activation/consent/proposal`
	);
}

export function confirmConsent(
	workspaceId: string,
	confirmation: {
		bundleHash: string;
		environment: string;
		action: string;
		confirmationToken: string;
	}
): Promise<void> {
	if (fixtureMode) return wait(15).then(() => undefined);
	return apiFetch<void>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/activation/consent/confirm`,
		{ method: 'POST', body: JSON.stringify(confirmation) }
	);
}

export async function checkHealth(): Promise<boolean> {
	if (fixtureMode) return true;
	try {
		const response = await fetch(`${apiBaseUrl}/health`);
		return response.ok;
	} catch {
		return false;
	}
}

export async function checkAuth(): Promise<boolean> {
	if (fixtureMode) return true;
	try {
		const response = await fetch(`${apiBaseUrl}/v1/auth/check`, {
			headers: bearerToken ? { Authorization: `Bearer ${bearerToken}` } : undefined
		});
		return response.ok;
	} catch {
		return false;
	}
}

export function getCapabilities(): Promise<Capabilities> {
	const available: StatusSurface<true> = { state: 'available', value: true, reason: null };
	return wait(5).then(() => ({
		backend: fixtureMode ? 'fixtures' : 'thesis-api',
		chatMessages: true,
		executionStream: true,
		portfolio: available,
		riskDecisions: available,
		artifacts: available
	}));
}
