<script>
	import TimeSeriesChart from '$lib/components/charts/TimeSeriesChart.svelte';
	import { profile } from '$lib/data/spy0dteProfile.js';
	import { backtests } from '$lib/data/spy0dteBacktests.js';

	const m = profile.meta;
	const height = 230;
	const minSpan = 5 * 60 * 1000;

	const strategyLabels = {
		openingCalendar: 'Opening Calendar',
		middayCondor: 'Midday Iron Condor',
		powerHourStraddle: 'Power-Hour Straddle',
		overnightStraddle: 'Overnight ATM Straddle'
	};
	const strategyOrder = ['openingCalendar', 'middayCondor', 'powerHourStraddle', 'overnightStraddle'];
	let activeStrategy = $state('openingCalendar');

	function equityPoints(summary) {
		if (!summary || !summary.equity || !summary.days) return [];
		return summary.days.map((day, i) => ({
			t: (1788813000000 + i * 86400000),
			v: summary.equity[i]
		}));
	}

	function pnlPoints(summary) {
		if (!summary || !summary.days) return [];
		return summary.days.map((day, i) => ({
			t: (1788813000000 + i * 86400000),
			v: day.pnl
		}));
	}

	function callPut(group) {
		return [
			{ name: 'call', data: group.call, render: 'line' },
			{ name: 'put', data: group.put, render: 'line' }
		];
	}

	function callPutWithModel(group) {
		return [
			{ name: 'call actual', data: group.call, render: 'line' },
			{ name: 'put actual', data: group.put, render: 'line' },
			{ name: 'call model', data: group.modelCall, render: 'line' },
			{ name: 'put model', data: group.modelPut, render: 'line' }
		];
	}
</script>

<svelte:head>
	<title>SPY 0DTE intraday study - FCA Thesis</title>
</svelte:head>

<div class="page">
	<h1>SPY 0DTE intraday study</h1>
	<p class="sub">
		{m.sessions} trading sessions ({m.firstSession} &rarr; {m.lastSession}) &middot; expiring-day
		(0DTE) and day-before-expiry (1DTE) ladders &middot; {m.priceSource} &middot; one point per
		minute, {m.smoothing} &middot; {m.method}
	</p>

	<div class="card">
		<h2>Current pricing model (backward strip at each tick's own IV)</h2>
		<div class="grid">
			<TimeSeriesChart
				title="Implied volatility - 0DTE"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={[{ name: 'IV', data: profile.iv.dte0, render: 'line', fill: true }]}
			/>
			<TimeSeriesChart
				title="Implied volatility - 1DTE"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={[{ name: 'IV', data: profile.iv.dte1, render: 'line', fill: true }]}
			/>
			<TimeSeriesChart
				title="Average option price - 0DTE (dollars)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={[{ name: 'price', data: profile.price.dte0, render: 'line', fill: true }]}
			/>
			<TimeSeriesChart
				title="Average option price - 1DTE (dollars)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={[{ name: 'price', data: profile.price.dte1, render: 'line', fill: true }]}
			/>
			<TimeSeriesChart
				title="Realized theta - 0DTE (cents per hour, calls vs puts)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPutWithModel(profile.theta.dte0)}
			/>
			<TimeSeriesChart
				title="Realized theta - 1DTE (cents per hour, calls vs puts)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPutWithModel(profile.theta.dte1)}
			/>
			<TimeSeriesChart
				title="Realized delta movement - 0DTE (cents per minute, calls vs puts)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPutWithModel(profile.delta.dte0)}
			/>
			<TimeSeriesChart
				title="Realized delta movement - 1DTE (cents per minute, calls vs puts)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPutWithModel(profile.delta.dte1)}
			/>
			<TimeSeriesChart
				title="Vol repricing - 0DTE (cents per minute, calls vs puts)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.vol.dte0)}
			/>
			<TimeSeriesChart
				title="Vol repricing - 1DTE (cents per minute, calls vs puts)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.vol.dte1)}
			/>
			<TimeSeriesChart
				title="Share of the day's price movement (SPY, per minute)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={[{ name: '% of day', data: profile.movement.share, render: 'line', fill: true }]}
			/>
		</div>
	</div>

	<div class="card">
		<h2>Fixed IV model (SMA-30 of each contract's own IV, both ticks priced alike)</h2>
		<div class="grid">
			<TimeSeriesChart
				title="Realized theta - 0DTE (fixed IV, cents per hour)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.thetaFixed.dte0)}
			/>
			<TimeSeriesChart
				title="Realized theta - 1DTE (fixed IV, cents per hour)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.thetaFixed.dte1)}
			/>
			<TimeSeriesChart
				title="Realized delta movement - 0DTE (fixed IV, cents per minute)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.deltaFixed.dte0)}
			/>
			<TimeSeriesChart
				title="Realized delta movement - 1DTE (fixed IV, cents per minute)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.deltaFixed.dte1)}
			/>
		</div>
	</div>

	<div class="card">
		<h2>
			Movement-driven IV model (opening IV scaled per tick by SMA-7 movement share, both ticks
			priced alike)
		</h2>
		<div class="grid">
			<TimeSeriesChart
				title="Realized theta - 0DTE (movement IV, cents per hour)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.thetaMove.dte0)}
			/>
			<TimeSeriesChart
				title="Realized theta - 1DTE (movement IV, cents per hour)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.thetaMove.dte1)}
			/>
			<TimeSeriesChart
				title="Realized delta movement - 0DTE (movement IV, cents per minute)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.deltaMove.dte0)}
			/>
			<TimeSeriesChart
				title="Realized delta movement - 1DTE (movement IV, cents per minute)"
				axis="time"
				render="line"
				{height}
				{minSpan}
				series={callPut(profile.deltaMove.dte1)}
			/>
		</div>
	</div>

	<div class="card">
		<h2>Strategy backtests</h2>
		<div class="tabs">
			{#each strategyOrder as key}
				<button
					class="tab"
					class:active={activeStrategy === key}
					onclick={() => (activeStrategy = key)}
				>
					{strategyLabels[key]}
				</button>
			{/each}
		</div>
		{#if backtests.strategies[activeStrategy] && backtests.strategies[activeStrategy].count > 0}
			{@const s = backtests.strategies[activeStrategy]}
			<div class="stats">
				<div class="stat"><span class="label">Trades</span><span class="value">{s.count}</span></div>
				<div class="stat"><span class="label">Total P&L</span><span class="value" class:negative={s.totalPnl < 0}>${s.totalPnl.toFixed(2)}</span></div>
				<div class="stat"><span class="label">Win rate</span><span class="value">{(s.winRate * 100).toFixed(0)}%</span></div>
				<div class="stat"><span class="label">Mean</span><span class="value" class:negative={s.meanPnl < 0}>${s.meanPnl.toFixed(2)}</span></div>
				<div class="stat"><span class="label">Best</span><span class="value positive">+${s.best.toFixed(2)}</span></div>
				<div class="stat"><span class="label">Worst</span><span class="value negative">${s.worst.toFixed(2)}</span></div>
				<div class="stat"><span class="label">Max DD</span><span class="value negative">${s.maxDrawdown.toFixed(2)}</span></div>
			</div>
			<div class="bt-grid">
				<TimeSeriesChart
					title="Cumulative P&L"
					render="line"
					height={200}
					{minSpan}
					axis="date"
					series={[{ name: 'equity', data: equityPoints(s), render: 'line', fill: true }]}
				/>
				<TimeSeriesChart
					title="Per-trade P&L"
					render="line"
					height={200}
					{minSpan}
					axis="date"
					series={[{ name: 'P&L', data: pnlPoints(s), render: 'line' }]}
				/>
			</div>
		{:else}
			<p class="caveat">No qualifying trades.</p>
		{/if}
		<p class="caveat">
			Simulated from trade-print medians at exact strikes and minutes. No fill slippage, no
			commission, no bid/ask spread. Entry and exit prices are the actual NBBO-proxy medians the
			market printed in the relevant minute.
		</p>
	</div>

	<div class="card findings">
		<h2>Method</h2>
		<p>
			Every observation is repriced exactly at its own current implied volatility. Each observed
			one-minute change is decomposed <em>backward</em>: strip the implied-volatility move first,
			then the time move, then the spot move &mdash; the three components sum exactly to the
			observed change. The current-model charts show that strip at each tick's own IV (theta at
			the prevailing vol, delta as the pure spot move, vol repricing as the residual). The fixed
			set re-prices both endpoints of every interval at a trailing SMA-30 of the contract's own
			IV, and the movement set at an opening-anchored IV scaled per tick by the SMA-7 share of
			realized price movement &mdash; in both, spot and time fully explain the model change, so
			there is no vol component to strip.
		</p>
	</div>

	<div class="card findings">
		<h2>Findings</h2>
		<ul>
			<li>
				<strong>Movement is front-loaded.</strong> Roughly 44% of the day's SPY price movement
				lands in the first 90 minutes; the midday half-hours carry 3&ndash;4% each and the final
				half-hour about 6%.
			</li>
			<li>
				<strong>0DTE theta runs hot at the open and hot at the close.</strong> Average realized
				bleed is ~1.4&times; the flat-IV model across the day, and in the final half hour the
				actual bleed far outruns the model while 36% of the day's total decay is concentrated
				into 6% of the movement.
			</li>
			<li>
				<strong>The opening IV sag is real and expiry-specific.</strong> 0DTE implied volatility
				drops ~7% within 15 minutes of the open on 38 of 42 days; the 1DTE control drops only
				~2.7% and its IV then stays flat near the model all day.
			</li>
			<li>
				<strong>Volatility pricing is inverted within the day.</strong> 0DTE IV is cheapest of
				the day at the open and richest late, while realized movement does the opposite &mdash;
				decay and movement run on two nearly uncorrelated clocks.
			</li>
			<li>
				<strong>1DTE is the control that behaves.</strong> Its theta tracks the model almost
				exactly (~3.3 vs ~3.5 cents/hour) and its IV path is flat: the distortions are
				largely same-day-expiry effects.
			</li>
		</ul>
		<p class="caveat">
			Measured from trade prints rather than bid/ask mids, so reactivity levels near the close
			are partly print-noise. Rates, dividends, and risk costs are not modeled. Exploratory
			evidence &mdash; this sample formed the hypothesis.
		</p>
	</div>

	<div class="card findings">
		<h2>Strategy ideas to explore</h2>
		<ol>
			<li>
				<strong>Opening premium fade (0DTE).</strong> Sell ATM 0DTE premium shortly after the
				open and exit before ~10:30: the window carries above-model decay plus the ~7% IV sag.
				Needs a hard stop for gap-through days &mdash; the sag exists because opens are
				dangerous.
			</li>
			<li>
				<strong>Opening calendar (short 0DTE / long 1DTE).</strong> The differential sag
				(&minus;7% vs &minus;2.7%) is a relative-value trade with much less directional
				exposure; enter 9:35&ndash;9:45, exit by 10:00&ndash;10:15.
			</li>
			<li>
				<strong>Midday vol selling with a morning filter.</strong> IV stays near its daily mean
				through the midday lull while realized movement collapses to 4&ndash;5% annualized.
				Sell midday premium (condors) only after a quiet morning, flat by the final hour.
			</li>
			<li>
				<strong>Power-hour decay harvest.</strong> 36% of the day's bleed lands in the last 30
				minutes against 6% of movement. Short premium with tight delta hedging into the close;
				manage the tail on trend days.
			</li>
			<li>
				<strong>Late-day vs morning IV spread.</strong> IV richens from open to close at
				unchanged (lower) realized movement: long morning IV / short late-day IV as a
				term-structure-within-the-day trade.
			</li>
		</ol>
		<p class="caveat">
			Each idea is a hypothesis for a deterministic backtest with realistic spreads, fills, and
			costs &mdash; none of these are validated. Priority for testing: (2) and (3) carry the
			cleanest structure versus noise.
		</p>
	</div>
</div>

<style>
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
		gap: 14px;
	}
	.card {
		margin-bottom: 14px;
	}
	.card h2 {
		margin-top: 0;
	}
	.findings {
		max-width: 900px;
	}
	.findings h2 {
		margin-top: 0;
	}
	.findings li {
		margin: 8px 0;
		line-height: 1.5;
	}
	.caveat {
		font-size: 12px;
		color: var(--muted);
	}
	.tabs {
		display: flex;
		gap: 6px;
		margin-bottom: 14px;
		flex-wrap: wrap;
	}
	.tab {
		padding: 6px 14px;
		border: 1px solid rgba(148, 163, 184, 0.25);
		border-radius: 6px;
		background: transparent;
		color: var(--muted);
		font-size: 12px;
		cursor: pointer;
	}
	.tab.active {
		background: var(--accent);
		color: #0c1220;
		font-weight: 600;
		border-color: var(--accent);
	}
	.stats {
		display: flex;
		gap: 18px;
		margin-bottom: 14px;
		flex-wrap: wrap;
	}
	.stat {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.stat .label {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted);
	}
	.stat .value {
		font-size: 16px;
		font-weight: 600;
	}
	.stat .value.negative {
		color: #f87171;
	}
	.stat .value.positive {
		color: #34d399;
	}
	.bt-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}
</style>
