<script lang="ts">
	import { listScreeners, runScreener, saveScreener } from '$lib/api';
	import { notify } from '$lib/workspace.svelte';
	import { relativeTime } from '$lib/format';
	import type { ScreenerDefinition, ScreenerFilter, ScreenerResultRow } from '$lib/types';

	/**
	 * Screener: preset volume/liquidity filters, results table linking into
	 * strategies, and saved screeners/studies persisted as workspace
	 * artifacts. Extracted as a component so the save flow is testable.
	 */
	let {
		workspaceId,
		initialSaved = [],
		strategies = []
	}: {
		workspaceId: string;
		initialSaved?: ScreenerDefinition[];
		strategies?: { id: string; name: string; symbol: string }[];
	} = $props();

	const PRESETS = [
		{ kind: 'volume' as const, op: '>' as const, value: 5_000_000, label: 'Volume > 5M' },
		{ kind: 'volume' as const, op: '>' as const, value: 20_000_000, label: 'Volume > 20M' },
		{ kind: 'liquidity' as const, op: '>' as const, value: 250_000_000, label: 'Avg $Vol 20d > $250M' },
		{ kind: 'liquidity' as const, op: '>' as const, value: 1_000_000_000, label: 'Avg $Vol 20d > $1B' },
		{ kind: 'price' as const, op: '>' as const, value: 20, label: 'Price > $20' }
	];

	let activeFilters = $state<ScreenerFilter[]>([PRESETS[0]]);
	let results = $state<ScreenerResultRow[]>([]);
	let running = $state(false);
	let saved = $state<ScreenerDefinition[]>([]);
	let saveName = $state('');
	let saving = $state(false);

	$effect(() => {
		if (initialSaved.length && !saved.length) saved = initialSaved;
	});

	function togglePreset(preset: (typeof PRESETS)[number]): void {
		const exists = activeFilters.some(
			(f) => f.kind === preset.kind && f.op === preset.op && f.value === preset.value
		);
		activeFilters = exists
			? activeFilters.filter(
					(f) => !(f.kind === preset.kind && f.op === preset.op && f.value === preset.value)
				)
			: [...activeFilters, { kind: preset.kind, op: preset.op, value: preset.value }];
	}

	async function run(): Promise<void> {
		running = true;
		try {
			results = await runScreener();
		} finally {
			running = false;
		}
	}

	function matchingStrategies(symbol: string): { id: string; name: string }[] {
		return strategies.filter((s) => s.symbol === symbol);
	}

	async function save(): Promise<void> {
		if (!saveName.trim() || !activeFilters.length) return;
		saving = true;
		try {
			const created = await saveScreener(workspaceId, {
				name: saveName.trim(),
				filters: activeFilters
			});
			saved = [...saved, created];
			saveName = '';
			notify(`Screener saved as workspace artifact ${created.artifact_path}`);
		} finally {
			saving = false;
		}
	}
</script>

<div class="grid gap-4 lg:grid-cols-[1fr_320px]" data-testid="screener-panel">
	<div class="space-y-4">
		<section class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h3 class="mb-3 text-sm font-semibold text-slate-200">Filters</h3>
			<div class="flex flex-wrap gap-2">
				{#each PRESETS as preset (`${preset.kind}:${preset.value}`)}
					{@const active = activeFilters.some(
						(f) => f.kind === preset.kind && f.op === preset.op && f.value === preset.value
					)}
					<button
						type="button"
						class="rounded-full border px-3 py-1 text-xs transition
							{active
							? 'border-accent/50 bg-accent/15 text-accent'
							: 'border-surface-600 text-slate-400 hover:border-white/20 hover:text-slate-200'}"
						onclick={() => togglePreset(preset)}
						data-testid="screener-preset"
						data-preset={preset.label}
					>
						{preset.label}
					</button>
				{/each}
			</div>
			<div class="mt-3 flex items-center gap-2">
				<button
					type="button"
					class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25"
					onclick={run}
					disabled={running || !activeFilters.length}
					data-testid="screener-run"
				>
					{running ? 'Screening…' : 'Run screener'}
				</button>
				<span class="font-mono text-[10px] text-slate-600">
					{activeFilters.length} filter{activeFilters.length === 1 ? '' : 's'} active
				</span>
			</div>
		</section>

		<section class="rounded-xl border border-surface-700 bg-surface-900">
			<h3 class="border-b border-surface-700 px-4 py-2.5 text-sm font-semibold text-slate-200">
				Results
			</h3>
			{#if results.length}
				<table class="w-full border-collapse font-mono text-xs">
					<thead>
						<tr class="text-left text-[10px] tracking-wider text-slate-500 uppercase">
							<th class="px-4 py-2">Symbol</th>
							<th class="px-4 py-2 text-right">Last</th>
							<th class="px-4 py-2 text-right">Volume</th>
							<th class="px-4 py-2 text-right">Avg $Vol 20d</th>
							<th class="px-4 py-2 text-right">Change</th>
							<th class="px-4 py-2">Strategy</th>
						</tr>
					</thead>
					<tbody>
						{#each results as row (row.symbol)}
							<tr class="border-t border-surface-700" data-testid="screener-row" data-symbol={row.symbol}>
								<td class="px-4 py-1.5 font-semibold text-slate-200">{row.symbol}</td>
								<td class="px-4 py-1.5 text-right text-slate-300">{row.last.toFixed(2)}</td>
								<td class="px-4 py-1.5 text-right text-slate-400">{row.volume.toLocaleString('en-US')}</td>
								<td class="px-4 py-1.5 text-right text-slate-400">
									${(row.avg_dollar_volume_20d / 1e9).toFixed(1)}B
								</td>
								<td class="px-4 py-1.5 text-right {row.change_pct >= 0 ? 'text-profit' : 'text-loss'}">
									{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(2)}%
								</td>
								<td class="px-4 py-1.5">
									{#each matchingStrategies(row.symbol) as strategy (strategy.id)}
										<a
											href="/w/{workspaceId}/strategies/{strategy.id}"
											class="text-accent hover:underline"
										>
											{strategy.name}
										</a>
									{:else}
										<span class="text-slate-600">—</span>
									{/each}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{:else}
				<p class="px-4 py-8 text-center text-xs text-slate-600">
					Run the screener to populate results.
				</p>
			{/if}
		</section>
	</div>

	<aside class="space-y-4">
		<section
			class="rounded-xl border border-surface-700 bg-surface-900 p-4"
			data-testid="screener-save"
		>
			<h3 class="mb-2 text-sm font-semibold text-slate-200">Save as workspace artifact</h3>
			<input
				type="text"
				class="w-full rounded-lg border border-surface-600/60 bg-surface-950 px-3 py-2 text-sm text-slate-200 outline-none placeholder:text-slate-600 focus:border-accent/50"
				placeholder="Screener name…"
				bind:value={saveName}
				data-testid="screener-name-input"
			/>
			<ul class="mt-2 space-y-0.5 font-mono text-[10px] text-slate-500">
				{#each activeFilters as filter (filter.kind + filter.value)}
					<li>
						{filter.kind} {filter.op} {filter.value.toLocaleString('en-US')}
					</li>
				{/each}
			</ul>
			<button
				type="button"
				class="mt-3 w-full rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25 disabled:opacity-40"
				disabled={!saveName.trim() || !activeFilters.length || saving}
				onclick={save}
				data-testid="screener-save-button"
			>
				{saving ? 'Saving…' : 'Save screener'}
			</button>
		</section>

		<section class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h3 class="mb-2 text-sm font-semibold text-slate-200">
				Saved screeners &amp; studies <span class="font-mono text-[10px] text-slate-500">({saved.length})</span>
			</h3>
			<ul class="space-y-1.5" data-testid="saved-screeners">
				{#each saved as screener (screener.id)}
					<li class="rounded-lg border border-surface-700 bg-surface-900 px-2.5 py-1.5">
						<div class="flex items-baseline gap-2">
							<span class="truncate text-xs font-medium text-slate-200">{screener.name}</span>
							<span class="ml-auto font-mono text-[10px] text-slate-600">
								{relativeTime(screener.created_at)}
							</span>
						</div>
						<p class="truncate font-mono text-[10px] text-slate-600">{screener.artifact_path}</p>
					</li>
				{:else}
					<li class="text-[11px] text-slate-600">Nothing saved yet.</li>
				{/each}
			</ul>
		</section>
	</aside>
</div>
