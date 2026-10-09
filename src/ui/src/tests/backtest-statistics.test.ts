import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import BacktestReport from '$lib/components/backtest-view/BacktestReport.svelte';
import StatsTable from '$lib/components/backtest-view/StatsTable.svelte';
import fixture from './fixtures/backtest-reports.json';

describe('canonical backtest statistics presentation', () => {
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
