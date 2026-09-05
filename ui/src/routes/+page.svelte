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
</div>
