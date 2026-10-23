<script lang="ts">
	import BacktestReport from '$lib/components/backtest-view/BacktestReport.svelte';
	import { getBacktest } from '$lib/api';
	import { observeJob } from '$lib/research/jobs';
	import type { BacktestDetail, HarnessJobStatus, ToolResult } from '$lib/types';
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

	const identifier = $derived(jobId ?? runState);
	let observed = $state<HarnessJobStatus | null>(null);
	const statusText = $derived(observed?.state ?? runState);
	const normalizedState = $derived((statusText ?? '').toLowerCase());
	const isActive = $derived(normalizedState === 'running' || normalizedState === 'queued');
	const isFailed = $derived(
		(!observed && !result.ok) ||
			normalizedState === 'failed' ||
			normalizedState === 'cancelled'
	);

	let detail = $state<BacktestDetail | null>(null);
	let loading = $state(false);
	let loadError = $state('');

	$effect(() => {
		const ws = workspaceId;
		const id = jobId ?? '';
		if (!ws || !id) {
			observed = null;
			detail = null;
			loading = false;
			loadError = '';
			return;
		}
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout> | null = null;
		let resolving = false;
		loading = true;
		loadError = '';
		detail = null;
		observed = null;

		async function resolve(): Promise<void> {
			try {
				const next = await getBacktest(ws, id);
				if (cancelled) return;
				detail = next;
				loading = false;
				loadError = '';
			} catch (error: unknown) {
				if (cancelled) return;
				loadError = error instanceof Error ? error.message : 'Failed to load backtest';
				loading = false;
				timer = setTimeout(() => void resolve(), 5000);
			}
		}

		const stop = observeJob(id, ({ job, error }) => {
			if (cancelled) return;
			observed = job;
			loadError = error;
			if (job?.state === 'succeeded' && !resolving) {
				resolving = true;
				void resolve();
			} else if (job?.state === 'failed' || job?.state === 'cancelled') {
				loading = false;
				loadError = job.error_message || 'Execution ended without a report.';
			}
		});
		return () => {
			cancelled = true;
			stop();
			if (timer) clearTimeout(timer);
		};
	});

	const report = $derived.by(() => {
		const current = detail;
		if (!current) return null;
		return {
			title: current.name,
			equity: current.equity ?? [],
			trades: current.trades ?? [],
			stats: current.stats ?? {},
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
		<a href="/w/{workspaceId}/backtests/{jobId}" class="text-xs text-accent hover:underline">Inspect execution →</a>
		{#if loadError && loading}<p class="text-xs text-warn">Status unavailable: {loadError}. Retrying automatically.</p>{/if}
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
