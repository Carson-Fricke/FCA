/**
 * UI view models derived from generated chat/runtime contracts, plus scoped
 * mirrors for product endpoints not yet included in the backend export.
 *
 * Chat/execution records mirror the snake_case serialization of
 * `src/thesis/chat/contracts.py`; execution SSE events mirror the camelCase
 * `ExecutionEvent.as_dict()` shape from `src/thesis/execution/events.py`;
 * harness status surfaces mirror `src/thesis/integrations/runtime_status.py`;
 * consent shapes mirror `src/thesis/activation/consent.py` canonical JSON.
 */
import type {
	ChatMessageWire,
	ChatThreadWire,
	SteeringExecutionWire,
	SteeringPercolationWire,
	ExecutionEventWire,
	SubAgentNodeWire,
	TimelineItemWire,
	WorkflowDefinitionWire,
	WorkflowExecutionWire,
	ToolResultWire
} from './generated';

// ---------- Status surfaces (runtime_status.py) ----------

export type StatusState = 'available' | 'unavailable';

export interface StatusSurface<T> {
	state: StatusState;
	value: T | null;
	reason: string | null;
}

export interface RuntimeStatusRecord {
	request_ref: string;
	state: string;
	created_at: string;
	updated_at: string;
	error_message: string | null;
}

export interface ManifestSummary {
	job_id: string;
	status: string;
	strategy_id: string;
	data_start: string | null;
	data_end: string | null;
	run_digest: string | null;
	configuration_digest: string | null;
	artifact_count: number;
	total_bytes: number;
	metrics: Record<string, string>;
}

export interface ArtifactEntry {
	name: string;
	size: number;
	object_path: string;
	sas_url: string | null;
	/** Content digest when the run manifest publishes one. */
	sha256?: string | null;
}

export interface PositionRecord {
	symbol: string;
	quantity: string;
	average_price: string | null;
}

export interface BookRecord {
	name: string;
	cash: string;
	equity: string;
	positions: PositionRecord[];
}

export interface PortfolioSummary {
	books: BookRecord[];
	cash: string;
	equity: string;
}

export interface RiskDecisionRecord {
	rule: string;
	decision: string;
	reason: string | null;
}

// ---------- Consent (activation/consent.py canonical JSON) ----------

export type GrantEnvironment = 'dev' | 'prod';
export type GrantAction = 'paper' | 'shadow' | 'live';
export type GrantScopeKind = 'one_shot' | 'interval';

export interface GrantScope {
	kind: GrantScopeKind;
	intervalEnd: string | null;
}

export interface ConsentProposal {
	schema: 'fca-thesis-consent-proposal-v1';
	action: GrantAction;
	bundleHash: string;
	capitalBound: string;
	environment: GrantEnvironment;
	expiration: string;
	instruments: string[];
	proposedAt: string;
	riskBound: string;
	riskBoundKind: 'max-daily-loss';
	scope: GrantScope;
}

export interface ConsentConfirmation {
	schema: 'fca-thesis-consent-confirmation-v1';
	action: GrantAction;
	bundleHash: string;
	confirmationToken: string;
	environment: GrantEnvironment;
}

// ---------- Chat + execution (chat/contracts.py serialization) ----------

export type MessageRole = ChatMessageWire['role'];

/** Transcript position and pending tool state are UI projections, not wire fields. */
export type ChatMessage = Pick<ChatMessageWire,
	'message_id' | 'chat_id' | 'workspace_id' | 'actor_id' | 'role' | 'body' | 'state' |
	'attachment_ids' | 'created_at' | 'updated_at' | 'execution_id' | 'intent_epoch'
> & Partial<Omit<ChatMessageWire,
	'message_id' | 'chat_id' | 'workspace_id' | 'actor_id' | 'role' | 'body' | 'state' |
	'attachment_ids' | 'created_at' | 'updated_at' | 'execution_id' | 'intent_epoch' | 'tool_result'
>> & {
	seq?: number;
	threadId?: string | null;
	tool_result?: ToolResult | null;
};

/** Thread kind and flattened links are presentation projections of the durable record. */
export type ChatThread = ChatThreadWire & {
	kind: 'chat' | 'investigation';
	investigation_ids: NonNullable<ChatThreadWire['links']['investigation_ids']>;
	strategy_ids: NonNullable<ChatThreadWire['links']['strategy_ids']>;
};

/** The percolation result returned by POST .../chats/{chat}/messages. */
export type SteeringPercolationTarget = {
	execution_id: SteeringExecutionWire['executionId'];
	intent_epoch: SteeringExecutionWire['intentEpoch'];
	steering_intent: SteeringPercolationWire['intent'];
};

export interface PostMessageResult {
	message: ChatMessage;
	percolated: SteeringPercolationTarget[];
}

export type ExecutionEventType = ExecutionEventWire['eventType'];

/** Durable execution event; `as_dict()` camelCase serialization. */
export type ExecutionEvent = ExecutionEventWire & {
	seq?: number;
	threadId?: string | null;
};

/**
 * One item on the unified per-chat Cosmos timeline. Messages, execution
 * events, and execution state changes all share one ordered log; `threadId`
 * routes an item to the root agent or one sub-agent.
 */
export type TimelineItem = TimelineItemWire;

/** Cards can project handles before the complete execution record is loaded. */
export type WorkflowExecutionRecord = Pick<WorkflowExecutionWire,
	'execution_id' | 'chat_id' | 'trigger_message_id' | 'workflow_id' | 'workflow_version' |
	'requested_by' | 'state' | 'phase' | 'intent_epoch' | 'steering_message_ids' | 'created_at'
> & Partial<Omit<WorkflowExecutionWire,
	'execution_id' | 'chat_id' | 'trigger_message_id' | 'workflow_id' | 'workflow_version' |
	'requested_by' | 'state' | 'phase' | 'intent_epoch' | 'steering_message_ids' | 'created_at'
>>;

/** Read-only topology returned by GET /v1/workflows/{workflow_id}. */
export type WorkflowDefinition = WorkflowDefinitionWire;

// ---------- Nested sub-agents and transcript items ----------

/**
 * Discriminator for chat transcript items. Every renderable entry carries a
 * `kind` plus a `data` payload so the transcript can route to the right card
 * without inspecting message bodies.
 */
export type TranscriptItemKind = ChatMessageWire['kind'];

export interface TranscriptItem {
	id: string;
	kind: TranscriptItemKind;
	data: Record<string, unknown>;
}

export type SubAgentStatus = SubAgentNodeWire['status'] | 'spawned' | 'progress';

/** Sandbox context attached to one sub-agent spawn. */
export interface SubAgentMcpContext {
	enabled: boolean;
	tools: string[];
}

export interface SubAgentContext {
	/** True when the sub-agent runs in a fresh, isolated context window. */
	isolated: boolean;
	/** Knowledge cutoff label for the isolated context, when declared. */
	knowledge_cutoff: string | null;
	mcp: SubAgentMcpContext;
}

/** One node in the nested sub-agent execution tree. */
export type SubAgentNode = Pick<SubAgentNodeWire,
	'subAgentId' | 'parentSubAgentId' | 'phase' | 'role' | 'acceptsInput' | 'workflowExecutionId'
> & Partial<Omit<SubAgentNodeWire,
	'children' | 'status' | 'subAgentId' | 'parentSubAgentId' | 'phase' | 'role' | 'acceptsInput' | 'workflowExecutionId'
>> & {
	/** Event-only display statuses aren't durable lifecycle values. */
	status: SubAgentStatus;
	/** Available only from an explicit event snapshot; not stored on durable tree nodes. */
	context: SubAgentContext | null;
};

/** A phase in the compact workflow stepper. */
export interface WorkflowPhase {
	name: string;
	status: 'done' | 'active' | 'pending';
	attempts?: number;
}

/** Reference to an execution node: the root execution or one sub-agent. */
export interface ExecutionNodeRef {
	kind: 'execution' | 'sub_agent';
	id: string;
}

// ---------- Ephemeral tool results (tool.result SSE event) ----------

export type ToolResultFailure = NonNullable<ToolResultWire['failure']>;

export type ToolResult = ToolResultWire & {
	pending?: boolean;
	outcome?: 'unknown_outcome' | 'interrupted';
	requestState?: 'waiting' | 'answered' | 'resolved';
	arguments?: unknown;
	durationMs?: number;
	startedAt?: string;
};

/** Response from the Thesis backtest-job materialization endpoint. */
export interface BacktestRunCard {
	job_id: string;
	state?: string | null;
	backtest?: BacktestDetail | null;
	error?: string | null;
	executionCertainty?: string | null;
	harness_execution?: boolean;
}

// ---------- Workspace product records ----------

export type StrategyStatus = 'live' | 'paper' | 'paused' | 'draft';
export type WidgetType = 'equity' | 'pnl' | 'allocation' | 'winrate' | 'orders';
export type DeploymentMode = 'paper' | 'live';

export interface WorkspaceSummary {
	id: string;
	name: string;
	description: string;
	initials: string;
	created_at: string;
}

export interface ActivityItem {
	id: string;
	workspace_id: string;
	title: string;
	description: string;
	type: 'deploy' | 'backtest' | 'strategy' | 'chat' | 'widget';
	created_at: string;
}

export interface WorkspaceOverview {
	workspace: WorkspaceSummary;
	activity: ActivityItem[];
	openChats: ChatThread[];
	health: 'operational' | 'degraded';
}

export interface WorkspaceSnapshot {
	workspaces: WorkspaceSummary[];
	overview: WorkspaceOverview;
	counts: {
		strategies: number;
		chats: number;
		backtests: number;
		deployments: number;
		dashboards: number;
		screeners: number;
	};
}

export interface StrategyVersion {
	version: string;
	created_at: string;
	parent_version: string | null;
	note: string;
}

export interface Strategy {
	id: string;
	workspace_id: string;
	name: string;
	symbol: string;
	status: StrategyStatus;
	description: string;
	pnl: number | null;
	return_pct: number | null;
	win_rate: number | null;
	capital: number | null;
	code: string;
	versions: StrategyVersion[];
	created_at: string;
}

export interface SegmentMetrics {
	returnPct: number;
	sharpe: number | null;
	maxDrawdown: number;
	trades: number;
}

/**
 * The manifest's declared train/test split. `null` (or a missing manifest
 * field) means the run does not declare a split — the UI must never
 * fabricate one.
 */
export interface DeclaredSplit {
	boundary: string;
	inSample: SegmentMetrics;
	outOfSample: SegmentMetrics;
}

export interface EquityPoint {
	t: number;
	v: number;
}

export interface TradeRecord {
	id: string;
	symbol: string;
	side: 'buy' | 'sell';
	qty: number;
	entryTime: number;
	entryPrice: number;
	exitTime: number;
	exitPrice: number;
	/** Net PnL after fees; the figure the statistics headline. */
	pnl: number;
	grossPnl: number;
	fees: number;
}

/**
 * Statistics are ``null`` when the harness did not publish them. Nothing is
 * zero-filled, so every numeric field the UI shows must handle ``null``.
 */
export interface BacktestSummaryRow {
	job_id: string;
	workspace_id: string;
	strategy_id: string;
	name: string;
	period_start: string;
	period_end: string;
	capital: number | null;
	total_return: number | null;
	sharpe: number | null;
	max_drawdown: number | null;
	win_rate: number | null;
	trades: number | null;
	split_declared: boolean;
	created_at: string;
	chat_id?: string | null;
	execution_id?: string | null;
}

export interface BacktestDetail {
	job_id: string;
	workspace_id: string;
	strategy_id: string;
	name: string;
	period_start: string;
	period_end: string;
	initial_capital: number | null;
	final_capital: number | null;
	capital_change: number | null;
	equity: EquityPoint[];
	trades: TradeRecord[];
	stats: Record<string, number | string | null>;
	split: DeclaredSplit | null;
	manifest: {
		job_id: string;
		status: string;
		run_digest: string | null;
		configuration_digest: string | null;
		data_start: string | null;
		data_end: string | null;
		artifact_count: number;
		total_bytes: number;
	};
	/**
	 * Manifest-imported artifacts when the API publishes them. The UI renders
	 * name/size/digest from this list and never fabricates entries.
	 */
	artifacts?: ArtifactEntry[];
	chat_id?: string | null;
	execution_id?: string | null;
}

/**
 * Payload for `POST /v1/workspaces/{ws}/backtests/launch`, mirroring the
 * Thesis `LaunchBacktestRequest` contract at the activation seam.
 */
export interface LaunchBacktestInput {
	/** Content-addressed strategy bundle reference. */
	bundleRef: string;
	/** Harness configuration path, e.g. `config/harness/backtest.yaml`. */
	configPath: string;
	/** Activation environment; the API defaults to `paper`. */
	environment?: 'paper' | 'shadow' | 'live';
	/** Optional strategy attribution. */
	strategyId?: string | null;
	/** Optional run display name. */
	name?: string | null;
}

/**
 * The activation outcome document returned by the launch route. Thesis
 * reports the harness result as inert data; it never interprets it.
 */
export interface BacktestLaunchResult {
	action: string;
	bundle_ref: string;
	environment: string;
	exit_code: number | null;
	job_id: string | null;
	state: string | null;
	request_payload: Record<string, unknown>;
}

// ---------- Harness MCP surface (thesis.http.harness) ----------

/** Where Thesis or the harness runs; drives the transport selection. */
export type HarnessLocation = 'local' | 'remote';

/** One published harness MCP tool. */
export interface HarnessToolDescriptor {
	name: string;
	description: string;
}

/**
 * Topology and connection status from `GET /v1/harness/status`. An unreachable
 * harness still answers 200 with `reachable: false` and an `error` string.
 */
export interface HarnessStatus {
	mode: HarnessLocation;
	thesis_location: HarnessLocation;
	harness_location: HarnessLocation;
	reachable: boolean;
	server: Record<string, unknown>;
	tools: HarnessToolDescriptor[];
	health: Record<string, unknown> | null;
	error: string | null;
}

/** One harness job row from `GET /v1/jobs/{job_id}`. */
export interface HarnessJobStatus {
	job_id: string;
	state: string;
	created_at?: string | null;
	updated_at?: string | null;
	error_message?: string | null;
	[key: string]: unknown;
}

/** One job result row from `GET /v1/jobs/{job_id}/result`. */
export interface HarnessJobResult {
	job_id: string;
	state?: string;
	strategy_id?: string;
	[key: string]: unknown;
}

/** Manifest-imported artifact on the canonical `sha256`/`sizeBytes`/`uri` shape. */
export interface HarnessArtifact {
	name: string;
	uri: string;
	sha256: string;
	sizeBytes: number;
	sasUrl: string | null;
}

/** The artifact manifest from `GET /v1/jobs/{job_id}/artifacts`. */
export interface HarnessArtifactManifest {
	job_id: string;
	artifacts: HarnessArtifact[];
}

export interface DeploymentCard {
	id: string;
	workspace_id: string;
	strategy_id: string;
	strategy_name: string;
	mode: DeploymentMode;
	state: 'running' | 'paused' | 'stopped';
	health: 'healthy' | 'degraded' | 'stale';
	allocated_capital: number;
	unrealized_pnl: number;
	realized_pnl: number;
	since: string;
	request_ref: string | null;
}

export interface OrderRecord {
	id: string;
	workspace_id: string;
	strategy_id: string;
	symbol: string;
	side: 'buy' | 'sell';
	quantity: string;
	price: string;
	status: 'filled' | 'open' | 'cancelled';
	mode: DeploymentMode;
	created_at: string;
}

export interface WidgetDefinition {
	id: string;
	workspace_id: string;
	title: string;
	type: WidgetType;
	pinned: boolean;
	created_at: string;
	/** Artifact-backed series/labels for the declared widget type. */
	data: Record<string, unknown>;
}

// ---------- Screener (thesis.http.screeners, /v1/workspaces/{ws}/screeners) ----------

/**
 * The bar intervals the harness accepts for a screening run, mirroring
 * `BAR_INTERVALS` in `src/thesis/screening/definitions.py`.
 */
export type ScreenerBarInterval = '1min' | '1d';

/**
 * A content-addressed screener definition document as served by the screener
 * router (`ScreenerDefinition.document()`): authored Rust source plus the
 * run-defining configuration. `sourceSha256` is the digest the harness verifies
 * as `bundle_hash`; `definitionSha256` is the domain-separated content address
 * and `definitionId` its `sha256:<hex>` form.
 */
export interface ScreenerDefinition {
	schema: 'fca-thesis-screener-definition-v1';
	name: string;
	lookbackDays: number;
	barInterval: ScreenerBarInterval;
	rankMetric: string;
	universeRevision: string | null;
	source: string;
	parameters: Record<string, unknown>;
	sourceSha256: string;
	definitionSha256: string;
	definitionId: string;
}

/** One ranked screening row (`ScreenRow.document()`). */
export interface ScreenRow {
	symbol: string;
	metrics: Record<string, string | number>;
	rank?: number;
}

/**
 * One imported screening result (`ScreeningRecord.document()`) with its
 * definition and run provenance. `status` is the terminal harness state the
 * importer accepted (for example `succeeded`); a result exists only for a
 * terminal run whose `screen.json` artifact was readable.
 */
export interface ScreeningResult {
	result_id: string;
	workspace_id: string;
	definition_id: string;
	definition_hash: string;
	job_id: string;
	status: string;
	as_of: string;
	rows: ScreenRow[];
	/**
	 * Import provenance: the harness artifact entry, artifact index, run
	 * catalog, and the `screen.json` document without its rows.
	 */
	provenance: Record<string, unknown>;
}

/** Request body for `POST /v1/workspaces/{ws}/screeners`. */
export interface ScreenerSaveInput {
	name: string;
	lookback_days: number;
	bar_interval: ScreenerBarInterval;
	rank_metric: string;
	universe_revision?: string | null;
	source: string;
	parameters?: Record<string, unknown> | null;
}

/** Request body for `POST /v1/workspaces/{ws}/screeners/run`. */
export interface ScreenerRunInput {
	definition_id: string;
	as_of: string;
}

// ---------- Lineage + evidence ----------

export interface LineageNode {
	id: string;
	version: string;
	created_at: string;
	note: string;
}

export interface LineageEdge {
	from: string;
	to: string;
	relation: 'adapted-from' | 'forked-from';
}

export interface StrategyLineage {
	strategy_id: string;
	strategy_name: string;
	nodes: LineageNode[];
	edges: LineageEdge[];
}

export interface EvidenceItem {
	id: string;
	kind: 'backtest' | 'artifact' | 'knowledge' | 'upload';
	title: string;
	digest: string | null;
	reference: string;
	revision: string | null;
	trust_tier: 'workspace' | 'untrusted' | 'external';
	link: string | null;
}

export interface KnowledgeHit {
	title: string;
	path: string;
	revision: string;
	trust_tier: 'workspace' | 'external';
	excerpt: string;
}

// ---------- Workspace artifacts ----------

export type ArtifactKind = 'strategy' | 'study' | 'indicator';
export type ArtifactViewer = 'code' | 'json' | 'markdown' | 'text';

export interface ArtifactInput {
	data_type: string;
	symbols: string[];
	interval: string | null;
}

export interface ArtifactFileEntry {
	path: string;
	sha256: string;
	size_bytes: number;
	viewer: ArtifactViewer;
}

export interface ArtifactHistoryEntry {
	label: string;
	created_at: string | null;
	note: string | null;
}

export interface ArtifactDigests {
	source_sha256: string | null;
	bundle_sha256: string | null;
	manifest_sha256: string | null;
}

export interface ArtifactExitCriterion {
	falsifier_id: string | null;
	observable: string | null;
	condition: string | null;
	threshold: number | null;
	description: string | null;
}

export interface StrategyArtifactPreview {
	type: 'strategy';
	entrypoint: string;
	symbol: string;
	exit_criteria: ArtifactExitCriterion[];
	limitations: string[];
	signals: null;
}

export interface StudyArtifactPreview {
	type: 'study';
	window: { start: string | null; end: string | null };
	indicator_ids: string[];
	metrics: { name: string; value: string }[];
}

export interface IndicatorUsage {
	id: string;
	kind: ArtifactKind;
	title: string;
}

export interface IndicatorArtifactPreview {
	type: 'indicator';
	value_series: null;
	used_by: IndicatorUsage[];
}

export type ArtifactPreview =
	| StrategyArtifactPreview
	| StudyArtifactPreview
	| IndicatorArtifactPreview;

export interface WorkspaceArtifact {
	id: string;
	kind: ArtifactKind;
	title: string;
	subtitle: string;
	description: string;
	status: string;
	version: string;
	artifact_path: string;
	created_at: string | null;
	updated_at: string | null;
	git_commit: string | null;
	digests: ArtifactDigests;
	inputs: ArtifactInput[];
	files: ArtifactFileEntry[];
	file_count: number;
	size_bytes: number;
	history: ArtifactHistoryEntry[];
	preview: ArtifactPreview;
}

export interface WorkspaceArtifactSources {
	strategies: string;
	studies: string;
	indicators: string;
}

export interface WorkspaceArtifactCatalog {
	workspace_id: string;
	items: WorkspaceArtifact[];
	sources: WorkspaceArtifactSources;
}

export interface ArtifactFileContent {
	path: string;
	text: string;
	truncated: boolean;
	sha256: string;
	size_bytes: number;
}

// ---------- Backend selection ----------

/** Selectable data backend: bundled fixtures, same-origin local, or remote. */
export type BackendKind = 'fixtures' | 'local' | 'azure';
