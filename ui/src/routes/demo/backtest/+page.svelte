<script>
	import BacktestView from '$lib/components/backtest-view/BacktestView.svelte';
	import { fixture } from '$lib/data/backtestFixture.js';

	const wins = fixture.trades.filter((t) => t.pnl >= 0).length;
	const pnl = fixture.trades.reduce((a, t) => a + t.pnl, 0);
</script>

<svelte:head>
	<title>Backtest visualizer demo - FCA Thesis</title>
</svelte:head>

<div class="page">
	<h1>Backtest visualizer</h1>
	<p class="sub">
		{fixture.title} &middot; {fixture.trades.length} trades &middot; {wins}W/{fixture.trades.length - wins}L
		&middot; realized PnL {pnl >= 0 ? '+' : ''}{pnl.toLocaleString('en-US', { maximumFractionDigits: 0 })}
	</p>
	<BacktestView title="Equity (fixture)" {...fixture} height={640} />
	<p class="sub note">
		Fixture data only. Real runs arrive through the harness recorder artifacts imported by the
		workspace, served by the Thesis artifact-view endpoint (T207).
	</p>
</div>

<style>
	.note {
		margin-top: 12px;
		font-size: 12px;
	}
</style>
