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
	ConsentProposal,
	DeploymentCard,
	EquityPoint,
	ExecutionEvent,
	KnowledgeHit,
	OrderRecord,
	PortfolioSummary,
	RiskDecisionRecord,
	ScreenerDefinition,
	ScreeningResult,
	StatusSurface,
	Strategy,
	TradeRecord,
	WidgetDefinition,
	WorkspaceOverview,
	WorkspaceSnapshot,
	WorkspaceStateData,
	WorkspaceSummary
} from '../types';
import { decodeWire } from '../wire';
import type { ChatMessageWire, ChatThreadListWire, WorkflowExecutionWire } from '../generated';

export interface Fixtures {
	workspaces: { items: WorkspaceSummary[] };
	workspaceSnapshot: Record<string, { overview: WorkspaceOverview; counts: WorkspaceSnapshot['counts'] }>;
	chats: Record<string, ChatThreadListWire>;
	chatMessages: Record<string, { items: ChatMessageWire[] }>;
	executions: Record<string, WorkflowExecutionWire>;
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
	screenerResults: Record<string, { items: ScreeningResult[] }>;
	workspaceState: Record<string, WorkspaceStateData>;
	knowledgeSearch: { items: KnowledgeHit[] };
	consentProposal: { proposal: ConsentProposal };
}

export const fixtures: Fixtures = {
	workspaces,
	workspaceSnapshot,
	chats: Object.fromEntries(Object.entries(chats).map(([id, doc]) => [id,
		decodeWire('ChatThreadListWire', doc, `fixture/chats/${id}`)])),
	chatMessages: Object.fromEntries(Object.entries(chatMessages).map(([id, doc]) => [id,
		decodeWire('ChatMessageListWire', doc, `fixture/messages/${id}`)])),
	executions: Object.fromEntries(Object.entries(executions).map(([id, doc]) => [id,
		decodeWire('WorkflowExecutionWire', doc, `fixture/execution/${id}`)])),
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
