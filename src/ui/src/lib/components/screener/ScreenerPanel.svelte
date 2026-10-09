<script lang="ts">
	/**
	 * Screener page composition: one authored definition (name, as-of date,
	 * lookback, bar interval, rank metric, filter builder, Rust editor) saved
	 * as a content-addressed workspace artifact, run against the as-of date
	 * through the Thesis screener route, and rendered with full provenance.
	 *
	 * There is no server-side filter interpreter: the builder generates Rust
	 * source, the harness JIT compiles it, and the definition hash binds the
	 * saved artifact to those exact source bytes.
	 */
	import { runScreener, saveScreener } from '$lib/api';
	import { notify } from '$lib/workspace.svelte';
	import FilterBuilder from './FilterBuilder.svelte';
	import RustScreenerEditor from './RustScreenerEditor.svelte';
	import SavedStudies from './SavedStudies.svelte';
	import ScreenerResultsTable from './ScreenerResultsTable.svelte';
	import {
		BAR_INTERVALS,
		DEFAULT_KIND,
		describeScreenerError,
		metricsForRows,
		newRow,
		validateRow
	} from './filterCatalog';
	import type { FilterRow } from './filterCatalog';
	import { generateScreenerSource, lintScreenerSource, parseFilterRows } from './screenerSource';
	import type { ScreenerBarInterval, ScreenerDefinition, ScreeningResult } from '$lib/types';

	let {
		workspaceId,
		initialSaved = [],
		initialResult = null
	}: {
		workspaceId: string;
		initialSaved?: ScreenerDefinition[];
		initialResult?: ScreeningResult | null;
	} = $props();

	const initialRows: FilterRow[] = [newRow(DEFAULT_KIND)];
	let rows = $state<FilterRow[]>(initialRows);
	let source = $state(generateScreenerSource(initialRows));
	let sourceDirty = $state(false);
	let name = $state('');
	let asOf = $state(new Date().toISOString().slice(0, 10));
	let lookbackDays = $state(20);
	let barIntervals = $state<ScreenerBarInterval[]>(['1d']);
	let rankMetric = $state('close');
	let saved = $state<ScreenerDefinition[]>([]);
	let selectedId = $state<string | null>(null);
	let result = $state<ScreeningResult | null>(null);
	let running = $state(false);
	let saving = $state(false);
	let serverError = $state<string | null>(null);

	// The page resolves the saved list asynchronously; seed once the initial
	// documents arrive and the local list is still untouched.
	$effect(() => {
		if (initialSaved.length && saved.length === 0) {
			saved = initialSaved;
			if (!selectedId) selectedId = initialSaved[0]?.definitionId ?? null;
		}
	});

	$effect(() => {
		if (initialResult && !result) result = initialResult;
	});

	const sourceIssues = $derived(lintScreenerSource(source));
	const sourceValid = $derived(!sourceIssues.some((issue) => issue.severity === 'error'));
	const rowsValid = $derived(rows.every((row) => validateRow(row) === null));
	const nameValid = $derived(name.trim().length > 0 && name.trim().length <= 256);
	const lookbackValid = $derived(
		Number.isInteger(lookbackDays) && lookbackDays >= 1 && lookbackDays <= 3650
	);
	const asOfValid = $derived(/^\d{4}-\d{2}-\d{2}$/.test(asOf));
	const rankValid = $derived(rankMetric.trim().length > 0);
	const draftValid = $derived(sourceValid && rowsValid && nameValid && lookbackValid && rankValid && barIntervals.length > 0);
	const canSave = $derived(draftValid && !saving && !running);
	const canRun = $derived(draftValid && asOfValid && !saving && !running);

	const metricOptions = $derived.by(() => {
		const options = ['close', ...metricsForRows(rows)];
		if (rankMetric && !options.includes(rankMetric)) options.push(rankMetric);
		return [...new Set(options)];
	});

	function onRowsChange(next: FilterRow[]): void {
		rows = next;
		// Filter edits regenerate the source while it is still generated;
		// manual source edits are never clobbered by the builder.
		if (!sourceDirty) source = generateScreenerSource(next, barIntervals[0]);
	}

	function regenerate(): void {
		source = generateScreenerSource(rows, barIntervals[0]);
		sourceDirty = false;
	}

	function onSourceEdited(): void {
		sourceDirty = true;
	}

	function loadDefinition(definition: ScreenerDefinition): void {
		name = definition.name;
		lookbackDays = definition.lookbackDays;
		barIntervals = definition.barIntervals;
		rankMetric = definition.rankMetric;
		source = definition.source;
		selectedId = definition.definitionId;
		serverError = null;
		const parsed = parseFilterRows(definition.source);
		if (parsed) {
			rows = parsed;
			sourceDirty = false;
		} else {
			rows = [];
			sourceDirty = true;
		}
	}

	function draftDiffers(definition: ScreenerDefinition): boolean {
		return (
			definition.name !== name.trim() ||
			definition.lookbackDays !== lookbackDays ||
			JSON.stringify(definition.barIntervals) !== JSON.stringify(barIntervals) ||
			definition.rankMetric !== rankMetric.trim() ||
			definition.source !== source
		);
	}

	async function saveDraft(announce = true): Promise<ScreenerDefinition | null> {
		if (!draftValid || saving) return null;
		saving = true;
		serverError = null;
		try {
			const created = await saveScreener(workspaceId, {
				name: name.trim(),
				lookback_days: lookbackDays,
				bar_intervals: barIntervals,
				rank_metric: rankMetric.trim(),
				source
			});
			saved = [created, ...saved.filter((item) => item.definitionId !== created.definitionId)];
			selectedId = created.definitionId;
			if (announce) notify(`Screener saved · ${created.definitionId}`);
			return created;
		} catch (error) {
			serverError = describeScreenerError(error);
			return null;
		} finally {
			saving = false;
		}
	}

	async function save(): Promise<void> {
		await saveDraft(true);
	}

	async function runDefinition(definition: ScreenerDefinition): Promise<void> {
		if (running || saving) return;
		running = true;
		serverError = null;
		selectedId = definition.definitionId;
		try {
			result = await runScreener(workspaceId, {
				definition_id: definition.definitionId,
				as_of: asOf
			});
			notify(`Screener job ${result.job_id} · ${result.status}`);
		} catch (error) {
			serverError = describeScreenerError(error);
		} finally {
			running = false;
		}
	}

	async function run(): Promise<void> {
		if (!canRun) return;
		const selected = saved.find((item) => item.definitionId === selectedId) ?? null;
		// A definition is immutable content: a dirty draft is saved first so
		// the run always compiles exactly the bytes the operator sees.
		const definition =
			selected && !draftDiffers(selected) ? selected : await saveDraft(false);
		if (!definition) return;
		await runDefinition(definition);
	}
</script>

<svelte:head><title>Screener — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-4" data-testid="screener-panel">
	<header class="flex flex-wrap items-center gap-3">
		<div>
			<h1 class="text-xl font-semibold text-slate-800">Screener</h1>
			<p class="text-sm text-slate-500">
				Rust-authored point-in-time screens: the builder generates source, the harness compiles
				and ranks it, and the definition hash binds every result to its bytes.
			</p>
		</div>
	</header>

	<section class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="screener-definition">
		<div class="grid gap-3 md:grid-cols-2 lg:grid-cols-5">
			<label class="flex flex-col gap-1 text-[10px] text-slate-500 lg:col-span-2">
				Definition name
				<input
					type="text"
					class="rounded-lg border border-surface-600 bg-surface-950 px-3 py-1.5 text-sm text-slate-800 outline-none placeholder:text-slate-500 focus:border-accent/50"
					placeholder="e.g. Liquid large caps"
					bind:value={name}
					data-testid="screener-name-input"
				/>
			</label>
			<label class="flex flex-col gap-1 text-[10px] text-slate-500">
				As-of date
				<input
					type="date"
					class="rounded-lg border border-surface-600 bg-surface-950 px-2 py-1.5 font-mono text-xs text-slate-700 outline-none"
					bind:value={asOf}
					data-testid="screener-asof"
				/>
			</label>
			<label class="flex flex-col gap-1 text-[10px] text-slate-500">
				Lookback days
				<input
					type="number"
					min="1"
					max="3650"
					class="rounded-lg border border-surface-600 bg-surface-950 px-2 py-1.5 font-mono text-xs text-slate-700 outline-none"
					bind:value={lookbackDays}
					data-testid="screener-lookback"
				/>
			</label>
			<label class="flex flex-col gap-1 text-[10px] text-slate-500">
				Bar intervals (select several)
				<select
					class="rounded-lg border border-surface-600 bg-surface-950 px-2 py-1.5 font-mono text-xs text-slate-700 outline-none"
					multiple
					bind:value={barIntervals}
					onchange={() => { if (!sourceDirty) source = generateScreenerSource(rows, barIntervals[0]); }}
					data-testid="screener-interval"
				>
					{#each BAR_INTERVALS as interval (interval)}
						<option value={interval}>{interval}</option>
					{/each}
				</select>
			</label>
			<label class="flex flex-col gap-1 text-[10px] text-slate-500 md:col-span-2 lg:col-span-2">
				Rank metric
				<select
					class="rounded-lg border border-surface-600 bg-surface-950 px-2 py-1.5 font-mono text-xs text-slate-700 outline-none"
					bind:value={rankMetric}
					data-testid="screener-rank-metric"
				>
					{#each metricOptions as metric (metric)}
						<option value={metric}>{metric}</option>
					{/each}
				</select>
			</label>
		</div>

		<div class="mt-3 flex flex-wrap items-center gap-3 border-t border-surface-700 pt-3">
			<button
				type="button"
				class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
				disabled={!canSave}
				onclick={save}
				data-testid="screener-save-button"
			>
				{saving ? 'Saving…' : 'Save definition'}
			</button>
			<button
				type="button"
				class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
				disabled={!canRun}
				onclick={run}
				data-testid="screener-run"
			>
				{running ? 'Screening…' : 'Run screener'}
			</button>
			<span class="font-mono text-[10px] text-slate-500">
				{sourceIssues.length
					? `${sourceIssues.length} pre-flight issue${sourceIssues.length === 1 ? '' : 's'}`
					: 'pre-flight clean'} · point-in-time run: no lookahead past the as-of date
			</span>
			<span class="ml-auto font-mono text-[10px] text-slate-500" title={metricsForRows(rows).join(', ')}>
				{selectedId ? `selected ${selectedId.slice(0, 19)}…` : 'unsaved draft'}
			</span>
		</div>
		{#if serverError}
			<p
				class="mt-2 rounded-lg border border-loss/40 bg-loss/10 px-2 py-1.5 font-mono text-[10px] text-loss"
				data-testid="screener-error"
			>
				{serverError}
			</p>
		{/if}
	</section>

	<div class="grid gap-4 lg:grid-cols-[1fr_320px]">
		<div class="space-y-4">
			<FilterBuilder {rows} onChange={onRowsChange} metricOptions={metricsForRows(rows)} />
			<RustScreenerEditor
				bind:source
				{sourceDirty}
				{serverError}
				onEdited={onSourceEdited}
				onGenerate={regenerate}
			/>
			<ScreenerResultsTable
				{result}
				{running}
				error={serverError}
				definitionName={saved.find((item) => item.definitionId === selectedId)?.name ?? null}
			/>
		</div>

		<aside class="space-y-4">
			<SavedStudies {saved} {selectedId} onLoad={loadDefinition} onRun={runDefinition} />
		</aside>
	</div>
</div>
