/**
 * Typed Thesis API client.
 *
 * Two modes, selected by environment:
 *  - fixture mode (default): every request resolves from per-route JSON
 *    fixtures under `src/lib/fixtures/`, so the UI renders fully before
 *    every backend route exists;
 *  - real-fetch mode: the same typed calls hit the Thesis API through the
 *    same-origin `/api` base that the Static Web Apps linked backend proxies
 *    to the internal Container App. The browser carries no shared API
 *    credential; authentication moves to an API-issued session cookie
 *    (see ADR-001 and T306/T307).
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
import { fetchJson, ThesisApiError } from './transport';
import { fixtureTimeline } from './fixture-timeline';
import { decodeWire } from './wire';
import type { TimelinePageWire, CreateMessageResponseWire, SubAgentNodeWire, ChatThreadWire, NodeInputReceiptWire } from './generated';
export { ThesisApiError } from './transport';
import type {
	ArtifactEntry,
	BackendKind,
	BacktestDetail,
	BacktestLaunchResult,
	BacktestRunCard,
	BacktestSummaryRow,
	ChatMessage,
	ChatThread,
	ConsentProposal,
	DeploymentCard,
	ExecutionNodeRef,
	HarnessArtifactManifest,
	HarnessJobResult,
	HarnessJobStatus,
	HarnessRunRecord,
	HarnessStatus,
	KnowledgeHit,
	LaunchBacktestInput,
	OrderRecord,
	PortfolioSummary,
	PostMessageResult,
	RiskDecisionRecord,
	ScreenerDefinition,
	ScreenerRunInput,
	ScreenerSaveInput,
	ScreeningResult,
	StatusSurface,
	Strategy,
	StrategyStatus,
	SubAgentNode,
	WidgetDefinition,
	WorkflowExecutionRecord,
	WorkflowDefinition,
	WorkspaceSnapshot,
	WorkspaceStateData,
	WorkspaceSummary
} from './types';

/**
 * The same-origin API prefix the shipped SPA targets. Azure Static Web Apps'
 * linked backend proxies `/api/*` to the internal Thesis Container App, so the
 * browser only ever calls its own origin and no API credential is baked into
 * the bundle. A build may override the base with the non-secret
 * `PUBLIC_THESIS_API_BASE` (an absolute loopback URL or a relative path).
 */
export const SAME_ORIGIN_API_BASE = '/api';

const buildBase: string = (
	(import.meta.env.PUBLIC_THESIS_API_BASE as string | undefined) ?? ''
).trim();
const forceFixtures = (import.meta.env.PUBLIC_THESIS_UI_FIXTURES as string | undefined) === 'true';

/** Whether a base value is a relative path on the current origin (`/api`). */
function isRelativeBase(url: string): boolean {
	const trimmed = url.trim();
	return trimmed.startsWith('/') && !trimmed.startsWith('//');
}

/** Whether the build pins a same-origin API path through `PUBLIC_THESIS_API_BASE`. */
const hasSameOriginOverride: boolean = isRelativeBase(buildBase);

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

/**
 * The same-origin API base. Defaults to the linked-backend prefix `/api`; a
 * relative `PUBLIC_THESIS_API_BASE` (e.g. `/api/v2`) overrides it.
 */
const sameOriginBase: string = stripSlash(
	hasSameOriginOverride ? buildBase : SAME_ORIGIN_API_BASE
);

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
 * loopback base or a relative same-origin override (local), then a remote
 * base (Azure), else fixtures. A base configured for the build is Azure only
 * when it is not loopback.
 */
export const backendKind: BackendKind =
	_storedKind(_storedBackend) ??
	(forceFixtures
		? 'fixtures'
		: localConfiguredBase || hasSameOriginOverride
			? 'local'
			: canUseRemoteApi
				? 'azure'
				: 'fixtures');

/** True when the UI renders from bundled fixtures instead of a backend. */
export const fixtureMode: boolean = backendKind === 'fixtures';

/** True when any non-fixture backend is active. */
export const canUseRealApi: boolean = backendKind !== 'fixtures';

/**
 * The API base URL. `local` uses a configured loopback base or the
 * same-origin `/api` prefix; `azure` uses the configured remote base. In
 * fixture mode no request is issued, so the same-origin base is inert.
 */
export const apiBaseUrl: string =
	backendKind === 'azure' ? azureBaseUrl : localConfiguredBase || sameOriginBase;

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
				: 'Local Thesis API, same-origin through the linked backend.',
			available: isLocalHosting,
			base: localConfiguredBase || sameOriginBase
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
	workflowDefinitions.clear();
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

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
	return fetchJson<T>(apiBaseUrl, path, init);
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
 * Deterministic fixture-only content digest (64 lowercase hex).
 *
 * Fixture mode has no crypto dependency, so definitions saved without a
 * backend get a stable FNV-1a expansion of their exact content: identical
 * content addresses identically, which keeps the definition id, the saved
 * list, and a fixture run's provenance coherent. It is not SHA-256 and never
 * leaves fixture mode; the Thesis definition hasher stays authoritative.
 */
function fixtureDigest(content: string): string {
	let digest = '';
	let state = 0x811c9dc5;
	for (let round = 0; round < 4; round += 1) {
		for (let index = 0; index < content.length; index += 1) {
			state ^= content.charCodeAt(index);
			state = Math.imul(state, 0x01000193) >>> 0;
		}
		state = (state ^ Math.imul(round + 1, 0x9e3779b9)) >>> 0;
		digest += state.toString(16).padStart(8, '0');
	}
	return digest;
}

/**
 * In-memory working copy of the fixtures. Mutations performed in fixture
 * mode (posting a message, removing a widget) live only
 * for this page-load â€” the client never pretends to be durable state.
 */
function itemsOf<T>(doc: Record<string, { items: T[] }>): Record<string, T[]> {
	return Object.fromEntries(Object.entries(doc).map(([key, value]) => [key, plain(value.items)]));
}

const store: {
	widgets: Record<string, WidgetDefinition[]>;
	screeners: Record<string, ScreenerDefinition[]>;
	screenerResults: Record<string, ScreeningResult[]>;
	messages: Record<string, ChatMessage[]>;
	nextId: number;
} = {
	widgets: itemsOf(fixtures.widgets),
	screeners: itemsOf(fixtures.screeners),
	screenerResults: itemsOf(fixtures.screenerResults),
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

/**
 * Fetch one workspace summary. This is the render gate for a workspace route:
 * it proves the workspace exists and carries its name without loading counts or
 * activity. The heavier snapshot is fetched after the shell renders.
 */
export function getWorkspace(workspaceId: string): Promise<WorkspaceSummary> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const summary = fixtures.workspaces.items.find((item) => item.id === workspaceId);
			if (!summary) throw new Error(`unknown workspace ${workspaceId}`);
			return plain(summary);
		});
	}
	return apiFetch<WorkspaceSummary>(`/v1/workspaces/${encodeURIComponent(workspaceId)}`);
}

export function getSnapshot(workspaceId: string): Promise<WorkspaceSnapshot> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.workspaceSnapshot[workspaceId];
			if (doc) {
				return {
					workspaces: plain(fixtures.workspaces.items),
					overview: plain(doc.overview),
					counts: { ...doc.counts, chats: fixtures.chats[workspaceId]?.items.length ?? 0 }
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
					counts: {
						strategies: 0,
						chats: 0,
						backtests: 0,
						deployments: 0,
						dashboards: 0,
						screeners: 0
					}
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

export function listChats(workspaceId: string, signal?: AbortSignal): Promise<ChatThread[]> {
	const path = `/v1/workspaces/${encodeURIComponent(workspaceId)}/chats`;
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.chats[workspaceId];
			return decodeWire('ChatThreadListWire', plain(doc ?? { items: [] }), path).items.map(toChatThread);
		});
	}
	return apiFetch<unknown>(path, { signal }).then((raw) => decodeWire('ChatThreadListWire', raw, path).items.map(toChatThread));
}

function toChatThread(raw: ChatThreadWire): ChatThread {
	const investigation_ids = raw.links.investigation_ids ?? [];
	return { ...raw, kind: investigation_ids.length ? 'investigation' : 'chat',
		investigation_ids, strategy_ids: raw.links.strategy_ids ?? [] };
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
	input: CreateChatInput = {},
	signal?: AbortSignal
): Promise<ChatThread> {
	const title = input.title?.trim() || 'New conversation';
	const owner = input.ownerActorId ?? localActorId;
	const path = `/v1/workspaces/${encodeURIComponent(workspaceId)}/chats`;
	if (fixtureMode) {
		return wait(10).then(() => {
			const chatId = `chat-local-${store.nextId++}`;
			const created = decodeWire('ChatThreadWire', {
				chat_id: chatId,
				conversation_id: chatId, root_conversation_id: chatId, parent_conversation_id: null,
				workspace_id: workspaceId,
				title,
				state: 'active',
				owner_actor_id: owner,
				created_at: new Date().toISOString(),
				updated_at: null,
				links: { chat_ids: [], investigation_ids: [], strategy_ids: [], experiment_ids: [], workflow_execution_ids: [], tool_job_ids: [] },
				input_consumed_positions: {},
				created_by: owner,
				schema_version: 1,
				content_hash: fixtureDigest(JSON.stringify([workspaceId, store.nextId, title, owner]))
			}, path);
			const doc = fixtures.chats[workspaceId] ?? { items: [] };
			doc.items.unshift(plain(created));
			fixtures.chats[workspaceId] = doc;
			store.messages[created.chat_id] = [];
			return toChatThread(created);
		});
	}
	return apiFetch<unknown>(path, {
		method: 'POST',
		body: JSON.stringify({ title, owner_actor_id: owner }),
		signal
	}).then((raw) => toChatThread(decodeWire('ChatThreadWire', raw, path)));
}

export function deleteChat(workspaceId: string, chatId: string, signal?: AbortSignal): Promise<void> {
	if (fixtureMode) {
		return wait(10).then(() => {
			const doc = fixtures.chats[workspaceId];
			if (doc) {
				doc.items = doc.items.filter((chat) => chat.chat_id !== chatId);
			}
			delete store.messages[chatId];
			fixtureTimeline.remove(workspaceId, chatId);
		});
	}
	return apiFetch<void>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}`,
		{ method: 'DELETE', signal }
	);
}

export type TimelinePage = TimelinePageWire;

/** Fetch a snapshot page and its chat-wide stream watermark; null thread selects all threads. */
export async function listTimelinePage(
	workspaceId: string, chatId: string, threadId: string | null, before: number | null,
	signal?: AbortSignal
): Promise<TimelinePage> {
	if (fixtureMode) {
		signal?.throwIfAborted();
		return decodeWire('TimelinePageWire', fixtureTimeline.page(workspaceId, chatId, threadId, before), 'fixture/timeline');
	}
	signal?.throwIfAborted();
	const query = new URLSearchParams({ limit: '1000' });
	if (threadId !== null) query.set('thread', threadId);
	if (before !== null) query.set('before', String(before));
	const request = new AbortController();
	const cancel = () => request.abort(signal?.reason);
	signal?.addEventListener('abort', cancel, { once: true });
	const timeout = setTimeout(() => request.abort(new Error('Conversation history page timed out after 30 seconds.')), 30_000);
	let rejectAbort!: () => void;
	const aborted = new Promise<never>((_, reject) => {
		rejectAbort = () => reject(request.signal.reason);
		request.signal.addEventListener('abort', rejectAbort, { once: true });
	});
	try {
		const page = await Promise.race([apiFetch<TimelinePage>(
			`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/timeline?${query}`,
			{ signal: request.signal }
		), aborted]);
		signal?.throwIfAborted();
		return decodeWire('TimelinePageWire', page, `/chats/${chatId}/timeline`);
	} finally {
		clearTimeout(timeout);
		signal?.removeEventListener('abort', cancel);
		request.signal.removeEventListener('abort', rejectAbort);
	}
}

const workflowDefinitions = new Map<string, {
	promise: Promise<WorkflowDefinition>;
	expiresAt: number;
}>();
const WORKFLOW_DEFINITION_CACHE_LIMIT = 64;
const WORKFLOW_DEFINITION_CACHE_TTL = 5 * 60_000;

/** Share topology reads, not mutable execution state or failed/missing definitions. */
export function getWorkflowDefinition(
	workflowId: string,
	options: { workspaceId?: string; version?: string } = {}
): Promise<WorkflowDefinition> {
	const version = options.version || '';
	const key = JSON.stringify([backendKind, apiBaseUrl, options.workspaceId ?? '', workflowId, version]);
	const now = Date.now();
	for (const [storedKey, entry] of workflowDefinitions) {
		if (entry.expiresAt <= now) workflowDefinitions.delete(storedKey);
	}
	const cached = workflowDefinitions.get(key);
	if (cached) {
		workflowDefinitions.delete(key);
		workflowDefinitions.set(key, cached);
		return cached.promise;
	}
	const path = `/v1/workflows/${encodeURIComponent(workflowId)}${version ? `?${new URLSearchParams({ version })}` : ''}`;
	const entry: { promise: Promise<WorkflowDefinition>; expiresAt: number } = {
		promise: apiFetch<unknown>(path).then((raw) => {
			const value = decodeWire('WorkflowDefinitionWire', raw, path);
			if (workflowDefinitions.get(key) === entry) {
				// The route exposes only the current version. Never pin a latest read or mismatch.
				if (version && value.version === version && value.workflow_id === workflowId) {
					entry.expiresAt = Date.now() + WORKFLOW_DEFINITION_CACHE_TTL;
				} else workflowDefinitions.delete(key);
			}
			return value;
		}).catch((error) => {
			if (workflowDefinitions.get(key) === entry) workflowDefinitions.delete(key);
			throw error;
		}),
		expiresAt: Infinity
	};
	workflowDefinitions.set(key, entry);
	// Eviction also bounds tracked in-flight reads; late completions cannot reinsert themselves.
	while (workflowDefinitions.size > WORKFLOW_DEFINITION_CACHE_LIMIT) {
		workflowDefinitions.delete(workflowDefinitions.keys().next().value!);
	}
	return entry.promise;
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
	input: PostMessageInput,
	signal?: AbortSignal
): Promise<PostMessageResult> {
	if (fixtureMode) {
		return wait(20).then(() => {
			const doc = store.messages[chatId];
			if (!doc) throw new Error(`unknown chat ${chatId}`);
			const message: ChatMessage = {
				message_id: `msg-local-${store.nextId++}`,
				chat_id: chatId,
				conversation_id: chatId, root_conversation_id: chatId, parent_conversation_id: null,
				workspace_id: workspaceId,
				actor_id: 'user-alex',
				role: 'user',
				body: input.body,
				state: 'accepted',
				attachment_ids: input.attachmentIds ?? [],
				created_at: new Date().toISOString(),
				updated_at: null,
				execution_id: input.targetsExecutionId ?? null,
				intent_epoch: input.steeringIntent ? 1 : 0,
				created_by: localActorId,
				schema_version: 1,
				content_hash: fixtureDigest(JSON.stringify([workspaceId, chatId, store.nextId, input.body])),
				input_position: 0,
				investigation_ids: [],
				kind: 'message',
				data: null,
				reasoning: null,
				tool_result: null
			};
			decodeWire('ChatMessageWire', message, 'fixture/post');
			doc.push(message);
			fixtureTimeline.message(message);
			// Fixture percolation mirrors the API: steering bumps the active
			// execution's intent epoch once and records the ack.
			const targetId = input.targetsExecutionId ?? activeExecutionId(chatId);
			const percolatedTo =
				targetId && (input.steeringIntent || input.targetsExecutionId)
					? [
							{
								executionId: targetId,
								intentEpoch: message.intent_epoch
							}
						]
					: [];
			return messageResult(decodeWire('CreateMessageResponseWire', { ...message,
				steering: { intent: input.steeringIntent ?? null, targetsExecutionId: input.targetsExecutionId ?? null, percolatedTo }
			}, 'fixture/post'));
		});
	}
	const path = `/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/messages`;
	return apiFetch<unknown>(
		path,
		{
			signal,
			method: 'POST',
			body: JSON.stringify({
				actor_id: localActorId,
				body: input.body,
				attachment_ids: input.attachmentIds ?? [],
				steering_intent: input.steeringIntent ?? null,
				targets_execution_id: input.targetsExecutionId ?? null
			})
		}
	).then((raw) => messageResult(decodeWire('CreateMessageResponseWire', raw, path)));
}

function messageResult(raw: CreateMessageResponseWire): PostMessageResult {
	const { steering, ...message } = raw;
	return {
		message,
		// The API returns the persisted message directly and, for a steering
		// message, nests the percolation under `steering`; normalize it to the
		// `PostMessageResult` shape the components consume.
		percolated: (steering?.percolatedTo ?? []).map((target) => ({
			execution_id: target.executionId, intent_epoch: target.intentEpoch, steering_intent: steering!.intent
		}))
	};
}

function activeExecutionId(chatId: string): string {
	const doc = fixtures.executions[chatId];
	return doc?.execution_id ?? '';
}

export function getExecution(
	workspaceId: string,
	chatId: string,
	signal?: AbortSignal
): Promise<WorkflowExecutionRecord | null> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const doc = fixtures.executions[chatId];
			if (!doc?.execution_id) return null;
			return decodeWire('WorkflowExecutionWire', plain(doc), 'fixture/execution');
		});
	}
	return apiFetchOptional<unknown>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}/execution`,
		null,
		{ signal }
	).then((raw) => raw === null ? null : decodeWire('WorkflowExecutionWire', raw, `/chats/${chatId}/execution`));
}

// ---------------------------------------------------------------------------
// Chat-scoped sub-agent nodes: durable tree, node activity, node input.
//
// Contract (backend `thesis.http.chat_sub_agents`): a chat exposes its durable
// sub-agent tree at `…/chats/{chat}/sub_agents` (a pure projection of durable
// records that survives a restart). Node activity replays at
// `{node}/events?after_cursor=&include_descendants=` and streams at
// `{node}/events/stream`. Operator input posts to `{node}/messages` — the
// answering actor is derived from the authenticated session, never the body,
// and only nodes that accept input accept a post.
// ---------------------------------------------------------------------------

/** The base path for a chat and one of its sub-agent nodes. */
export function chatNodeBase(
	workspaceId: string,
	chatId: string,
	node?: ExecutionNodeRef | null
): string {
	const chat = `/v1/workspaces/${encodeURIComponent(workspaceId)}/chats/${encodeURIComponent(chatId)}`;
	if (!node || node.kind === 'execution') return chat;
	return `${chat}/sub_agents/${encodeURIComponent(node.id)}`;
}

/**
 * Normalize the backend's durable sub-agent node to the camelCase UI
 * contract. The chat-scoped tree serializes `ChatSubAgentNode.as_dict()`.
 */
function toSubAgentNode(raw: SubAgentNodeWire): SubAgentNode {
	const { children, ...node } = raw;
	return { ...node, context: null };
}

/**
 * List the chat's durable sub-agent tree as a flat node list (every node,
 * parent-linked). This is the durable ownership view: it survives restarts
 * and includes children spawned directly from chat. The execution's hidden
 * workflow-node record is included here so the tree projection can re-anchor
 * its children; it is never surfaced by the projection.
 */
export function listChatSubAgents(
	workspaceId: string,
	chatId: string,
	signal?: AbortSignal
): Promise<SubAgentNode[]> {
	const path = `${chatNodeBase(workspaceId, chatId)}/sub_agents`;
	if (fixtureMode) return wait(5).then(() => decodeWire('ChatSubAgentTreeWire', { chatId, roots: [], items: [] }, path).items.map(toSubAgentNode));
	return apiFetch<unknown>(path, { signal }).then((raw) =>
		decodeWire('ChatSubAgentTreeWire', raw, path).items.map(toSubAgentNode)
	);
}

export interface PostNodeMessageInput {
	body: string;
}

/**
 * Post operator input to a chat-scoped node. The caller must only invoke this
 * when the node's `acceptsInput` is true; the backend rejects input to a node
 * that does not accept it, and derives the actor from the session.
 */
export function postNodeMessage(
	workspaceId: string,
	chatId: string,
	node: ExecutionNodeRef,
	input: PostNodeMessageInput,
	signal?: AbortSignal
): Promise<ChatMessage> {
	const path = `${chatNodeBase(workspaceId, chatId, node)}/messages`;
	if (fixtureMode) {
		return wait(10).then(() => {
			const receipt = decodeWire('NodeInputReceiptWire', {
				message_id: `msg-node-${store.nextId++}`,
				chat_id: chatId,
				conversation_id: node.id, root_conversation_id: chatId, parent_conversation_id: chatId,
				workspace_id: workspaceId,
				actor_id: localActorId,
				role: 'user',
				body: input.body,
				state: 'accepted',
				attachment_ids: [],
				created_at: new Date().toISOString(),
				updated_at: null,
				execution_id: null,
				intent_epoch: 0,
				kind: 'sub_agent',
				data: { subAgentId: node.id },
				created_by: localActorId,
				schema_version: 1,
				content_hash: fixtureDigest(JSON.stringify([workspaceId, chatId, node.id, store.nextId, input.body])),
				input_position: 0,
				investigation_ids: [],
				reasoning: null,
				tool_result: null,
				subAgentId: node.id,
				operatorPolicy: { allowed: true, actorId: localActorId, actorKind: 'human_operator',
					allowedActorKinds: ['human_operator'], policy: 'human_only', role: 'operator', nodeId: node.id }
			}, path);
			const message = nodeMessage(receipt);
			fixtureTimeline.message(message);
			return message;
		});
	}
	return apiFetch<unknown>(path, {
		method: 'POST',
		body: JSON.stringify({ body: input.body }),
		signal
	}).then((raw) => nodeMessage(decodeWire('NodeInputReceiptWire', raw, path)));
}

function nodeMessage(receipt: NodeInputReceiptWire): ChatMessage {
	const { subAgentId, operatorPolicy, ...message } = receipt;
	return { ...message, kind: 'sub_agent', data: { ...(message.data ?? {}), subAgentId } };
}

/** Fetch one workflow execution record by its execution id. */
export function getWorkflowExecution(
	workspaceId: string,
	executionId: string
): Promise<WorkflowExecutionRecord | null> {
	if (fixtureMode) {
		return wait(5).then(() => {
			const found = Object.values(fixtures.executions).find(
				(record) => record.execution_id === executionId
			);
			return found ? decodeWire('WorkflowExecutionWire', plain(found), 'fixture/execution') : null;
		});
	}
	return apiFetchOptional<unknown>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/executions/${encodeURIComponent(executionId)}`,
		null
	).then((raw) => raw === null ? null : decodeWire('WorkflowExecutionWire', raw, `/executions/${executionId}`));
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

export interface CreateStrategyInput {
	name: string;
	symbol: string;
	description?: string;
	status?: StrategyStatus;
}

export function createStrategy(
	workspaceId: string,
	input: CreateStrategyInput
): Promise<Strategy> {
	const status = input.status ?? 'draft';
	const description = input.description ?? '';
	if (fixtureMode) {
		return wait(15).then(() => {
			const now = new Date().toISOString();
			const created: Strategy = {
				id: `str-local-${store.nextId++}`,
				workspace_id: workspaceId,
				name: input.name,
				symbol: input.symbol.toUpperCase(),
				status,
				description,
				pnl: 0,
				return_pct: 0,
				win_rate: 0,
				capital: 0,
				code: '',
				versions: [{ version: '0.1.0', created_at: now, parent_version: null, note: 'Created' }],
				created_at: now
			};
			const doc = fixtures.strategies[workspaceId] ?? { items: [] };
			doc.items.unshift(plain(created));
			fixtures.strategies[workspaceId] = doc;
			return created;
		});
	}
	return apiFetch<Strategy>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/strategies`,
		{
			method: 'POST',
			body: JSON.stringify({
				name: input.name,
				symbol: input.symbol,
				description,
				status
			})
		}
	);
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

const FIXTURE_ARTIFACT_NAMES = [
	'manifest.json',
	'metrics.json',
	'equity.parquet',
	'trades.parquet',
	'report.html',
	'orders.csv'
];

/**
 * Deterministic fixture artifact list derived from the fixture manifest's
 * declared `artifact_count`/`total_bytes`/`run_digest`. Fixture mode has no
 * stored artifact documents, so the client synthesizes exactly the manifest's
 * declared count; real mode returns whatever the API publishes.
 */
function fixtureArtifacts(
	doc: Omit<BacktestDetail, 'equity' | 'trades'>
): ArtifactEntry[] {
	const count = Math.max(0, Math.trunc(doc.manifest?.artifact_count ?? 0));
	const total = Math.max(0, Math.trunc(doc.manifest?.total_bytes ?? 0));
	if (!count) return [];
	const per = Math.floor(total / count);
	const digest = doc.manifest?.run_digest ?? null;
	const runId = doc.manifest?.run_id ?? doc.id;
	return Array.from({ length: count }, (_, index) => {
		const name = FIXTURE_ARTIFACT_NAMES[index] ?? `artifact-${index + 1}.bin`;
		const size = index === count - 1 ? Math.max(0, total - per * (count - 1)) : per;
		return {
			name,
			size,
			object_path: `backtests/${runId}/artifacts/${name}`,
			sas_url: null,
			sha256: digest
		};
	});
}

export function getBacktest(workspaceId: string, backtestId: string): Promise<BacktestDetail> {
	if (fixtureMode) {
		return wait(10).then(() => {
			const doc =
				fixtures.backtestDetail[backtestId];
			const series =
				fixtures.backtestSeries[backtestId];
			if (!doc || !series) throw new Error(`unknown backtest ${backtestId}`);
			return {
				...plain(doc),
				equity: plain(series.equity),
				trades: plain(series.trades),
				artifacts: fixtureArtifacts(doc)
			};
		});
	}
	return apiFetch<BacktestDetail>(
		`/v1/workspaces/${encodeURIComponent(workspaceId)}/backtests/${encodeURIComponent(backtestId)}`
	);
}

/**
 * Materialize one harness run into workspace card data.
 *
 * Called once the harness MCP tool returns a run id. Thesis owns the harness
 * boundary, so the same call yields the report for local and Azure execution.
 */
export function materializeBacktestRun(
	workspaceId: string,
	runId: string,
	options: { chatId?: string | null; executionId?: string | null } = {}
): Promise<BacktestRunCard> {
	const params = new URLSearchParams();
	if (options.chatId) params.set('chat_id', options.chatId);
	if (options.executionId) params.set('execution_id', options.executionId);
	const query = params.toString();
	const path = `/v1/workspaces/${encodeURIComponent(workspaceId)}/backtests/runs/${encodeURIComponent(runId)}${query ? `?${query}` : ''}`;
	if (fixtureMode) {
		return wait(10).then(() => ({
			status: 'complete' as const,
			run_id: runId,
			state: 'completed',
			backtest_id: null,
			backtest: null
		}));
	}
	return apiFetch<BacktestRunCard>(path);
}

/**
 * Launch a backtest through the canonical `POST /v1/backtests/jobs` route.
 * The Thesis harness router fronts the harness MCP `run_backtest` tool, so the
 * same call works whether the harness is local or remote. Fixture mode resolves
 * a synthetic accepted outcome shaped like the legacy launch document.
 */
export function launchBacktest(
	workspaceId: string,
	input: LaunchBacktestInput
): Promise<BacktestLaunchResult> {
	const body = {
		bundle_ref: input.bundleRef,
		config_path: input.configPath,
		strategy_id: input.strategyId ?? null,
		name: input.name ?? null,
		environment: input.environment ?? 'paper'
	};
	if (fixtureMode) {
		return wait(15).then(() => ({
			action: 'backtest',
			bundle_ref: body.bundle_ref,
			environment: body.environment,
			exit_code: 0,
			job_id: `job-local-${store.nextId++}`,
			state: 'accepted',
			request_payload: { ...body }
		}));
	}
	return apiFetch<HarnessJobResult>('/v1/backtests/jobs', {
		method: 'POST',
		body: JSON.stringify({
			config_path: input.configPath,
			workspace: workspaceId
		})
	}).then((job) => ({
		action: job.action,
		bundle_ref: body.bundle_ref,
		environment: body.environment,
		exit_code: job.exit_code,
		job_id: job.job_id,
		state: job.state,
		request_payload: job.result
	}));
}

// ---------------------------------------------------------------------------
// Harness topology + MCP operations (thesis.http.harness, `/v1/harness/status`
// and the canonical `/v1/backtests/jobs|runs` routes).
// ---------------------------------------------------------------------------

const TERMINAL_JOB_STATES = new Set([
	'completed',
	'failed',
	'cancelled',
	'canceled',
	'archived',
	'error'
]);

/**
 * Read the harness topology and connection status. The route answers 200 even
 * when the harness is unreachable, so the UI can render `reachable`/`error`
 * instead of treating the probe as a transport failure.
 */
export function getHarnessStatus(): Promise<HarnessStatus> {
	if (fixtureMode) {
		return wait(5).then(() => ({
			mode: 'local' as const,
			thesis_location: 'local' as const,
			harness_location: 'local' as const,
			reachable: true,
			server: { serverInfo: { name: 'harness', version: '0.1.0' } },
			tools: [
				{ name: 'run_backtest', description: 'Run one deterministic backtest' },
				{ name: 'get_job_status', description: 'Read a backtest job' },
				{ name: 'get_run', description: 'Read a stored run' },
				{ name: 'list_artifacts', description: 'List a run artifact manifest' }
			],
			health: { status: 'ok' },
			error: null
		}));
	}
	return apiFetch<HarnessStatus>('/v1/harness/status');
}

/** Read one canonical backtest job row. */
export function getHarnessJob(jobId: string): Promise<HarnessJobStatus> {
	if (fixtureMode) {
		return wait(5).then(() => ({ job_id: jobId, state: 'completed' }));
	}
	return apiFetch<HarnessJobStatus>(
		`/v1/backtests/jobs/${encodeURIComponent(jobId)}`
	);
}

/** Read one stored run's catalog row. */
export function getHarnessRun(runId: string): Promise<HarnessRunRecord> {
	if (fixtureMode) {
		return wait(5).then(() => ({
			run_id: runId,
			status: 'completed',
			strategy_id: 'fixture'
		}));
	}
	return apiFetch<HarnessRunRecord>(
		`/v1/backtests/runs/${encodeURIComponent(runId)}`
	);
}

/** Read one run's artifact manifest on the canonical `uri`/`sha256`/`sizeBytes` shape. */
export function listHarnessArtifacts(runId: string): Promise<HarnessArtifactManifest> {
	if (fixtureMode) {
		return wait(5).then(() => ({ run_id: runId, artifacts: [] }));
	}
	return apiFetch<HarnessArtifactManifest>(
		`/v1/backtests/runs/${encodeURIComponent(runId)}/artifacts`
	);
}

/**
 * Poll a launched job until it reaches a terminal state or the attempt budget
 * is spent. A local harness that completes synchronously resolves immediately.
 */
export async function pollHarnessJob(
	jobId: string,
	options: { intervalMs?: number; maxAttempts?: number } = {}
): Promise<HarnessJobStatus> {
	const intervalMs = options.intervalMs ?? 750;
	const maxAttempts = options.maxAttempts ?? 40;
	let status = await getHarnessJob(jobId);
	for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
		if (TERMINAL_JOB_STATES.has(status.state)) return status;
		await wait(intervalMs);
		status = await getHarnessJob(jobId);
	}
	return status;
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

// ---------------------------------------------------------------------------
// Screener definitions and runs (thesis.http.screeners).
//
// Definitions are content-addressed workspace artifacts authored as Rust
// source; a run materializes the definition's config through the Thesis MCP
// boundary and returns the imported terminal record. Fixture mode mirrors the
// same shapes from bundled documents: saves address by content and a run
// returns a synthetic complete result whose provenance matches the definition
// it ran, so the page renders with no backend.
// ---------------------------------------------------------------------------

function screenerPath(workspaceId: string): string {
	return `/v1/workspaces/${encodeURIComponent(workspaceId)}/screeners`;
}

/** List the workspace's saved screener definitions. */
export function listScreeners(workspaceId: string): Promise<ScreenerDefinition[]> {
	if (fixtureMode) {
		return wait(5).then(() => plain(store.screeners[workspaceId] ?? []));
	}
	return apiFetch<{ items: ScreenerDefinition[] }>(screenerPath(workspaceId)).then((r) => r.items);
}

/**
 * Save one authored definition. The Thesis API validates the definition
 * (HTTP 422), hashes the LF-normalized source, and returns the content
 * addressed document; saving the same content twice yields the same id.
 */
export function saveScreener(
	workspaceId: string,
	input: ScreenerSaveInput
): Promise<ScreenerDefinition> {
	const path = screenerPath(workspaceId);
	if (fixtureMode) {
		return wait(15).then(() => {
			const name = input.name.trim();
			if (!name || name.length > 256) throw new ThesisApiError(422, path);
			if (!Number.isInteger(input.lookback_days) || input.lookback_days < 1 || input.lookback_days > 3650) {
				throw new ThesisApiError(422, path);
			}
			if (input.bar_interval !== '1min' && input.bar_interval !== '1d') {
				throw new ThesisApiError(422, path);
			}
			if (!input.rank_metric.trim() || !input.source.trim()) throw new ThesisApiError(422, path);
			const parameters = plain(input.parameters ?? {});
			const universeRevision = input.universe_revision ?? null;
			const definitionSha256 = fixtureDigest(
				JSON.stringify([
					name,
					input.lookback_days,
					input.bar_interval,
					input.rank_metric.trim(),
					universeRevision,
					input.source,
					parameters
				])
			);
			const created: ScreenerDefinition = {
				schema: 'fca-thesis-screener-definition-v1',
				name,
				lookbackDays: input.lookback_days,
				barInterval: input.bar_interval,
				rankMetric: input.rank_metric.trim(),
				universeRevision,
				source: input.source,
				parameters,
				sourceSha256: fixtureDigest(input.source),
				definitionSha256,
				definitionId: `sha256:${definitionSha256}`
			};
			const existing = store.screeners[workspaceId] ?? [];
			store.screeners[workspaceId] = [
				created,
				...existing.filter((item) => item.definitionId !== created.definitionId)
			];
			return plain(created);
		});
	}
	return apiFetch<ScreenerDefinition>(path, {
		method: 'POST',
		body: JSON.stringify({
			name: input.name,
			lookback_days: input.lookback_days,
			bar_interval: input.bar_interval,
			rank_metric: input.rank_metric,
			universe_revision: input.universe_revision ?? null,
			source: input.source,
			parameters: input.parameters ?? null
		})
	});
}

/**
 * Run one saved definition at an explicit as-of date. The Thesis route blocks
 * until the harness run imports (or fails): unknown definitions raise 404, an
 * unavailable harness raises 503, and a failed run raises 502.
 */
export function runScreener(
	workspaceId: string,
	input: ScreenerRunInput
): Promise<ScreeningResult> {
	const path = `${screenerPath(workspaceId)}/run`;
	if (fixtureMode) {
		return wait(20).then(() => {
			const definition = (store.screeners[workspaceId] ?? []).find(
				(item) => item.definitionId === input.definition_id
			);
			if (!definition) throw new ThesisApiError(404, path);
			const template = fixtures.screenerResults[workspaceId]?.items?.[0];
			const rows = plain(template?.rows ?? []).map((row, index) => ({
				...row,
				rank: row.rank ?? index + 1
			}));
			const resultId = `sha256:${fixtureDigest(
				`${workspaceId}\u0000${definition.definitionId}\u0000${input.as_of}`
			)}`;
			const created: ScreeningResult = {
				result_id: resultId,
				workspace_id: workspaceId,
				definition_id: definition.definitionId,
				definition_hash: definition.definitionSha256,
				run_id: `screen-${resultId.slice(7, 23)}`,
				status: 'complete',
				as_of: input.as_of,
				rows,
				provenance: {
					...(template ? plain(template.provenance) : {}),
					screen: {
						...(plain((template?.provenance?.screen as Record<string, unknown>) ?? {})),
						as_of: input.as_of,
						bar_interval: definition.barInterval,
						bundle_hash: definition.sourceSha256,
						definition_hash: definition.definitionSha256,
						definition_id: definition.definitionId,
						rank_metric: definition.rankMetric,
						universe_revision: definition.universeRevision
					}
				}
			};
			store.screenerResults[workspaceId] = [
				created,
				...(store.screenerResults[workspaceId] ?? []).filter(
					(item) => item.result_id !== created.result_id
				)
			];
			return plain(created);
		});
	}
	return apiFetch<ScreeningResult>(path, {
		method: 'POST',
		body: JSON.stringify({ definition_id: input.definition_id, as_of: input.as_of })
	});
}

/** Fetch one stored screening result (with provenance) by its content id. */
export function getScreenerResult(
	workspaceId: string,
	resultId: string
): Promise<ScreeningResult> {
	const path = `${screenerPath(workspaceId)}/results/${encodeURIComponent(resultId)}`;
	if (fixtureMode) {
		return wait(5).then(() => {
			const found = (store.screenerResults[workspaceId] ?? []).find(
				(item) => item.result_id === resultId
			);
			if (!found) throw new ThesisApiError(404, path);
			return plain(found);
		});
	}
	return apiFetch<ScreeningResult>(path);
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
		// Same-origin fetch sends the API-issued session cookie by default;
		// no shared bearer is attached (cookie auth lands in T306/T307).
		const response = await fetch(`${apiBaseUrl}/v1/auth/check`);
		return response.ok;
	} catch {
		return false;
	}
}
