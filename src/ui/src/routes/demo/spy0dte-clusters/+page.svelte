<script>
	import TimeSeriesChart from '$lib/components/charts/TimeSeriesChart.svelte';
	import { clusters } from '$lib/data/spy0dteClusters.js';
	import { classification } from '$lib/data/spy0dteClassification.js';

	const cm = clusters.meta;
	const height = 260;
	const minSpan = 5 * 60 * 1000;
	let activeCluster = $state(0);

	const palette = ['#22d3ee', '#f472b6', '#fbbf24', '#34d399', '#a78bfa', '#f87171', '#60a5fa', '#fb923c'];

	function centroidPoints(cluster) {
		const base = 1788813000000;
		return cluster.centroid.map((v, i) => ({ t: base + i * 60000, v }));
	}

	function bandPoints(cluster, field) {
		const base = 1788813000000;
		return cluster[field].map((v, i) => ({ t: base + i * 60000, v }));
	}

	function pctPosPoints(cluster) {
		const base = 1788813000000;
		return cluster.pctPositive.map((v, i) => ({ t: base + i * 60000, v }));
	}

	function pctNegPoints(cluster) {
		const base = 1788813000000;
		return cluster.pctNegative.map((v, i) => ({ t: base + i * 60000, v }));
	}

	function overallPosPoints() {
		const base = 1788813000000;
		return clusters.overall.pctPositive.map((v, i) => ({ t: base + i * 60000, v }));
	}

	function overallNegPoints() {
		const base = 1788813000000;
		return clusters.overall.pctNegative.map((v, i) => ({ t: base + i * 60000, v }));
	}

	function allCentroidsSeries() {
		return clusters.clusters.map((c, i) => ({
			name: `C${c.cluster} (${c.count})`,
			data: centroidPoints(c),
			render: 'line',
			color: palette[i % palette.length]
		}));
	}

	function accuracyPoints() {
		const base = 1788813000000;
		return classification.accuracyByMinute.map((a) => ({
			t: base + a.minute * 60000,
			v: a.accuracy * 100
		}));
	}

	function baselinePoints() {
		const base = 1788813000000;
		return classification.accuracyByMinute.map((a) => ({
			t: base + a.minute * 60000,
			v: a.randomBaseline * 100
		}));
	}

	const activeIQR = $derived(
		classification.iqrBands.find((b) => b.cluster === activeCluster)
	);
</script>

<svelte:head>
	<title>SPY intraday price-path clustering - FCA Thesis</title>
</svelte:head>

<div class="page">
	<h1>SPY intraday price-path clustering</h1>
	<p class="sub">
		{cm.sessions} sessions over {cm.years} years ({cm.firstSession} &rarr; {cm.lastSession})
		&middot; {cm.algorithm}
		&middot; {cm.normalized}
	</p>

	<div class="card">
		<h2>All cluster centroids (normalized % change from open)</h2>
		<TimeSeriesChart
			title="{cm.clusters} families of intraday price action"
			axis="time"
			render="line"
			{height}
			{minSpan}
			series={allCentroidsSeries()}
		/>
		<div class="legend-row">
			{#each clusters.clusters as c, i}
				<button
					class="legend-btn"
					class:active={activeCluster === c.cluster}
					style="border-color: {palette[i % palette.length]}"
					onclick={() => (activeCluster = c.cluster)}
				>
					<span class="dot" style="background: {palette[i % palette.length]}"></span>
					C{c.cluster} ({c.count} sessions)
				</button>
			{/each}
		</div>
	</div>

	{#each clusters.clusters as c, i}
		{#if activeCluster === c.cluster}
			<div class="card">
				<h2>
					<span class="dot" style="background: {palette[i % palette.length]}"></span>
					Cluster {c.cluster}: {c.count} sessions &middot; avg return {c.avgReturn > 0 ? '+' : ''}{c.avgReturn}%
				</h2>
				<div class="grid">
					<TimeSeriesChart
						title="Centroid path with 25th–75th percentile band"
						axis="time"
						render="line"
						{height}
						{minSpan}
						series={[
							{ name: 'centroid', data: centroidPoints(c), render: 'line' },
							{ name: '25th pct', data: activeIQR ? bandPoints(activeIQR, 'p25') : [], render: 'line' },
							{ name: '75th pct', data: activeIQR ? bandPoints(activeIQR, 'p75') : [], render: 'line' },
							{ name: '10th pct', data: activeIQR ? bandPoints(activeIQR, 'p10') : [], render: 'line' },
							{ name: '90th pct', data: activeIQR ? bandPoints(activeIQR, 'p90') : [], render: 'line' }
						]}
					/>
					<TimeSeriesChart
						title="% of sessions with positive / negative price at each minute"
						axis="time"
						render="line"
						{height}
						{minSpan}
						series={[
							{ name: '% positive', data: pctPosPoints(c), render: 'line' },
							{ name: '% negative', data: pctNegPoints(c), render: 'line' }
						]}
					/>
				</div>
				<p class="caveat">
					The shaded band shows the 10th–90th percentile range of outcomes within this
					family. The 25th–75th percentile (IQR) is the tighter inner band: 50% of sessions
					in this cluster land inside it. Wide bands = high within-family variance.
				</p>
			</div>
		{/if}
	{/each}

	<div class="card">
		<h2>Classification accuracy: how early can you identify the day's family?</h2>
		<TimeSeriesChart
			title="Nearest-centroid classification accuracy using only the path through minute N"
			axis="time"
			render="line"
			height={220}
			{minSpan}
			series={[
				{ name: 'accuracy %', data: accuracyPoints(), render: 'line', fill: true },
				{ name: 'random baseline', data: baselinePoints(), render: 'line' }
			]}
		/>
		<p class="caveat">
			For each minute N, the price path through minute N is classified against the cluster
			centroids computed on the same truncated paths. Accuracy above the random baseline
			({classification.meta.randomBaseline * 100}%) means the partial path carries information
			about the final family. The signal is weak before minute 120 (~29% at 2 hours), useful
			by minute 240 (~46% at 4 hours), and strong after minute 300 (~60% at 5 hours).
			Practical takeaway: the first 90 minutes are not enough for confident regime
			classification — the day's family doesn't separate until 3–5 hours in.
		</p>
	</div>

	<div class="card">
		<h2>All sessions: % positive vs negative at each minute</h2>
		<TimeSeriesChart
			title="Pooled across all {cm.sessions} sessions"
			axis="time"
			render="line"
			{height}
			{minSpan}
			series={[
				{ name: '% positive', data: overallPosPoints(), render: 'line' },
				{ name: '% negative', data: overallNegPoints(), render: 'line' }
			]}
		/>
		<p class="caveat">
			For each minute of the trading session, the percentage of sessions where the normalized
			price is above (positive) or below (negative) the opening price. The crossover point shows
			where the average session transitions from net-positive to net-negative intraday drift.
		</p>
	</div>

	<div class="card findings">
		<h2>Method</h2>
		<p>
			Each session's 390-minute SPY price path is normalized to percentage change from the
			opening price, then padded or truncated to exactly 390 minutes. Sessions are clustered
			using the k-Shape algorithm (Paparrizos &amp; Gravano, 2015), which uses shape-based
			distance (SBD) computed via normalized cross-correlation. SBD is invariant to shifting
			and scaling, making it ideal for comparing price paths regardless of absolute price level.
			Cluster centroids are computed via the k-Shape shape-extraction procedure, which aligns
			members by cross-correlation before averaging. For each cluster, the percentage of member
			sessions with positive vs negative normalized price at each minute reveals the internal
			structure of each price-action family.
		</p>
	</div>
</div>

<style>
	.legend-row {
		display: flex;
		gap: 8px;
		margin-top: 10px;
		flex-wrap: wrap;
	}
	.legend-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px;
		border: 1px solid rgba(148, 163, 184, 0.2);
		border-radius: 4px;
		background: transparent;
		color: var(--muted);
		font-size: 11px;
		cursor: pointer;
	}
	.legend-btn.active {
		background: rgba(34, 211, 238, 0.1);
		border-color: var(--accent);
		color: var(--accent);
	}
	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		display: inline-block;
	}
	.grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}
	.findings {
		max-width: 900px;
	}
	.findings h2 {
		margin-top: 0;
	}
	.caveat {
		font-size: 12px;
		color: var(--muted);
		margin-top: 8px;
	}
</style>
