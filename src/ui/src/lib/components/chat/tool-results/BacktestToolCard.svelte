<script lang="ts">
	import BacktestReport from '$lib/components/backtest-view/BacktestReport.svelte';
	import { getBacktest, materializeBacktestRun } from '$lib/api';
	import type { BacktestDetail, ToolResult } from '$lib/types';
	import { formatValue, isRecord, readNumber, readRecord, readString } from './safe';

	let { result, workspaceId }: { result: ToolResult; workspaceId: string } = $props();

	const data = $derived(isRecord(result.structured) ? result.structured : {});
	const boundary = $derived(readString(data, 'boundary'));
	const jobId = $derived(readString(data, 'job_id'));
	const runState = $derived(readString(data, 'state'));
	const location = $derived(readString(data, 'location'));
	const exitCode = $derived(readNumber(data, 'exit_code'));
	const metrics = $derived(readRecord(data, 'metrics'));
	const metricRows = $derived(Object.entries(metrics));
	const chatId = $derived(readString(data, 'chat_id'));
	const executionId = $derived(readString(data, 'execution_id'));

	const identifier = $derived(jobId ?? runState);
	const statusText = $derived(runState);
	const normalizedState = $derived((runState ?? '').toLowerCase());
	const isActive = $derived(normalizedState === 'running' || normalizedState === 'queued');
	const isFailed = $derived(
		!result.ok ||
			normalizedState === 'failed' ||
			normalizedState === 'cancelled'
	);

	let detail = $state<BacktestDetail | null>(null);
	let loading = $state(false);
	let loadError = $state('');

	const MATERIALIZE_MAX_ATTEMPTS = 40;
	const MATERIALIZE_INTERVAL_MS = 3000;

	$effect(() => {
		const ws = workspaceId;
		const id = jobId ?? '';
		const chat = chatId;
		const execution = executionId;
		if (!ws || !id) {
			detail = null;
			loading = false;
			loadError = '';
			return;
		}
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout> | null = null;
		let attempts = 0;
		loading = true;
		loadError = '';
		detail = null;

		async function loadById(jobIdValue: string): Promise<void> {
			const next = await getBacktest(ws, jobIdValue);
			if (cancelled) return;
			detail = next;
			loading = false;
		}

		async function resolve(): Promise<void> {
			try {
				const card = await materializeBacktestRun(ws, id, {
					chatId: chat,
					executionId: execution
				});
				if (cancelled) return;
				if (card.backtest) {
					detail = card.backtest;
					loading = false;
					return;
				}
				const cardState = (card.state ?? '').toLowerCase();
				if (card.error) {
					loadError = card.error;
					loading = false;
					return;
				}
				if (cardState === 'failed' || cardState === 'cancelled') {
					loadError = failureMessage;
					loading = false;
					return;
				}
				if (cardState === 'succeeded') {
					await loadById(id);
					return;
				}
				attempts += 1;
				if (attempts >= MATERIALIZE_MAX_ATTEMPTS) {
					loadError = 'Backtest is still running. Reopen the conversation to refresh.';
					loading = false;
					return;
				}
				timer = setTimeout(() => void resolve(), MATERIALIZE_INTERVAL_MS);
			} catch (error: unknown) {
				if (cancelled) return;
				loadError = error instanceof Error ? error.message : 'Failed to load backtest';
				loading = false;
			}
		}

		void resolve();
		return () => {
			cancelled = true;
			if (timer) clearTimeout(timer);
		};
	});

	/**
	 * BacktestReport reads camelCase keys. The backend may publish snake_case,
	 * so carry every original key through and add the camelCase aliases the
	 * report understands.
	 */
	function normalizeStats(
		stats: Record<string, number | string | null>
	): Record<string, number | string | null> {
		const out: Record<string, number | string | null> = { ...stats };
		const aliases: Record<string, string> = {
			sharpe_per_sample: 'sharpePerSample',
			max_drawdown: 'maxDrawdown',
			win_rate: 'hitRate',
			total_return: 'totalReturn',
			total_pnl: 'totalPnl',
			profit_factor: 'profitFactor',
			trade_count: 'tradeCount',
			win_count: 'winCount',
			loss_count: 'lossCount'
		};
		for (const [snake, camel] of Object.entries(aliases)) {
			if (stats[snake] !== undefined && out[camel] === undefined) out[camel] = stats[snake];
		}
		return out;
	}

	const report = $derived.by(() => {
		const current = detail;
		if (!current) return null;
		return {
			title: current.name,
			equity: current.equity ?? [],
			trades: current.trades ?? [],
			stats: normalizeStats(current.stats ?? {}),
			instruments: []
		};
	});

	const failureMessage = $derived(
		readString(isRecord(result.failure) ? result.failure : {}, 'message') ||
			result.summary ||
			'Backtest failed before a report was produced.'
	);
</script>

<div class="space-y-2" data-testid="backtest-tool-card">
	<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 font-mono text-[11px]">
		{#if boundary}<dt class="text-slate-500">boundary</dt><dd class="text-slate-800">{boundary}</dd>{/if}
		{#if jobId}<dt class="text-slate-500">job_id</dt><dd class="text-slate-800">{jobId}</dd>{/if}
		{#if statusText}<dt class="text-slate-500">state</dt><dd class="text-slate-800">{statusText}</dd>{/if}
		{#if exitCode !== null}<dt class="text-slate-500">exit_code</dt><dd class="text-slate-800">{exitCode}</dd>{/if}
		{#if location}<dt class="text-slate-500">location</dt><dd class="text-slate-800">{location}</dd>{/if}
	</dl>

	{#if !boundary && !identifier && exitCode === null && !isFailed}
		<p class="text-xs text-slate-500">{result.summary || 'No structured details reported.'}</p>
	{/if}

	{#if metricRows.length}
		<div>
			<p class="mb-1 text-[10px] tracking-wider text-slate-500 uppercase">metrics</p>
			<div class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 font-mono text-[11px]">
				{#each metricRows as [key, value] (key)}
					<span class="text-slate-500">{key}</span>
					<span class="text-slate-800">{formatValue(value)}</span>
				{/each}
			</div>
		</div>
	{/if}

	{#if jobId}
		{#if loading}
			<p
				class="flex items-center gap-2 text-xs text-slate-500"
				data-testid="backtest-tool-loading"
			>
				<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden="true"></span>
				{isActive ? (statusText ?? 'running') : 'loading report…'}
			</p>
		{:else if loadError}
			<p
				class="rounded border border-loss/30 bg-loss/10 px-2 py-1 text-xs text-loss"
				data-testid="backtest-tool-error"
			>
				{loadError}
			</p>
		{:else if report}
			<div
				class="overflow-hidden rounded-lg border border-surface-700 bg-surface-900/40 p-2"
				data-testid="backtest-tool-report"
			>
				<BacktestReport report={report} height={260} previewHeight={140} defaultExpanded={false} />
			</div>
		{/if}
	{:else if isActive}
		<p
			class="flex items-center gap-2 text-xs text-slate-500"
			data-testid="backtest-tool-running"
		>
			<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-warn" aria-hidden="true"></span>
			{statusText ?? 'running'}
		</p>
	{:else if isFailed}
		<p
			class="rounded border border-loss/30 bg-loss/10 px-2 py-1 text-xs text-loss"
			data-testid="backtest-tool-failed"
		>
			{failureMessage}
		</p>
	{/if}
</div>
