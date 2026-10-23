<script lang="ts">
	import { page } from '$app/state';
	import SplitEquityView from '$lib/components/backtest/SplitEquityView.svelte';
	import { getBacktest, inspectBacktest } from '$lib/api';
	import type { BacktestCatalogRow, BacktestDetail } from '$lib/types';

	const workspaceId = $derived(page.params.workspace ?? '');
	const jobId = $derived(page.params.id ?? '');
	let job = $state<BacktestCatalogRow | null>(null);
	let report = $state<BacktestDetail | null>(null);
	let error = $state('');
	let reportError = $state('');
	let tab = $state('Overview');
	let refreshKey = $state(0);
	let loadedIdentity = '';
	const terminal = (state: string) => ['succeeded', 'failed', 'cancelled'].includes(state);
	const json = (value: unknown) => JSON.stringify(value, null, 2);
	const duration = (value: number | null) => value === null ? '—' : `${(value / 60).toFixed(1)} min`;

	$effect(() => {
		const ws = workspaceId; const id = jobId; void refreshKey;
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout> | undefined;
		const identity = JSON.stringify([ws, id]);
		if (loadedIdentity !== identity) { job = null; report = null; loadedIdentity = identity; }
		error = ''; reportError = '';
		async function refresh() {
			try {
				const next = await inspectBacktest(ws, id);
				if (cancelled) return;
				job = next; error = '';
				if (next.state === 'succeeded') {
					try { const loaded = await getBacktest(ws, id); if (!cancelled) { report = loaded; reportError = ''; } }
					catch (cause) { if (!cancelled) reportError = cause instanceof Error ? cause.message : 'Report preview unavailable'; }
				}
				if (!cancelled && (!terminal(next.state) || next.status_error || next.evidence_error || reportError)) timer = setTimeout(refresh, 5000);
			} catch (cause) {
				if (cancelled) return;
				error = cause instanceof Error ? cause.message : 'Could not observe this execution';
				timer = setTimeout(refresh, 6000);
			}
		}
		void refresh();
		return () => { cancelled = true; if (timer) clearTimeout(timer); };
	});
</script>

<svelte:head><title>Backtest {jobId} — Thesis</title></svelte:head>
<div class="mx-auto max-w-6xl space-y-4">
	<a href="/w/{workspaceId}/backtests" class="text-xs text-slate-500 hover:text-accent">← Backtests</a>
	<header class="flex flex-wrap items-start justify-between gap-3">
		<div><h1 class="text-xl font-semibold text-slate-800">{job?.strategy_id || 'Backtest'}</h1><p class="mt-1 break-all font-mono text-xs text-slate-500">{jobId}</p></div>
		<button type="button" class="rounded-lg border border-surface-600 px-3 py-1.5 text-xs text-slate-600" onclick={() => refreshKey += 1}>Refresh</button>
	</header>
	{#if error}<p role="alert" class="rounded-lg border border-warn/30 p-3 text-sm text-warn">{error} Status reads will retry.</p>{/if}
	{#if job}
		<div class="flex flex-wrap items-center gap-3 text-xs text-slate-600">
			<span class="rounded px-2 py-1 {job.state === 'failed' ? 'bg-loss/10 text-loss' : job.state === 'succeeded' ? 'bg-profit/10 text-profit' : 'bg-accent/10 text-accent'}">Execution: {job.state}</span>
			<span>Evidence: {job.evidence === 'no_trades' ? 'No trades' : job.evidence === 'available' ? 'Published results' : 'Incomplete'}</span>
			<span>{job.period_start?.slice(0, 10) ?? '—'} → {job.period_end?.slice(0, 10) ?? '—'} · end exclusive</span>
			{#if job.chat_id}<a class="text-accent hover:underline" href="/w/{workspaceId}/conversations?chat={job.chat_id}" data-testid="backtest-started-in-chat">Started in chat ↗</a>{/if}
		</div>
		{#if job.status_error}<p role="alert" class="rounded-lg border border-warn/30 p-3 text-sm text-warn">Status unavailable: {job.status_error}. Last observed {job.observed_at ?? 'never'}.</p>{/if}
		{#if job.error_message}<p role="alert" class="rounded-lg border border-loss/30 bg-loss/5 p-3 text-sm text-loss">{job.error_message}</p>{/if}
		<nav class="flex flex-wrap gap-5 border-b border-surface-700" aria-label="Backtest details">
			{#each ['Overview', 'Execution', 'Fills & positions', 'Source & inputs'] as name}<button type="button" class="border-b-2 px-1 py-3 text-xs {tab === name ? 'border-accent text-accent' : 'border-transparent text-slate-500'}" onclick={() => tab = name}>{name}</button>{/each}
		</nav>
		{#if tab === 'Overview'}
			{#if report?.manifest.metrics?.equity?.report_interval_ns}<p class="text-xs text-slate-500">Chart: {report.manifest.metrics.equity.report_interval_ns / 1e9}s buckets with observed extrema and fill boundaries. Statistics use all {report.manifest.metrics.equity.samples?.toLocaleString()} causal dispatch samples.</p>{/if}
			{#if report}<SplitEquityView backtest={report} showTrades={false} />{:else}<p class="rounded-xl border border-surface-700 p-5 text-sm text-slate-500">{reportError || (job.state === 'succeeded' ? 'Loading published report…' : 'A report is available after a successful execution.')}</p>{/if}
			<section class="rounded-xl border border-surface-700 p-4"><h2 class="text-sm font-semibold text-slate-800">Execution metrics</h2><dl class="mt-3 grid grid-cols-[max-content_1fr] gap-x-5 gap-y-2 text-xs">{#each Object.entries(job.metrics) as [name, value]}<dt class="text-slate-500">{name.replaceAll('_', ' ')}</dt><dd class="break-all font-mono text-slate-700">{value}</dd>{/each}</dl></section>
		{:else if tab === 'Execution'}
			<div class="flex flex-wrap gap-6 text-sm text-slate-600"><span>Queue: {duration(job.queue_seconds)}</span><span>Execution through publication: {duration(job.execution_seconds)}</span><span>{job.artifact_count} artifacts · {(job.total_bytes / 1024 / 1024).toFixed(2)} MB</span></div>
			<div class="overflow-x-auto rounded-xl border border-surface-700"><table class="w-full text-left text-xs"><thead class="bg-surface-900 text-slate-500"><tr><th class="p-3">Stage</th><th class="p-3">Started</th><th class="p-3">Finished / last progress</th><th class="p-3 text-right">Rows</th></tr></thead><tbody>{#each job.progress as stage, index (index)}<tr class="border-t border-surface-700 text-slate-700"><td class="p-3">{stage.stage.replaceAll('_', ' ')}{stage.completed ? ' ✓' : ''}</td><td class="p-3 font-mono">{stage.started_at}</td><td class="p-3 font-mono">{stage.finished_at ?? stage.updated_at}</td><td class="p-3 text-right font-mono">{stage.rows.toLocaleString()}</td></tr>{/each}</tbody></table></div>
			{#if job.evidence_error}<p class="text-sm text-warn">Evidence unavailable: {job.evidence_error}</p>{/if}
			{#if job.artifacts?.length}<section class="rounded-xl border border-surface-700 p-4" data-testid="backtest-artifacts"><h2 class="text-sm font-semibold text-slate-800">Published artifacts</h2>{#each job.artifacts as artifact}<div class="mt-3 text-xs"><p class="font-medium text-slate-700">{artifact.name} · {(artifact.sizeBytes / 1024 / 1024).toFixed(2)} MB</p><p class="mt-1 break-all font-mono text-slate-500">SHA-256 {artifact.sha256}<br />{artifact.uri}</p></div>{/each}</section>{/if}
		{:else if tab === 'Fills & positions'}
			{#if job.evidence_error}<p class="text-sm text-warn">Evidence unavailable: {job.evidence_error}</p>{/if}
			{#if job.portfolio?.positions?.length}<div class="flex flex-wrap gap-3">{#each job.portfolio.positions as position}<div class="rounded-xl border border-surface-700 px-4 py-3"><p class="text-xs text-slate-500">{String(position.symbol ?? '')} · final quantity</p><p class="mt-1 break-all font-mono text-sm text-slate-800">{String(position.quantity ?? 'unavailable')}</p></div>{/each}</div>{/if}
			<section class="rounded-xl border border-surface-700 p-4"><h2 class="text-sm font-semibold text-slate-800">Final portfolio</h2><pre class="mt-3 overflow-x-auto text-xs text-slate-600">{json(job.portfolio ?? 'No portfolio artifact available.')}</pre></section>
			<section class="rounded-xl border border-surface-700 p-4"><h2 class="text-sm font-semibold text-slate-800">Strategy-reported metrics</h2><pre class="mt-3 overflow-x-auto text-xs text-slate-600">{json(job.custom_metrics ?? {})}</pre></section>
			{#if report}<details class="rounded-xl border border-surface-700 p-4"><summary class="cursor-pointer text-sm font-semibold text-slate-800">Closed trade slices ({report.trades.length})</summary><pre class="mt-3 max-h-[36rem] overflow-auto text-xs text-slate-600">{json(report.trades)}</pre></details>{/if}
			<section class="rounded-xl border border-surface-700 p-4"><h2 class="text-sm font-semibold text-slate-800">Actual fills ({job.fills?.length ?? 0})</h2><p class="mt-1 text-xs text-slate-500">Includes each partial fill, its order identity, quantity, price and fee.</p><pre class="mt-3 max-h-[36rem] overflow-auto text-xs text-slate-600">{json(job.fills ?? [])}</pre></section>
		{:else}
			<p class="text-xs text-slate-500">Input provenance: {job.input_origin === 'harness_accepted' ? 'accepted by the harness' : job.input_origin === 'retained_submission' ? 'retained submission; harness acceptance is unconfirmed' : 'source and inputs are unavailable'}.</p>
			{#each job.programs as program, index (index)}<section class="rounded-xl border border-surface-700 p-4"><h2 class="text-sm font-semibold text-slate-800">{program.strategy_id}</h2><p class="mt-2 break-all font-mono text-xs text-slate-500">Bundle: {program.bundle_hash ?? 'unavailable'}<br />Source SHA-256: {program.source_sha256 ?? 'unavailable'}</p><details class="mt-3"><summary class="cursor-pointer text-xs text-accent">Exact submitted source</summary><pre class="mt-3 max-h-[36rem] overflow-auto text-xs leading-relaxed text-slate-600">{program.source ?? 'Source was not retained in the accepted input.'}</pre></details><details class="mt-3"><summary class="cursor-pointer text-xs text-accent">Parameters</summary><pre class="mt-3 overflow-auto text-xs text-slate-600">{json(program.parameters)}</pre></details></section>{/each}
			<details class="rounded-xl border border-surface-700 p-4"><summary class="cursor-pointer text-sm text-slate-700">Full {job.input_origin === 'harness_accepted' ? 'accepted' : 'submitted'} configuration</summary><pre class="mt-3 max-h-[36rem] overflow-auto text-xs text-slate-600">{json(job.input_origin === 'harness_accepted' ? job.accepted_input : job.submitted_input)}</pre></details>
		{/if}
	{:else if !error}<p role="status" class="py-8 text-sm text-slate-500">Loading execution…</p>{/if}
</div>
