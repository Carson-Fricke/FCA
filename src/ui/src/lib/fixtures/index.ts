/**
 * Fixture-backed mode: one JSON document per API route, typed at the
 * boundary so the client hands out contract-shaped records.
 */
import workspaces from './workspaces.json';
import workspaceSnapshot from './workspace-snapshot.json';
import chats from './chats.json';
import chatMessages from './chat-messages.json';
import executions from './executions.json';
import executionEvents from './execution-events.json';
import strategies from './strategies.json';
import backtests from './backtests.json';
import backtestDetail from './backtest-detail.json';
import backtestSeries from './backtest-series.json';
import deployments from './deployments.json';
import orders from './orders.json';
import portfolio from './portfolio.json';
import riskDecisions from './risk-decisions.json';
import widgets from './widgets.json';
import screeners from './screeners.json';
import screenerResults from './screener-results.json';
import workspaceState from './workspace-state.json';
import knowledgeSearch from './knowledge-search.json';
import consentProposal from './consent-proposal.json';
import type {
	BacktestDetail,
	BacktestSummaryRow,
	ChatMessage,
	ChatThread,
	ConsentProposal,
	DeploymentCard,
	EquityPoint,
	ExecutionEvent,
	KnowledgeHit,
	OrderRecord,
	PortfolioSummary,
	RiskDecisionRecord,
	StatusSurface,
	Strategy,
	TradeRecord,
	WidgetDefinition,
	WorkflowExecutionRecord,
	WorkspaceOverview,
	WorkspaceSnapshot,
	WorkspaceStateData,
	WorkspaceSummary
} from '../types';

/**
 * Screener fixture rows are the input vocabulary of api.ts's fixture
 * translator: flat definition rows plus flat result rows. listScreeners
 * translates them into the real ScreenerSavedDefinition contract and the
 * fixture run path maps them into ScreenerRunResult hits, so fixture mode
 * exercises the real shapes end to end. Filter kinds use the engine-native
 * preset names whose single numeric `min` parameter survives translation
 * (avg_dollar_volume_min / min_price / avg_volume_percentile_min).
 */
export interface ScreenerFixtureFilterRow {
	kind: string;
	op: string;
	value: number;
}

export interface ScreenerFixtureDefinitionRow {
	id: string;
	workspace_id: string;
	name: string;
	filters: ScreenerFixtureFilterRow[];
	created_at: string;
	artifact_path: string;
}

export interface ScreenerFixtureResultRow {
	symbol: string;
	last: number;
	volume: number;
	avg_dollar_volume_20d: number;
}

export interface Fixtures {
	workspaces: { items: WorkspaceSummary[] };
	workspaceSnapshot: Record<string, { overview: WorkspaceOverview; counts: WorkspaceSnapshot['counts'] }>;
	chats: Record<string, { items: ChatThread[] }>;
	chatMessages: Record<string, { items: ChatMessage[] }>;
	executions: Record<string, WorkflowExecutionRecord>;
	executionEvents: Record<string, { items: ExecutionEvent[] }>;
	strategies: Record<string, { items: Strategy[] }>;
	backtests: Record<string, { items: BacktestSummaryRow[] }>;
	backtestDetail: Record<string, Omit<BacktestDetail, 'equity' | 'trades'>>;
	backtestSeries: Record<string, { equity: EquityPoint[]; trades: TradeRecord[] }>;
	deployments: Record<string, { items: DeploymentCard[] }>;
	orders: Record<string, { items: OrderRecord[] }>;
	portfolio: Record<string, StatusSurface<PortfolioSummary>>;
	riskDecisions: Record<string, { items: RiskDecisionRecord[] }>;
	widgets: Record<string, { items: WidgetDefinition[] }>;
	screeners: Record<string, { items: ScreenerFixtureDefinitionRow[] }>;
	screenerResults: { items: ScreenerFixtureResultRow[] };
	workspaceState: Record<string, WorkspaceStateData>;
	knowledgeSearch: { items: KnowledgeHit[] };
	consentProposal: { proposal: ConsentProposal };
}

export const fixtures: Fixtures = {
	workspaces,
	workspaceSnapshot,
	chats,
	chatMessages,
	executions,
	executionEvents,
	strategies,
	backtests,
	backtestDetail,
	backtestSeries,
	deployments,
	orders,
	portfolio,
	riskDecisions,
	widgets,
	screeners,
	screenerResults,
	workspaceState,
	knowledgeSearch,
	consentProposal
} as unknown as Fixtures;
