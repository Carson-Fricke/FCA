import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import SplitEquityView from '$lib/components/backtest/SplitEquityView.svelte';
import type { BacktestDetail, EquityPoint } from '$lib/types';

function curve(start: number, n: number): EquityPoint[] {
	const out: EquityPoint[] = [];
	for (let i = 0; i < n; i++) {
		out.push({ t: Date.UTC(2024, 0, 2) + i * 86400000, v: start * (1 + i * 0.001) });
	}
	return out;
}

const base: BacktestDetail = {
	id: 'bt-204',
	workspace_id: 'alpha-lab',
	strategy_id: 'str-1',
	name: 'v3.2',
	period_start: '2024-01-02',
	period_end: '2025-08-29',
	initial_capital: 100000,
	final_capital: 118400,
	net_pnl: 18400,
	equity: curve(100000, 100),
	trades: [],
	stats: { sharpe: 1.31, maxDrawdown: -0.064 },
	split: {
		boundary: '2025-01-31',
		inSample: { returnPct: 9.8, sharpe: 1.42, maxDrawdown: -6.4, trades: 31 },
		outOfSample: { returnPct: 7.0, sharpe: 1.18, maxDrawdown: -4.9, trades: 17 }
	},
	manifest: {
		run_id: 'run-1',
		status: 'completed',
		run_digest: 'a'.repeat(64),
		configuration_digest: 'b'.repeat(64),
		data_start: '2024-01-02',
		data_end: '2025-08-29',
		artifact_count: 1,
		total_bytes: 10
	}
};

describe('Backtest split rendering', () => {
	it('segments equity in-sample/out-of-sample at the declared boundary with metric cards', () => {
		render(SplitEquityView, { props: { backtest: base } });

		const declared = screen.getByTestId('split-declared');
		expect(declared).toBeInTheDocument();
		expect(declared.getAttribute('data-boundary')).toBe('2025-01-31');
		expect(screen.getByText(/Train\/test split declared/i)).toBeInTheDocument();

		// Per-segment metric cards exist for both segments.
		expect(screen.getByText('In-sample return')).toBeInTheDocument();
		expect(screen.getByText('Out-of-sample return')).toBeInTheDocument();
		expect(screen.getByText('In-sample Sharpe')).toBeInTheDocument();
		expect(screen.getByText('Out-of-sample Sharpe')).toBeInTheDocument();

		// Segmented charts for both halves of the curve.
		expect(screen.getByText(/In-sample equity/)).toBeInTheDocument();
		expect(screen.getByText(/Out-of-sample equity/)).toBeInTheDocument();
	});

	it('renders an explicit not-declared state and never fabricates a split', () => {
		render(SplitEquityView, {
			props: { backtest: { ...base, split: null, trades: [] } }
		});

		expect(screen.getByTestId('split-not-declared')).toBeInTheDocument();
		expect(screen.queryByTestId('split-declared')).toBeNull();
		expect(screen.getByText(/split not declared/i)).toBeInTheDocument();
		expect(screen.getByText(/No split is fabricated/i)).toBeInTheDocument();

		// Overall cards only — no per-segment cards.
		expect(screen.queryByText('In-sample return')).toBeNull();
		expect(screen.getByText('Return')).toBeInTheDocument();
	});
});
