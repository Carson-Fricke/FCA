/**
 * Typed mirrors of the Thesis API contracts the UI consumes.
 *
 * Chat/execution records mirror the snake_case serialization of
 * `src/thesis/chat/contracts.py`; execution SSE events mirror the camelCase
 * `ExecutionEvent.as_dict()` shape from `src/thesis/execution/events.py`;
 * harness status surfaces mirror `src/thesis/integrations/runtime_status.py`;
 * consent shapes mirror `src/thesis/activation/consent.py` canonical JSON.
 */

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
	run_id: string;
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

export interface ConsentAuditEntry {
	grantId: string;
	kind: 'proposal' | 'confirmation' | 'grant' | 'revocation';
	at: string;
	actor: string;
	bundleHash: string;
	environment: GrantEnvironment;
	action: GrantAction;
	summary: string;
}

// ---------- Chat + execution (chat/contracts.py serialization) ----------

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

export interface ChatMessage {
	message_id: string;
	chat_id: string;
	workspace_id: string;
	actor_id: string;
	role: MessageRole;
	body: string;
	state: string;
	attachment_ids: string[];
	created_at: string;
	updated_at: string | null;
	execution_id: string | null;
	intent_epoch: number;
	/** Model reasoning trace persisted alongside the assistant reply, if any. */
	reasoning?: string;
	/** Structured harness-tool outcome carried by a tool-role message. */
	tool_result?: ToolResult | null;
}

export interface ChatThread {
	chat_id: string;
	workspace_id: string;
	title: string;
	state: string;
	kind: 'chat' | 'investigation';
	owner_actor_id: string;
	created_at: string;
	updated_at: string | null;
	investigation_ids: string[];
	strategy_ids: string[];
}

/** The percolation result returned by POST .../chats/{chat}/messages. */
export interface SteeringPercolationTarget {
	execution_id: string;
	intent_epoch: number;
	steering_intent: string | null;
}

export interface PostMessageResult {
	message: ChatMessage;
	percolated: SteeringPercolationTarget[];
}

export type ExecutionEventType =
	| 'workflow.launched'
	| 'workflow.started'
	| 'workflow.paused'
	| 'workflow.steered'
	| 'workflow.stopped'
	| 'workflow.cancelled'
	| 'workflow.completed'
	| 'workflow.failed'
	| 'message.accepted'
	| 'tool.submitted'
	| 'tool.started'
	| 'tool.result'
	| 'tool.failed';

/** Durable execution event; `as_dict()` camelCase serialization. */
export interface ExecutionEvent {
	actorId: string;
	actorScope: 'user' | 'assistant' | 'system' | 'tool' | 'workflow';
	correlationId: string;
	cursor: number;
	eventId: string;
	eventType: ExecutionEventType;
	executionId: string;
	intentEpoch: number;
	lateResult: boolean;
	occurredAt: string;
	payload: Record<string, unknown>;
	workspaceId: string;
}

export interface WorkflowExecutionRecord {
	execution_id: string;
	chat_id: string;
	trigger_message_id: string;
	workflow_id: string;
	workflow_version: string;
	requested_by: string;
	state: string;
	phase: string | null;
	intent_epoch: number;
	steering_message_ids: string[];
	created_at: string;
}

// ---------- Ephemeral tool results (tool.result SSE event) ----------

export interface ToolResultFailure {
	kind: string;
	message: string;
	tool?: string | null;
	statusCode?: number | null;
	payload?: Record<string, unknown>;
}

export interface ToolResult {
	tool: string;
	callId: string | null;
	ok: boolean;
	summary: string;
	structured: Record<string, unknown>;
	text: string;
	failure: ToolResultFailure | null;
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
	counts: { strategies: number; chats: number; backtests: number; deployments: number; dashboards: number; screeners: number };
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
	pnl: number;
	return_pct: number;
	win_rate: number;
	capital: number;
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
	pnl: number;
}

export interface BacktestSummaryRow {
	id: string;
	workspace_id: string;
	strategy_id: string;
	name: string;
	period_start: string;
	period_end: string;
	capital: number;
	total_return: number;
	sharpe: number;
	max_drawdown: number;
	win_rate: number;
	trades: number;
	split_declared: boolean;
	created_at: string;
}

export interface BacktestDetail {
	id: string;
	workspace_id: string;
	strategy_id: string;
	name: string;
	period_start: string;
	period_end: string;
	initial_capital: number;
	final_capital: number;
	net_pnl: number;
	equity: EquityPoint[];
	trades: TradeRecord[];
	stats: Record<string, number>;
	split: DeclaredSplit | null;
	manifest: {
		run_id: string;
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

/** Canonical launch result from `POST /v1/backtests/jobs`. */
export interface HarnessJobResult {
	action: string;
	config_path: string;
	workspace: string | null;
	boundary: HarnessLocation | null;
	job_id: string | null;
	state: string | null;
	exit_code: number | null;
	location: string | null;
	result: Record<string, unknown>;
}

/** One harness job row from `GET /v1/backtests/jobs/{id}`. */
export interface HarnessJobStatus {
	job_id: string;
	state: string;
	created_at?: string | null;
	updated_at?: string | null;
	error_message?: string | null;
	[key: string]: unknown;
}

/** One stored run row from `GET /v1/backtests/runs/{id}`. */
export interface HarnessRunRecord {
	run_id: string;
	status?: string;
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

/** The artifact manifest from `GET /v1/backtests/runs/{id}/artifacts`. */
export interface HarnessArtifactManifest {
	run_id: string;
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

// ---------- Screener (thesis.screening HTTP surface, /v1/workspaces/{ws}/screening) ----------

/**
 * One declarative filter: a preset kind plus validated parameters. The kind
 * vocabulary and per-kind parameter validation mirror
 * `src/thesis/screening/presets.py` (every violation is a backend
 * FilterError at declaration time).
 */
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

// ---------- CLEO projection + workspace state ----------

export interface CleoPlanStep {
	id: string;
	title: string;
	state: 'done' | 'active' | 'pending' | 'blocked';
	gate: string | null;
}

export interface CleoPlan {
	plan_id: string;
	title: string;
	current_stage: string;
	stages: { name: string; state: 'done' | 'active' | 'pending' }[];
	steps: CleoPlanStep[];
	updated_at: string;
}

export interface CleoTaskEvent {
	id: string;
	at: string;
	task_id: string;
	kind: 'task.created' | 'task.started' | 'task.completed' | 'gate.passed' | 'gate.blocked';
	detail: string;
}

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

export interface WorkspaceStateData {
	cleo: CleoPlan;
	taskTimeline: CleoTaskEvent[];
	lineages: StrategyLineage[];
	evidence: EvidenceItem[];
	consentAudit: ConsentAuditEntry[];
}

// ---------- Hypothesis validation (thesis.validation HTTP surface, /v1/workspaces/{ws}/validation) ----------

export type ValidationStatus = 'queued' | 'running' | 'completed' | 'failed';

export type ValidationVerdict = 'supported' | 'refuted' | 'inconclusive' | 'pending';

/**
 * One evidence atom cited by a validation. `run_id` routes to the producing
 * backtest/run detail; `artifact` names the object path inside that run.
 */
export interface ValidationEvidenceItem {
	id: string;
	title: string;
	reference: string;
	kind: 'backtest' | 'artifact' | 'knowledge' | 'experiment' | 'upload';
	run_id: string | null;
	artifact: string | null;
}

/** The declarative contract a validation workflow is executed against. */
export interface ValidationContract {
	schema_version: number;
	hypothesis: string;
	metric: string | null;
	threshold: number | null;
	direction: 'increase' | 'decrease' | 'two_sided' | null;
	evidence_requirements: string[];
}

/** A hypothesis-validation workflow record with its verdict and evidence. */
export interface ValidationRecord {
	id: string;
	workspace_id: string;
	status: ValidationStatus;
	claim: string;
	/** Present when the workflow persisted its contract; otherwise render the claim. */
	contract?: ValidationContract | null;
	verdict: ValidationVerdict;
	evidence: ValidationEvidenceItem[];
	created_at: string;
	updated_at: string | null;
}

/** Selectable data backend: bundled fixtures, same-origin local, or remote. */
export type BackendKind = 'fixtures' | 'local' | 'azure';

export interface Capabilities {
	backend: BackendKind;
	chatMessages: boolean;
	executionStream: boolean;
	portfolio: StatusSurface<true>;
	riskDecisions: StatusSurface<true>;
	artifacts: StatusSurface<true>;
}
