<script lang="ts">
	import { page } from '$app/state';
	import HistoryTimeline from '$lib/components/artifacts/HistoryTimeline.svelte';
	import { getStrategy, listBacktestPage } from '$lib/api';
	import type { BacktestCatalogPage, Strategy } from '$lib/types';
	const workspaceId = $derived(page.params.workspace ?? '');
	const strategyId = $derived(page.params.id ?? '');
	let strategy = $state<Strategy | null>(null);
	let runs = $state<BacktestCatalogPage | null>(null);
	let error = $state('');
	let runsError = $state('');
	let tab = $state('Source');
	let refreshKey = $state(0);
	$effect(() => {
		const ws = workspaceId; const id = strategyId; void refreshKey;
		let cancelled = false; let timer: ReturnType<typeof setTimeout> | undefined;
		strategy = null; runs = null; error = ''; runsError = '';
		getStrategy(ws, id).then(item => { if (!cancelled) strategy = item; }).catch(cause => { if (!cancelled) error = cause instanceof Error ? cause.message : 'Strategy unavailable'; });
		async function refreshRuns() {
			try { const next = await listBacktestPage(ws, { strategyId: id, limit: 50 }); if (cancelled) return; runs = next; runsError = ''; if (next.items.some(row => !['succeeded', 'failed', 'cancelled'].includes(row.state) || row.status_error)) timer = setTimeout(refreshRuns, 5000); }
			catch (cause) { if (!cancelled) { runsError = cause instanceof Error ? cause.message : 'Executions unavailable'; timer = setTimeout(refreshRuns, 6000); } }
		}
		void refreshRuns();
		return () => { cancelled = true; if (timer) clearTimeout(timer); };
	});
</script>

<svelte:head><title>{strategy?.name ?? 'Strategy'} — Thesis</title></svelte:head>
<div class="mx-auto max-w-6xl space-y-4">
	<a href="/w/{workspaceId}/strategies" class="text-xs text-slate-500 hover:text-accent">← Strategies</a>
	{#if error}<p role="alert" class="rounded-xl border border-loss/30 p-4 text-sm text-loss">{error}</p>{/if}
	{#if strategy}
		<header class="flex flex-wrap items-start justify-between gap-3"><div><h1 class="text-xl font-semibold text-slate-800">{strategy.name}</h1><p class="mt-1 text-xs text-slate-500">{strategy.status} · {strategy.inputs?.flatMap(input => input.symbols).filter((symbol, index, symbols) => symbols.indexOf(symbol) === index).join(', ') || strategy.symbol || 'No market subscriptions'}</p></div><button type="button" class="rounded-lg border border-surface-600 px-3 py-1.5 text-xs text-slate-600" onclick={() => refreshKey += 1}>Refresh</button></header>
		<p class="text-sm text-slate-600">{strategy.description}</p>
		<p class="break-all font-mono text-xs text-slate-500">Revision {strategy.version ?? 'unavailable'}<br />Commit {strategy.git_commit ?? 'unavailable'}</p>
		<nav class="flex gap-5 border-b border-surface-700" aria-label="Strategy details">{#each ['Source', 'Inputs', 'History', 'Backtests'] as name}<button type="button" class="border-b-2 px-1 py-3 text-xs {tab === name ? 'border-accent text-accent' : 'border-transparent text-slate-500'}" onclick={() => tab = name}>{name}{name === 'Backtests' && runs ? ` (${runs.total})` : ''}</button>{/each}</nav>
		{#if tab === 'Source'}<section class="rounded-xl border border-surface-700 bg-surface-900"><pre class="max-h-[42rem] overflow-auto p-4 font-mono text-xs leading-relaxed text-slate-700" data-testid="strategy-code">{strategy.code}</pre>{#if strategy.source_truncated}<p class="p-4 text-xs text-warn">This source preview is truncated. Open the artifact browser to inspect individual files.</p>{/if}</section>
		{:else if tab === 'Inputs'}<section class="rounded-xl border border-surface-700 p-4"><h2 class="text-sm font-semibold text-slate-800">Declared subscriptions</h2><pre class="mt-3 overflow-auto text-xs text-slate-600">{JSON.stringify(strategy.inputs ?? [], null, 2)}</pre></section><details class="rounded-xl border border-surface-700 p-4"><summary class="cursor-pointer text-sm text-slate-700">Manifest</summary><pre class="mt-3 overflow-auto text-xs text-slate-600">{strategy.manifest_text ?? 'No manifest preview available.'}</pre></details>
		{:else if tab === 'History'}<HistoryTimeline history={strategy.history ?? []} />
		{:else}
			{#if runsError}<p role="alert" class="text-sm text-warn">{runsError}</p>{/if}
			<p class="text-xs text-slate-500">Each execution retains its own source and parameters. Earlier runs may use an earlier strategy revision.</p>
			{#if runs?.items.length}<div class="overflow-x-auto rounded-xl border border-surface-700"><table class="w-full text-left text-xs"><thead class="bg-surface-900 text-slate-500"><tr><th class="p-3">Execution</th><th class="p-3">State</th><th class="p-3">Source SHA-256</th><th class="p-3">Window</th></tr></thead><tbody>{#each runs.items as run (run.job_id)}<tr class="border-t border-surface-700"><td class="p-3"><a class="font-mono text-accent hover:underline" href="/w/{workspaceId}/backtests/{run.job_id}">{run.job_id}</a></td><td class="p-3 text-slate-700">{run.state}{run.status_error ? ' · status unavailable' : ''}</td><td class="p-3 font-mono text-slate-500" title={run.programs.find(program => program.strategy_id === strategyId)?.source_sha256 ?? ''}>{run.programs.find(program => program.strategy_id === strategyId)?.source_sha256?.slice(0, 12) ?? 'unavailable'}</td><td class="p-3 text-slate-500">{run.period_start?.slice(0, 10)} → {run.period_end?.slice(0, 10)}</td></tr>{/each}</tbody></table></div>{:else if runs}<p class="py-6 text-sm text-slate-500">No backtests have been submitted for this strategy.</p>{:else if !runsError}<p role="status" class="py-6 text-sm text-slate-500">Loading executions…</p>{/if}
			<a class="text-xs text-accent hover:underline" href="/w/{workspaceId}/backtests?strategy_id={encodeURIComponent(strategyId)}">View all executions →</a>
		{/if}
	{:else if !error}<p role="status" class="py-8 text-sm text-slate-500">Loading strategy…</p>{/if}
</div>
