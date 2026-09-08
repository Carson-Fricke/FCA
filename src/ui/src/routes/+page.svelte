<script>
	import { nodes } from '$lib/data/dagFixture.js';
	import { fixture } from '$lib/data/backtestFixture.js';

	const done = nodes.filter((n) => n.status === 'done').length;
</script>

<div class="page">
	<h1>FCA Thesis UI</h1>
	<p class="sub">
		Component workbench. Roadmap sample: {done}/{nodes.length} tasks done. Backtest sample:
		{fixture.trades.length} trades over {fixture.instruments.length} instruments.
	</p>

	<div class="card">
		<h2><a href="/demo/backtest">Backtest visualizer demo</a></h2>
		<p>
			PnL and equity curve with trade overlays plus per-instrument price panels, one locked time
			axis, synchronized pan and zoom. Fixture data, no backend.
		</p>
	</div>

	<div class="card">
		<h2><a href="/demo/dag">CLEO task DAG demo</a></h2>
		<p>
			Hierarchy and dependency DAG on canvas with pan, zoom, and leaf click-through to task details.
			Fixture data, no backend.
		</p>
	</div>

	<div class="card">
		<h2><a href="/demo/spy0dte-clusters">SPY intraday price-path clustering</a></h2>
		<p>
			k-Shape clustering of normalized intraday price paths across 6 years of SPY data,
			revealing families of price action with per-minute positive/negative breakdowns.
		</p>
	</div>

	<div class="card">
		<h2><a href="/demo/spy0dte">SPY 0DTE intraday study</a></h2>
		<p>
			Implied volatility, option price, realized theta, and realized delta profiles per minute of
			the trading session for 0DTE and 1DTE ladders, plus the intraday movement profile, with
			findings and candidate strategies. Real data: 42 sessions, July&ndash;September 2026.
		</p>
	</div>
</div>
