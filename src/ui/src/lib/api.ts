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
	BackendKind,
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
	StatusSurface,
	Strategy,
	WidgetDefinition,
	WorkflowExecutionRecord,
	WorkspaceSnapshot,
	WorkspaceStateData,
	WorkspaceSummary
} from './types';

// ---------------------------------------------------------------------------
// Screener contract (thesis.screening HTTP surface, `/v1/workspaces/{ws}/screening`).
// The UX agent owns moving these into `types.ts` and rebuilding the panel;
// they are declared here so the client functions carry the real shapes today.
// ---------------------------------------------------------------------------

/** One declarative filter: a preset kind plus validated parameters. */
export interface ScreenerFilterSpec {
	kind: string;
	params?: Record<string, string | number | boolean>;
}

/** A saved screener definition document as served by the screening router. */
export interface ScreenerSavedDefinition {
	screener_id: string;
	definition: {
		schema_version: number;
		name: string;
		universe_revision: string;
		lookback_days: number;
		filters: ScreenerFilterSpec[];
	};
	definition_hash: string;
	artifact_path: string;
}

/** One surviving symbol with its evaluated fact sheet (string metrics). */
export interface ScreenerHitRow {
	rank: number;
	symbol: string;
	metrics: Record<string, string>;
}

/** One deterministic run result with full provenance. */
export interface ScreenerRunResult {
	schema_version: number;
	result_id: string;
	screener_id: string;
	definition_name: string;
	universe_revision: string;
	as_of: string;
	data_window_start: string;
	data_window_end: string;
	definition_hash: string;
	hits: ScreenerHitRow[];
}

/** Payload to save one screener definition. */
export interface ScreenerSaveInput {
	name: string;
	universe_revision?: string;
	lookback_days?: number;
	filters: ScreenerFilterSpec[];
}

/** Payload to run one screener at an explicit point-in-time as-of date. */
export interface ScreenerRunInput extends ScreenerSaveInput {
	as_of: string;
}

const buildBase: string = (
	(import.meta.env.PUBLIC_THESIS_API_BASE as string | undefined) ?? ''
).trim();
const forceFixtures = (import.meta.env.PUBLIC_THESIS_UI_FIXTURES as string | undefined) === 'true';
const bearerToken: string | null =
	(import.meta.env.PUBLIC_THESIS_API_BEARER_TOKEN as string | undefined) ?? null;

/** Session bearer for raw transports (SSE streams) that bypass apiFetch. */
export const apiBearerToken: string | null = bearerToken;

/** Guarded localStorage read: Node exposes a non-functional localStorage. */
function storedValue(key: string): string | null {
	try {
		if (!browser || typeof localStorage === 'undefined') return null;
		return localStorage.getItem(key);
	} catch {
		return null;
	}
}

/** Whether a hostname names the operator's own loopback interface. */
function isLoopbackHost(host: string): boolean {
	const bare = host.startsWith('[') && host.endsWith(']') ? host.slice(1, -1) : host;
	const normalized = bare.toLowerCase();
	return normalized === 'localhost' || normalized === '127.0.0.1' || normalized === '::1';
}

/** Whether a base URL points at the operator's own loopback interface. */
export function isLoopbackBase(url: string): boolean {
	const trimmed = url.trim();
	if (!trimmed) return false;
	try {
		return isLoopbackHost(new URL(trimmed).hostname);
	} catch {
		return false;
	}
}

/** A remote base is an absolute http(s) URL that is not loopback. */
function isRemoteBase(url: string): boolean {
	const trimmed = url.trim();
	return /^https?:\/\//i.test(trimmed) && !isLoopbackBase(trimmed);
}

/** True when this UI is served from the operator's own machine. */
export const isLocalHosting: boolean = browser ? isLoopbackHost(location.hostname) : false;

function stripSlash(url: string): string {
	return url.replace(/\/+$/, '');
}

const storedRemoteBase = (storedValue('thesis.azure_base') ?? '').trim();

/**
 * A loopback base is the *local* Thesis API, never Azure — even when it
 * arrives through the generic build-time `PUBLIC_THESIS_API_BASE`, or through a
 * stale `thesis.azure_base` value. Only a non-loopback absolute URL is remote.
 */
const localConfiguredBase: string = stripSlash(
	isLoopbackBase(storedRemoteBase)
		? storedRemoteBase
		: isLoopbackBase(buildBase)
			? buildBase
			: ''
);

/** The configured remote (Azure) API base; empty when none is configured. */
export const azureBaseUrl: string =
	stripSlash(isRemoteBase(storedRemoteBase) ? storedRemoteBase : '') ||
	stripSlash(isRemoteBase(buildBase) ? buildBase : '');

/** True when a remote (Azure) Thesis API base URL is configured. */
export const canUseRemoteApi: boolean = azureBaseUrl !== '';

const _storedBackend = storedValue('thesis.backend');

/** Honor a stored choice only when it is still valid for this build. */
function _storedKind(candidate: string | null): BackendKind | null {
	if (candidate === 'fixtures' || candidate === 'local') return candidate;
	if (candidate === 'azure' && canUseRemoteApi) return 'azure';
	return null;
}

/**
 * Resolve the active backend: an explicit stored choice wins, then a
 * loopback base (local), then a remote base (Azure), else fixtures. A base
 * configured for the build is Azure only when it is not loopback.
 */
export const backendKind: BackendKind =
	_storedKind(_storedBackend) ??
	(forceFixtures
		? 'fixtures'
		: localConfiguredBase
			? 'local'
			: canUseRemoteApi
				? 'azure'
				: 'fixtures');

/** True when the UI renders from bundled fixtures instead of a backend. */
export const fixtureMode: boolean = backendKind === 'fixtures';

/** True when any non-fixture backend is active. */
export const canUseRealApi: boolean = backendKind !== 'fixtures';

/**
 * The API base URL. `fixtures` resolves to '' (no fetching); `local` uses the
 * configured loopback base or '' for same-origin (dev proxy); `azure` uses the
 * configured remote base.
 */
export const apiBaseUrl: string =
	backendKind === 'azure'
		? azureBaseUrl
		: backendKind === 'local'
			? localConfiguredBase
			: '';

/** Human-readable label for a backend choice. */
export function backendLabel(kind: BackendKind = backendKind): string {
	return kind === 'fixtures' ? 'fixture data' : kind === 'local' ? 'local backend' : 'azure api';
}

/** True when a backend can be selected from the running UI. */
export function backendOptions(): {
	kind: BackendKind;
	label: string;
	detail: string;
	available: boolean;
	base: string;
}[] {
	return [
		{
			kind: 'fixtures',
			label: 'Fixtures',
			detail: 'Bundled sample data. No backend, fully offline.',
			available: true,
			base: ''
		},
		{
			kind: 'local',
			label: 'Local',
			detail: localConfiguredBase
				? 'Local Thesis API configured for this build.'
				: 'Local Thesis API, same-origin via the dev proxy.',
			available: isLocalHosting,
			base: localConfiguredBase
		},
		{
			kind: 'azure',
			label: 'Azure',
			detail: canUseRemoteApi
				? 'Remote Thesis API configured for this build.'
				: 'Enter a remote Thesis API base URL below.',
			available: true,
			base: azureBaseUrl
		}
	];
}

/**
 * Persist a backend choice and reload so every module re-resolves the base.
 * `azureBase` updates the stored remote URL when provided.
 */
export function setBackend(kind: BackendKind, azureBase?: string): void {
	if (!browser) return;
	localStorage.setItem('thesis.backend', kind);
	if (azureBase !== undefined) {
		const trimmed = azureBase.trim();
		if (trimmed) localStorage.setItem('thesis.azure_base', trimmed);
		else localStorage.removeItem('thesis.azure_base');
	}
	location.reload();
}

/** Backward-compatible fixture toggle used by the error and workspace pages. */
export function setFixtureMode(enabled: boolean): void {
	setBackend(
		enabled ? 'fixtures' : isLocalHosting ? 'local' : canUseRemoteApi ? 'azure' : 'local'
	);
}

export const uiEnvironment: string =
	(import.meta.env.PUBLIC_THESIS_UI_ENV as string | undefined) ?? 'dev';

/**
 * Single-operator actor id. Session-derived identity is not wired into the UI
 * yet, so every mutation attributes to this local operator, matching the
 * fixture records. The Thesis API requires an `actor_id` on messages and an
 * `owner_actor_id` on threads.
 */
export const localActorId = 'user-alex';

/** Non-2xx response from the Thesis API; carries the HTTP status. */
export class ThesisApiError extends Error {
	constructor(
		readonly status: number,
		readonly path: string
	) {
		super(`Thesis API ${status} on ${path}`);
		this.name = 'ThesisApiError';
	}
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
	const headers = new Headers(init?.headers);
	if (bearerToken) headers.set('Authorization', `Bearer ${bearerToken}`);
	if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
	const response = await fetch(`${apiBaseUrl}${path}`, { ...init, headers });
	if (!response.ok) {
		throw new ThesisApiError(response.status, path);
	}
	if (response.status === 204) return undefined as T;
	const text = await response.text();
	return (text ? JSON.parse(text) : undefined) as T;
}

/**
 * Fetch that resolves to `fallback` on a 404. Used for optional resources
 * (a chat's execution, a workspace-state document that has not been
 * persisted) and for the activation-consent surface, which the local
 * composition does not serve yet. Any other failure still throws so real
 * backend or configuration errors are not masked.
 */
async function apiFetchOptional<T>(path: string, fallback: T, init?: RequestInit): Promise<T> {
	try {
		return await apiFetch<T>(path, init);
	} catch (error) {
		if (error instanceof ThesisApiError && error.status === 404) return fallback;
		throw error;
	}
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
	screeners: Record<string, ScreenerSavedDefinition[]>;
	messages: Record<string, ChatMessage[]>;
	nextId: number;
} = {
	widgets: itemsOf(fixtures.widgets),
	screeners: {},
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
	return Promise.all([
		apiFetch<{
			overview: WorkspaceSnapshot['overview'];
			counts: WorkspaceSnapshot['counts'];
		}>(`/v1/workspaces/${encodeURIComponent(workspaceId)}/snapshot`),
		listWorkspaces()
	]).then(([snapshot, workspaces]) => ({
		// The workspace routes return the overview and counts; the catalog is
		// fetched separately so the shell's workspace switcher has its list.
		workspaces,
		overview: snapshot.overview,
		counts: snapshot.counts
	}));
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

export interface CreateChatInput {
	title?: string;
	ownerActorId?: string;
}

/** Derive a conversation title from the first message body. */
export function conversationTitleFrom(body: string): string {
	const firstLine = body.trim().split('\n', 1)[0]?.trim() ?? '';
	if (!firstLine) return 'New conversation';
	return firstLine.length > 60 ? `${firstLine.slice(0, 57)}…` : firstLine;
}

/**
 * Create a workspace conversation. The Thesis API persists the thread and
 * returns it; fixture mode appends an in-memory thread so the create flow is
 * exercised end to end without a backend.
 */
export function createChat(
	workspaceId: string,
	input: CreateChatInput = {}
): Promise<ChatThread> {
	const title = input.title?.trim() || 'New conversation';
	const owner = input.ownerActorId ?? localActorId;
	if (fixtureMode) {
		return wait(10).then(() => {
			const created: ChatThread = {
				chat_id: `chat-local-${store.nextId++}`,
				workspace_id: workspaceId,
				title,
				state: 'active',
				kind: 'chat',
				owner_actor_id: owner,
				created_at: new Date().toISOString(),
				updated_at: null,
				investigation_ids: [],
				strategy_ids: []
			};
			const doc = fixtures.chats[workspaceId] ?? { items: [] };
			doc.items.unshift(plain(created));
			fixtures.chats[workspaceId] = doc;
			store.messages[created.chat_id] = [];
			return created;
		});
	}
	return apiFetch<ChatThread>(`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats`, {
		method: 'POST',
		body: JSON.stringify({ title, owner_actor_id: owner })
	});
}

export function deleteChat(workspaceId: string, chatId: string): Promise<void> {
	if (fixtureMode) {
		return wait(10).then(() => {
			const doc = fixtures.chats[workspaceId];
			if (doc) {
				doc.items = doc.items.filter((chat) => chat.chat_id !== chatId);
			}
			delete store.messages[chatId];
		});
	}
	return apiFetch<void>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}`,
		{ method: 'DELETE' }
	);
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
	return apiFetch<ChatMessage & { steering?: RawSteering }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/messages`,
		{
			method: 'POST',
			body: JSON.stringify({
				actor_id: localActorId,
				body: input.body,
				attachment_ids: input.attachmentIds ?? [],
				steering_intent: input.steeringIntent ?? null,
				targets_execution_id: input.targetsExecutionId ?? null
			})
		}
	).then((raw) => ({
		message: raw,
		// The API returns the persisted message directly and, for a steering
		// message, nests the percolation under `steering`; normalize it to the
		// `PostMessageResult` shape the components consume.
		percolated: (raw.steering?.percolatedTo ?? []).map((target) => ({
			execution_id: target.executionId,
			intent_epoch: target.intentEpoch,
			steering_intent: raw.steering?.intent ?? null
		}))
	}));
}

interface RawSteering {
	intent: string | null;
	targetsExecutionId: string | null;
	percolatedTo: { executionId: string; intentEpoch: number }[];
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
	return apiFetchOptional<WorkflowExecutionRecord | null>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/execution`,
		null
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
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/deployments/orders`
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
	return apiFetch<StatusSurface<PortfolioSummary>>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/deployments/portfolio`
	);
}

export function getRiskDecisions(
	workspaceId: string,
	requestRef: string
): Promise<StatusSurface<RiskDecisionRecord[]>> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc =
				fixtures.riskDecisions[requestRef];
			if (!doc) return { state: 'unavailable', value: null, reason: 'not_found' };
			return { state: 'available', value: plain(doc.items), reason: null };
		});
	}
	return apiFetch<StatusSurface<RiskDecisionRecord[]>>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/deployments/risk-decisions?request_ref=${encodeURIComponent(requestRef)}`
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
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/dashboards/widgets`
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
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/dashboards/widgets/${encodeURIComponent(widgetId)}`,
		{ method: 'DELETE' }
	);
}

/**
 * Legacy fixture rows use a flat `{kind, op, value}` vocabulary; translate
 * them into the engine-native `{kind, params}` presets so fixture mode
 * exercises the real contract. The UX agent owns regenerating fixtures.
 */
function nativeFilter(kind: string, op: string | undefined, value: number): ScreenerFilterSpec {
	const preset =
		kind === 'price'
			? 'min_price'
			: kind === 'volume' || kind === 'liquidity'
				? 'avg_dollar_volume_min'
				: kind;
	if (op === '<' || op === '<=') {
		return { kind: preset, params: { max: value } };
	}
	return { kind: preset, params: { min: value } };
}

function toNativeFilters(legacy: unknown): ScreenerFilterSpec[] {
	if (!Array.isArray(legacy)) return [];
	return legacy
		.filter((row) => typeof row === 'object' && row !== null)
		.map((row) => {
			const record = row as { kind?: unknown; op?: unknown; value?: unknown };
			if (typeof record.value === 'number') {
				return nativeFilter(String(record.kind), record.op as string | undefined, record.value);
			}
			return { kind: String(record.kind ?? ''), params: {} };
		});
}

function legacyToSaved(row: Record<string, unknown>): ScreenerSavedDefinition {
	const id = String(row.id ?? 'scr');
	return {
		screener_id: id,
		definition: {
			schema_version: 1,
			name: String(row.name ?? id),
			universe_revision: 'universe:fixture',
			lookback_days: 20,
			filters: toNativeFilters(row.filters)
		},
		definition_hash: `fixture-${id}`,
		artifact_path: String(row.artifact_path ?? `screeners/${id}.json`)
	};
}

export function listScreeners(workspaceId: string): Promise<ScreenerSavedDefinition[]> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const legacy = plain(fixtures.screeners[workspaceId]?.items ?? []);
			const created = plain(store.screeners[workspaceId] ?? []);
			return [
				...legacy.map((row) => legacyToSaved(row as unknown as Record<string, unknown>)),
				...created
			];
		});
	}
	return apiFetch<{ items: ScreenerSavedDefinition[] }>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/screening`
	).then((r) => r.items);
}

export function saveScreener(
	workspaceId: string,
	input: ScreenerSaveInput
): Promise<ScreenerSavedDefinition> {
	if (fixtureMode) {
		return wait(15).then(() => {
			const created: ScreenerSavedDefinition = {
				screener_id: `scr-local-${store.nextId++}`,
				definition: {
					schema_version: 1,
					name: input.name,
					universe_revision: input.universe_revision ?? 'universe:fixture',
					lookback_days: input.lookback_days ?? 20,
					filters: input.filters
				},
				definition_hash: `fixture-${Date.now()}`,
				artifact_path: `screeners/definitions/${input.name
					.toLowerCase()
					.replace(/[^a-z0-9]+/g, '-')}.json`
			};
			store.screeners[workspaceId] = [...(store.screeners[workspaceId] ?? []), created];
			return created;
		});
	}
	return apiFetch<ScreenerSavedDefinition>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/screening`,
		{ method: 'POST', body: JSON.stringify(input) }
	);
}

export function runScreener(
	workspaceId: string,
	input: ScreenerRunInput
): Promise<ScreenerRunResult> {
	if (fixtureMode) {
		return wait(15).then(() => {
			const rows = plain(fixtures.screenerResults.items);
			return {
				schema_version: 1,
				result_id: 'fixture-run',
				screener_id: 'fixture',
				definition_name: input.name,
				universe_revision: input.universe_revision ?? 'universe:fixture',
				as_of: input.as_of,
				data_window_start: input.as_of,
				data_window_end: input.as_of,
				definition_hash: 'fixture-definition-hash',
				hits: rows.map((row, index) => ({
					rank: index + 1,
					symbol: row.symbol,
					metrics: {
						last_close: String(row.last),
						avg_volume: String(row.volume),
						avg_dollar_volume: String(row.avg_dollar_volume_20d)
					}
				}))
			};
		});
	}
	return apiFetch<ScreenerRunResult>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/screening/run`,
		{ method: 'POST', body: JSON.stringify(input) }
	);
}

/** Fetch one stored run result (with provenance) by its content id. */
export function getScreenerResult(
	workspaceId: string,
	resultId: string
): Promise<ScreenerRunResult> {
	if (fixtureMode) {
		return runScreener(workspaceId, {
			name: 'fixture-screen',
			as_of: new Date().toISOString().slice(0, 10),
			filters: []
		});
	}
	return apiFetch<ScreenerRunResult>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/screening/results/${encodeURIComponent(resultId)}`
	);
}

export function getWorkspaceState(workspaceId: string): Promise<WorkspaceStateData | null> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.workspaceState[workspaceId];
			return doc ? plain(doc) : null;
		});
	}
	return apiFetchOptional<WorkspaceStateData | null>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/state`,
		null
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

export function getConsentProposal(workspaceId: string): Promise<ConsentProposal | null> {
	if (fixtureMode) {
		return wait(5).then(() => plain(fixtures.consentProposal.proposal));
	}
	return apiFetchOptional<ConsentProposal | null>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/activation/consent/proposal`,
		null
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
		backend: backendKind,
		chatMessages: true,
		executionStream: true,
		portfolio: available,
		riskDecisions: available,
		artifacts: available
	}));
}
