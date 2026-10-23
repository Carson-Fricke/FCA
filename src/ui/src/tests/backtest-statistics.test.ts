import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import BacktestReport from '$lib/components/backtest-view/BacktestReport.svelte';
import StatsTable from '$lib/components/backtest-view/StatsTable.svelte';
import SplitEquityView from '$lib/components/backtest/SplitEquityView.svelte';
import { getBacktest } from '$lib/api';
import fixture from './fixtures/backtest-reports.json';

describe('canonical backtest statistics presentation', () => {
  it('renders published canonical metrics in the backtest detail summary even without an equity preview', async () => {
    const detail = await getBacktest('alpha-lab', 'job-0a1b2c3d4e5f60718293a4b5c6d7e8f9');
    render(SplitEquityView, {props:{backtest:{...detail, split:null, stats:fixture.closed_trades.stats, equity:[], trades:[]}}});
    const cards = screen.getAllByTestId('metric-card');
    expect(cards[0]).toHaveTextContent('Return -0.2%');
    expect(cards[1]).toHaveTextContent('Sharpe (per sample) -0.41');
    expect(cards[2]).toHaveTextContent('Max drawdown 2.0%');
    expect(cards[3]).toHaveTextContent('Trades 3');
    expect(screen.getByTestId('equity-preview-unavailable')).toHaveTextContent('Published statistics are shown above.');
  });
  it('shows each canonical metric once and attaches undefined reasons', () => {
    render(StatsTable, { props: { stats: fixture.no_net_losses.stats } });
    expect(screen.getAllByText('Profit factor')).toHaveLength(1);
    expect(screen.getByText('(no_net_losses)')).toBeInTheDocument();
    expect(screen.queryByText('Profit Factor')).not.toBeInTheDocument();
  });
  it('uses the published max drawdown even when the chart has no equity points', async () => {
    render(BacktestReport, { props: { report: fixture.closed_trades, defaultExpanded: true } });
    await fireEvent.click(screen.getByRole('tab', { name: 'Risk analysis' }));
    expect(screen.getByText('Drawdown % · max 2.0%')).toBeInTheDocument();
  });
  it('preserves undefined canonical drawdown and its reason in the chart title', async () => {
    render(BacktestReport, { props: { report: fixture.negative_equity, defaultExpanded: true } });
    await fireEvent.click(screen.getByRole('tab', { name: 'Risk analysis' }));
    expect(screen.getByText('Drawdown % · max undefined (negative_equity)')).toBeInTheDocument();
  });
});
