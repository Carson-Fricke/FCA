import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * Focused coverage for T292: the "Run backtest" launch action and the
 * API-driven artifact list. The api module is mocked so the component tests
 * observe only the calls and payloads; a separate case exercises the real
 * fixture-mode `launchBacktest` to guard the exported contract.
 */

const mocks = vi.hoisted(() => ({
	listBacktests: vi.fn(),
	launchBacktest: vi.fn(),
	getBacktest: vi.fn(),
	getHarnessStatus: vi.fn(),
	pollHarnessJob: vi.fn(),
	notify: vi.fn()
}));

vi.mock('$lib/api', () => ({
	listBacktests: mocks.listBacktests,
	launchBacktest: mocks.launchBacktest,
	getBacktest: mocks.getBacktest,
	getHarnessStatus: mocks.getHarnessStatus,
	pollHarnessJob: mocks.pollHarnessJob
}));

vi.mock('$lib/workspace.svelte', () => ({ notify: mocks.notify }));

import { page } from '$app/state';
import BacktestsPage from '../routes/w/[workspace]/backtests/+page.svelte';
import BacktestDetailPage from '../routes/w/[workspace]/backtests/[id]/+page.svelte';
import { fixtures } from '$lib/fixtures';
import type { BacktestDetail, BacktestSummaryRow, WorkspaceSnapshot } from '$lib/types';

const snapshot: WorkspaceSnapshot = {
	workspaces: fixtures.workspaces.items,
	overview: fixtures.workspaceSnapshot['alpha-lab'].overview,
	counts: fixtures.workspaceSnapshot['alpha-lab'].counts
};

const listPageData = { workspaceId: 'alpha-lab', snapshot };

const rows: BacktestSummaryRow[] = [
	{
		id: 'bt-204',
		workspace_id: 'alpha-lab',
		strategy_id: 'str-1',
		name: 'Momentum Breakout v3.2',
		period_start: '2024-01-02',
		period_end: '2025-08-29',
		capital: 100000,
		total_return: 0.184,
		sharpe: 1.31,
		max_drawdown: -0.064,
		win_rate: 0.58,
		trades: 48,
		split_declared: true,
		created_at: '2026-09-08T18:04:00Z',
		run_id: 'run-1',
		chat_id: 'chat-9',
		execution_id: 'exec-9'
	}
];

const accepted = {
	action: 'backtest',
	bundle_ref: 'ui-run',
	environment: 'paper',
	exit_code: 0,
	job_id: 'job-1',
	state: 'accepted',
	request_payload: {
		bundle_ref: 'ui-run',
		config_path: 'config/harness/backtest.yaml',
		environment: 'paper'
	}
};

const detail: BacktestDetail = {
	id: 'bt-204',
	workspace_id: 'alpha-lab',
	strategy_id: 'str-1',
	name: 'Momentum Breakout v3.2 — declared split',
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
	stats: { sharpe: 1.31, maxDrawdown: -0.064 },
	split: null,
	manifest: {
		run_id: 'run-1',
		status: 'completed',
		run_digest: 'a'.repeat(64),
		configuration_digest: 'b'.repeat(64),
		data_start: '2024-01-02',
		data_end: '2025-08-29',
		artifact_count: 2,
		total_bytes: 2048
	},
	artifacts: [
		{
			name: 'manifest.json',
			size: 1024,
			object_path: 'backtests/run-1/artifacts/manifest.json',
			sas_url: null,
			sha256: 'a'.repeat(64)
		},
		{
			name: 'equity.parquet',
			size: 1024,
			object_path: 'backtests/run-1/artifacts/equity.parquet',
			sas_url: null,
			sha256: 'b'.repeat(64)
		}
	]
};

beforeEach(() => {
	vi.clearAllMocks();
	page.params.workspace = 'alpha-lab';
	page.params.id = 'bt-204';
	mocks.listBacktests.mockResolvedValue([]);
	mocks.launchBacktest.mockResolvedValue(accepted);
	mocks.getBacktest.mockResolvedValue(detail);
	mocks.getHarnessStatus.mockResolvedValue({
		mode: 'local',
		thesis_location: 'local',
		harness_location: 'local',
		reachable: true,
		server: {},
		tools: [{ name: 'run_backtest', description: '' }],
		health: null,
		error: null
	});
	mocks.pollHarnessJob.mockResolvedValue({ job_id: 'job-1', state: 'completed' });
});

describe('Backtests launch action (T292)', () => {
	it('launches with the UI defaults, refreshes the list, and toasts the outcome', async () => {
		mocks.listBacktests.mockResolvedValueOnce(rows);
		render(BacktestsPage, { props: { data: listPageData } });

		await waitFor(() => expect(mocks.listBacktests).toHaveBeenCalledTimes(1));
		expect(mocks.listBacktests).toHaveBeenCalledWith('alpha-lab');

		await fireEvent.click(screen.getByTestId('run-backtest'));

		await waitFor(() => expect(mocks.launchBacktest).toHaveBeenCalledTimes(1));
		expect(mocks.launchBacktest).toHaveBeenCalledWith('alpha-lab', {
			bundleRef: 'ui-run',
			configPath: 'config/harness/backtest.yaml',
			environment: 'paper'
		});

		// Refresh re-reads the run history after the launch resolves.
		await waitFor(() => expect(mocks.listBacktests).toHaveBeenCalledTimes(2));
		await waitFor(() =>
			expect(mocks.notify).toHaveBeenCalledWith(expect.stringContaining('Backtest launched'))
		);
		expect(mocks.notify.mock.calls.at(-1)?.[1]).not.toBe(true);
	});

	it('surfaces a failed launch through the error toast without refreshing', async () => {
		mocks.launchBacktest.mockRejectedValueOnce(new Error('harness_unavailable'));
		render(BacktestsPage, { props: { data: listPageData } });

		await waitFor(() => expect(mocks.listBacktests).toHaveBeenCalledTimes(1));
		await fireEvent.click(screen.getByTestId('run-backtest'));

		await waitFor(() =>
			expect(mocks.notify).toHaveBeenCalledWith(
				expect.stringContaining('harness_unavailable'),
				true
			)
		);
		expect(mocks.listBacktests).toHaveBeenCalledTimes(1);
	});

	it('fixture mode resolves a synthetic accepted launch result', async () => {
		const actual = await vi.importActual<typeof import('$lib/api')>('$lib/api');
		const outcome = await actual.launchBacktest('alpha-lab', {
			bundleRef: 'ui-run',
			configPath: 'config/harness/backtest.yaml',
			environment: 'paper'
		});

		expect(outcome.action).toBe('backtest');
		expect(outcome.state).toBe('accepted');
		expect(outcome.bundle_ref).toBe('ui-run');
		expect(outcome.request_payload).toMatchObject({
			config_path: 'config/harness/backtest.yaml',
			environment: 'paper'
		});
	});
});

describe('Backtest origin links', () => {
	it('links a run back to the chat that started it', async () => {
		mocks.listBacktests.mockResolvedValueOnce(rows);
		render(BacktestsPage, { props: { data: listPageData } });

		await waitFor(() => expect(screen.getByTestId('backtest-origin-chat')).toBeInTheDocument());
		const link = screen.getByTestId('backtest-origin-chat');
		expect(link).toHaveAttribute('href', '/w/alpha-lab/conversations?chat=chat-9');
		expect(screen.getByTestId('backtest-origin')).toHaveTextContent('exec-9');
	});
});

describe('Backtest detail artifacts (T292)', () => {
	it('renders equity, trades, and API artifacts rather than fixtures', async () => {
		render(BacktestDetailPage);

		await waitFor(() => expect(screen.getByTestId('backtest-artifacts')).toBeInTheDocument());
		expect(mocks.getBacktest).toHaveBeenCalledWith('alpha-lab', 'bt-204');

		// Equity/trade content comes from getBacktest.
		expect(screen.getByText(/Momentum Breakout v3.2/)).toBeInTheDocument();
		expect(screen.getByText('AAPL')).toBeInTheDocument();

		const section = screen.getByTestId('backtest-artifacts');
		expect(section).toHaveTextContent('manifest.json');
		expect(section).toHaveTextContent('equity.parquet');
		expect(section).toHaveTextContent('1.0 KB');
		expect(section).toHaveTextContent('aaaaaaaaaaaa…');
		expect(section).toHaveTextContent('backtests/run-1/artifacts/manifest.json');
	});

	it('omits the artifact section when the API publishes none', async () => {
		mocks.getBacktest.mockResolvedValueOnce({ ...detail, artifacts: [] });
		render(BacktestDetailPage);

		await waitFor(() => expect(screen.getByText(/Momentum Breakout/)).toBeInTheDocument());
		expect(screen.queryByTestId('backtest-artifacts')).toBeNull();
	});
});
