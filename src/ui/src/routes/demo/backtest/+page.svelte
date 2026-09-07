<script>
	import BacktestReport from '$lib/components/backtest-view/BacktestReport.svelte';
	import { fixture } from '$lib/data/backtestFixture.js';

	let camera = $state(null);

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
	<BacktestReport report={fixture} bind:camera height={300} defaultExpanded />
	<p class="sub note">
		Fixture data only. Real runs arrive through the harness recorder artifacts imported by the
		workspace, served by the Thesis artifact-view endpoint (T207). In chat this card starts
		collapsed showing the PnL preview; click the header to expand the tabbed view (PnL &amp; orders,
		securities, risk analysis with drawdown and PnL distribution, model-provided panels, and
		backend-computed statistics). All charts share one zoom camera: pan, zoom, or trim the axis
		grips on any chart and the others follow.
	</p>
</div>

<style>
	.note {
		margin-top: 12px;
		font-size: 12px;
	}
</style>
