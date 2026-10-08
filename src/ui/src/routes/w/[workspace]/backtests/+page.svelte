<script lang="ts">
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import MetricCard from '$lib/components/common/MetricCard.svelte';
	import {
		getHarnessStatus,
		launchBacktest,
		listBacktests,
		pollHarnessJob
	} from '$lib/api';
	import { shortDate } from '$lib/format';
	import type { BacktestSummaryRow, HarnessStatus } from '$lib/types';
	import { notify } from '$lib/workspace.svelte';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let backtests = $state<BacktestSummaryRow[]>([]);
	let harness = $state<HarnessStatus | null>(null);
	let strategyFilter = $state('all');
	let onlySplit = $state(false);
	let launching = $state(false);

	$effect(() => {
		let cancelled = false;
		listBacktests(workspaceId).then((items) => {
			if (!cancelled) backtests = items;
		});
		// Connection status is advisory: an unreachable harness still reports
		// its topology so the badge can explain why launches will fail.
		getHarnessStatus().then((status) => {
			if (!cancelled) harness = status;
		});
		return () => {
			cancelled = true;
		};
	});

	/** Re-read the workspace's run history from the API. */
	async function refresh(): Promise<void> {
		backtests = await listBacktests(workspaceId);
	}

	/**
	 * Launch a backtest with the UI defaults and refresh the run history. The
	 * canonical jobs route fronts the harness MCP `run_backtest` tool; when it
	 * returns a job id the UI polls until the job reaches a terminal state. The
	 * list reflects whatever the API persisted, never a client-fabricated row.
	 */
	async function runBacktest(): Promise<void> {
		if (launching) return;
		launching = true;
		try {
			const outcome = await launchBacktest(workspaceId, {
				bundleRef: 'ui-run',
				configPath: 'config/harness/backtest.yaml',
				environment: 'paper'
			});
			const settled = outcome.job_id ? await pollHarnessJob(outcome.job_id) : null;
			await refresh();
			const state = settled?.state ?? outcome.state ?? 'accepted';
			notify(
				`Backtest launched (${state}${outcome.job_id ? ` · ${outcome.job_id}` : ''})`
			);
		} catch (error) {
			notify(
				`Backtest launch failed: ${error instanceof Error ? error.message : String(error)}`,
				true
			);
		} finally {
			launching = false;
		}
	}

	const strategyIds = $derived([...new Set(backtests.map((b) => b.strategy_id))]);
	const filtered = $derived(
		backtests.filter(
			(b) =>
				(strategyFilter === 'all' || b.strategy_id === strategyFilter) &&
				(!onlySplit || b.split_declared)
		)
	);

	const summary = $derived.by(() => {
		const returns = backtests.map((b) => b.total_return).filter((v): v is number => v != null);
		const sharpes = backtests.map((b) => b.sharpe_per_sample).filter((v): v is number => v != null);
		return {
			count: backtests.length,
			best: returns.length ? Math.max(...returns) : null,
			avgSharpePerSample: sharpes.length ? sharpes.reduce((a, b) => a + b, 0) / sharpes.length : null
		};
	});
</script>

<svelte:head><title>Backtests — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4">
	<header class="flex flex-wrap items-center gap-3">
		<div>
			<h1 class="text-xl font-semibold text-slate-800">Backtests</h1>
			<p class="text-sm text-slate-500">Run history and declared-split results.</p>
		</div>
		{#if harness}
			<div
				data-testid="harness-status"
				data-reachable={harness.reachable}
				class="flex items-center gap-2 rounded-lg border border-surface-600 bg-surface-900 px-2.5 py-1.5 text-xs"
				title={harness.error ?? undefined}
			>
				<span
					class="h-2 w-2 rounded-full {harness.reachable ? 'bg-profit' : 'bg-loss'}"
				></span>
				<span class="font-mono text-slate-600">
					harness {harness.mode} · {harness.reachable ? 'reachable' : 'unreachable'}
				</span>
				<span class="text-slate-500">{harness.tools.length} tools</span>
			</div>
		{/if}
		<button
			type="button"
			data-testid="run-backtest"
			disabled={launching}
			onclick={runBacktest}
			class="ml-auto rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-60"
		>
			{launching ? 'Launching…' : '＋ Run backtest'}
		</button>
	</header>

	<div class="grid grid-cols-3 gap-3">
		<MetricCard label="Runs" value={summary.count} />
		<MetricCard label="Best return" value={summary.best ?? '—'} format="signedPct" />
		<MetricCard label="Average Sharpe (per sample)" value={summary.avgSharpePerSample ?? '—'} format="ratio" />
	</div>

	<div class="flex flex-wrap items-center gap-2">
		<select
			class="rounded-lg border border-surface-600 bg-surface-900 px-2 py-1.5 text-xs text-slate-700 outline-none"
			bind:value={strategyFilter}
			aria-label="Filter by strategy"
		>
			<option value="all">All strategies</option>
			{#each strategyIds as id (id)}
				<option value={id}>{id}</option>
			{/each}
		</select>
		<label class="flex items-center gap-1.5 text-xs text-slate-500">
			<input type="checkbox" class="accent-cyan-400" bind:checked={onlySplit} />
			split declared only
		</label>
	</div>

	{#if filtered.length === 0}
		<EmptyState title="No backtests" hint="Runs submitted from chats will appear here." />
	{:else}
		<div class="overflow-hidden rounded-xl border border-surface-700 bg-surface-900" data-testid="backtest-history">
			<table class="w-full border-collapse font-mono text-xs">
				<thead>
					<tr class="bg-surface-900 text-left text-[10px] tracking-wider text-slate-500 uppercase">
						<th class="px-4 py-2">Run</th>
						<th class="px-4 py-2">Strategy</th>
						<th class="px-4 py-2">Period</th>
						<th class="px-4 py-2 text-right">Return</th>
						<th class="px-4 py-2 text-right">Sharpe (per sample)</th>
						<th class="px-4 py-2 text-right">Max DD</th>
						<th class="px-4 py-2 text-center">Split</th>
						<th class="px-4 py-2">Origin</th>
					</tr>
				</thead>
				<tbody>
					{#each filtered as backtest (backtest.job_id)}
						<tr class="border-t border-surface-700 hover:bg-surface-900">
							<td class="px-4 py-2">
								<a href="/w/{workspaceId}/backtests/{backtest.job_id}" class="text-accent hover:underline">
									{backtest.name}
								</a>
							</td>
							<td class="px-4 py-2 text-slate-500">{backtest.strategy_id}</td>
							<td class="px-4 py-2 text-slate-500">
								{shortDate(backtest.period_start)} → {shortDate(backtest.period_end)}
							</td>
							<td
								class="px-4 py-2 text-right {backtest.total_return == null
									? 'text-slate-500'
									: backtest.total_return >= 0
										? 'text-profit'
										: 'text-loss'}"
							>
								{backtest.total_return == null ? '—' : `${(backtest.total_return * 100).toFixed(1)}%`}
							</td>
							<td class="px-4 py-2 text-right text-slate-700"
								>{backtest.sharpe_per_sample == null ? '—' : backtest.sharpe_per_sample.toFixed(2)}</td
							>
							<td class="px-4 py-2 text-right text-loss"
								>{backtest.max_drawdown == null
									? '—'
									: `${(backtest.max_drawdown * 100).toFixed(1)}%`}</td
							>
							<td class="px-4 py-2 text-center">
								{#if backtest.split_declared}
									<span class="rounded-full border border-accent/40 px-2 py-0.5 text-[10px] text-accent">declared</span>
								{:else}
									<span class="rounded-full border border-surface-600 px-2 py-0.5 text-[10px] text-slate-500">not declared</span>
								{/if}
							</td>
							<td class="px-4 py-2" data-testid="backtest-origin">
								{#if backtest.chat_id}
									<a
										href="/w/{workspaceId}/conversations?chat={backtest.chat_id}"
										class="text-accent hover:underline"
										title="Open the chat that started this run{backtest.execution_id
											? ` · execution ${backtest.execution_id}`
											: ''}"
										data-testid="backtest-origin-chat"
									>
										chat
									</a>
								{/if}
								{#if backtest.execution_id}
									<span
										class="ml-2 text-slate-500"
										title="execution {backtest.execution_id}"
									>
										{backtest.execution_id.slice(0, 8)}…
									</span>
								{:else if !backtest.chat_id}
									<span class="text-slate-600">—</span>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>
