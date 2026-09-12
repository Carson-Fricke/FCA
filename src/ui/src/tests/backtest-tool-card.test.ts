import { render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	getBacktest: vi.fn(),
	materializeBacktestRun: vi.fn()
}));

vi.mock('$lib/api', () => ({
	getBacktest: mocks.getBacktest,
	materializeBacktestRun: mocks.materializeBacktestRun
}));

import BacktestToolCard from '$lib/components/chat/tool-results/BacktestToolCard.svelte';
import type { BacktestDetail, ToolResult } from '$lib/types';

const detail: BacktestDetail = {
	id: 'bt-1',
	workspace_id: 'alpha-lab',
	strategy_id: 'str-1',
	name: 'Momentum Breakout',
	period_start: '2024-01-02',
	period_end: '2025-08-29',
	initial_capital: 100000,
	final_capital: 118400,
	net_pnl: 18400,
	equity: [
		{ t: Date.UTC(2024, 0, 2), v: 100000 },
		{ t: Date.UTC(2025, 8, 29), v: 118400 }
	],
	trades: [
		{
			id: 'tr-1',
			symbol: 'AAPL',
			side: 'buy',
			qty: 10,
			entryTime: Date.UTC(2024, 1, 1),
			entryPrice: 180,
			exitTime: Date.UTC(2024, 2, 1),
			exitPrice: 190,
			pnl: 100
		}
	],
	stats: { sharpe: 1.31, max_drawdown: -0.064, win_rate: 0.58, total_return: 0.184 },
	split: null,
	manifest: {
		run_id: 'run-1',
		status: 'completed',
		run_digest: null,
		configuration_digest: null,
		data_start: null,
		data_end: null,
		artifact_count: 0,
		total_bytes: 0
	}
};

function toolResult(structured: Record<string, unknown>): ToolResult {
	return {
		tool: 'run_backtest',
		callId: 'call-1',
		ok: true,
		summary: 'run_backtest completed',
		structured,
		text: 'ok',
		failure: null
	};
}

beforeEach(() => {
	vi.clearAllMocks();
	mocks.getBacktest.mockResolvedValue(detail);
	mocks.materializeBacktestRun.mockResolvedValue({
		status: 'complete',
		run_id: 'run-1',
		state: 'completed',
		backtest_id: 'bt-1',
		backtest: null
	});
});

describe('BacktestToolCard rich report', () => {
	it('fetches the detail and renders the report when backtest_id is present', async () => {
		render(BacktestToolCard, {
			props: {
				result: toolResult({ backtest_id: 'bt-1', state: 'completed' }),
				workspaceId: 'alpha-lab'
			}
		});

		await waitFor(() =>
			expect(screen.getByTestId('backtest-tool-report')).toBeInTheDocument()
		);
		expect(mocks.getBacktest).toHaveBeenCalledWith('alpha-lab', 'bt-1');
		expect(screen.getByText(/Momentum Breakout/)).toBeInTheDocument();
	});

	it('renders a running indicator and does not fetch without a backtest_id', () => {
		render(BacktestToolCard, {
			props: {
				result: toolResult({ state: 'running' }),
				workspaceId: 'alpha-lab'
			}
		});

		expect(screen.getByTestId('backtest-tool-running')).toBeInTheDocument();
		expect(screen.queryByTestId('backtest-tool-report')).toBeNull();
		expect(mocks.getBacktest).not.toHaveBeenCalled();
	});

	it('materializes a run and renders the report when only run_id is present', async () => {
		render(BacktestToolCard, {
			props: {
				result: toolResult({ run_id: 'run-1', state: 'running' }),
				workspaceId: 'alpha-lab'
			}
		});

		await waitFor(() =>
			expect(screen.getByTestId('backtest-tool-report')).toBeInTheDocument()
		);
		expect(mocks.materializeBacktestRun).toHaveBeenCalledWith('alpha-lab', 'run-1', {
			chatId: null,
			executionId: null
		});
		expect(mocks.getBacktest).toHaveBeenCalledWith('alpha-lab', 'bt-1');
	});

	it('renders the failure message for a failed run', () => {
		render(BacktestToolCard, {
			props: {
				result: {
					...toolResult({ state: 'failed' }),
					ok: false,
					summary: 'harness exploded',
					failure: { kind: 'tool_error', message: 'harness exploded' }
				},
				workspaceId: 'alpha-lab'
			}
		});

		expect(screen.getByTestId('backtest-tool-failed')).toHaveTextContent('harness exploded');
	});

	it('shows the harness error for a failed launch with no structured state', () => {
		render(BacktestToolCard, {
			props: {
				result: {
					...toolResult({}),
					ok: false,
					summary: 'run_backtest failed',
					failure: {
						kind: 'tool',
						message: "the local backtest runner could not complete the backtest: could not read backtest config 'examples/backtest.yaml'"
					}
				},
				workspaceId: 'alpha-lab'
			}
		});

		expect(screen.getByTestId('backtest-tool-failed')).toHaveTextContent(
			"could not read backtest config 'examples/backtest.yaml'"
		);
	});
});
