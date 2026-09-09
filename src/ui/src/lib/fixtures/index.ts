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
	ScreenerDefinition,
	ScreenerResultRow,
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
	screeners: Record<string, { items: ScreenerDefinition[] }>;
	screenerResults: { items: ScreenerResultRow[] };
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
