<script lang="ts">
	/**
	 * Screener: declarative filter builder (engine-native vocabulary with
	 * declaration-time validation), point-in-time runs with an as-of date,
	 * a provenance-backed results table, saved studies (load / re-run), and
	 * a custom Python screen editor with boundary feedback. Definitions
	 * persist as workspace artifacts through the screening HTTP surface.
	 */
	import { runScreener, saveScreener } from '$lib/api';
	import { notify } from '$lib/workspace.svelte';
	import FilterBuilder from './FilterBuilder.svelte';
	import PythonScreenerEditor from './PythonScreenerEditor.svelte';
	import SavedStudies from './SavedStudies.svelte';
	import ScreenerResultsTable from './ScreenerResultsTable.svelte';
	import {
		DEFAULT_KIND,
		buildFilters,
		describeScreenerError,
		newRow,
		validateRow,
		specToRow
	} from './filterCatalog';
	import type { FilterRow } from './filterCatalog';
	import type { ScreenerRunResult, ScreenerSavedDefinition } from '$lib/types';

	let {
		workspaceId,
		initialSaved = []
	}: {
		workspaceId: string;
		initialSaved?: ScreenerSavedDefinition[];
	} = $props();

	let rows = $state<FilterRow[]>([newRow(DEFAULT_KIND)]);
	let asOf = $state(new Date().toISOString().slice(0, 10));
	let saveName = $state('');
	let lookbackDays = $state<number | null>(20);
	let saved = $state<ScreenerSavedDefinition[]>([]);
	let result = $state<ScreenerRunResult | null>(null);
	let running = $state(false);
	let saving = $state(false);
	let serverError = $state<string | null>(null);

	// The page resolves the saved list asynchronously; seed once when the
	// initial documents arrive and the local list is still untouched.
	$effect(() => {
		if (initialSaved.length && !saved.length) saved = initialSaved;
	});

	const validFilters = $derived(buildFilters(rows));
	const rowsValid = $derived(rows.length > 0 && rows.every((row) => validateRow(row) === null));
	const lookbackValid = $derived(lookbackDays === null || (lookbackDays >= 1 && lookbackDays <= 365));
	const canRun = $derived(rowsValid && !running);
	const canSave = $derived(
		rowsValid && saveName.trim().length > 0 && lookbackValid && !saving
	);

	async function run(): Promise<void> {
		running = true;
		serverError = null;
		try {
			result = await runScreener(workspaceId, {
				name: saveName.trim() || 'Ad-hoc screen',
				as_of: asOf,
				lookback_days: lookbackDays ?? 20,
				filters: validFilters
			});
		} catch (error) {
			serverError = describeScreenerError(error);
		} finally {
			running = false;
		}
	}

	async function rerunStudy(study: ScreenerSavedDefinition): Promise<void> {
		running = true;
		serverError = null;
		try {
			result = await runScreener(workspaceId, {
				name: study.definition.name,
				as_of: asOf,
				lookback_days: study.definition.lookback_days,
				filters: study.definition.filters
			});
		} catch (error) {
			serverError = describeScreenerError(error);
		} finally {
			running = false;
		}
	}

	function loadStudy(study: ScreenerSavedDefinition): void {
		rows = study.definition.filters.map((filter) => specToRow(filter));
		saveName = study.definition.name;
		lookbackDays = study.definition.lookback_days;
		serverError = null;
	}

	async function save(): Promise<void> {
		if (!canSave) return;
		saving = true;
		serverError = null;
		try {
			const created = await saveScreener(workspaceId, {
				name: saveName.trim(),
				lookback_days: lookbackDays ?? 20,
				filters: validFilters
			});
			saved = [...saved, created];
			saveName = '';
			notify(`Screener saved as workspace artifact ${created.artifact_path}`);
		} catch (error) {
			serverError = describeScreenerError(error);
		} finally {
			saving = false;
		}
	}
</script>

<div class="grid gap-4 lg:grid-cols-[1fr_320px]" data-testid="screener-panel">
	<div class="space-y-4">
		<FilterBuilder bind:rows bind:asOf {running} {canRun} onRun={run} {serverError} />
		<ScreenerResultsTable {result} {running} />
	</div>

	<aside class="space-y-4">
		<section class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="screener-save">
			<h3 class="text-sm font-semibold text-slate-800">Save as workspace artifact</h3>
			<input
				type="text"
				class="mt-2 w-full rounded-lg border border-surface-600/60 bg-surface-950 px-3 py-2 text-sm text-slate-800 outline-none placeholder:text-slate-500 focus:border-accent/50"
				placeholder="Screener name…"
				bind:value={saveName}
				data-testid="screener-name-input"
			/>
			<label class="mt-2 flex items-center gap-2 text-[11px] text-slate-500">
				Lookback days
				<input
					type="number"
					min="1"
					max="365"
					class="w-16 rounded border border-surface-600 bg-surface-950 px-2 py-1 font-mono text-xs text-slate-700 outline-none"
					bind:value={lookbackDays}
					data-testid="screener-lookback"
				/>
			</label>
			<button
				type="button"
				class="mt-3 w-full rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25 disabled:opacity-40"
				disabled={!canSave}
				onclick={save}
				data-testid="screener-save-button"
			>
				{saving ? 'Saving…' : 'Save screener'}
			</button>
			<p class="mt-1.5 font-mono text-[10px] text-slate-500">
				{validFilters.length} validated filter{validFilters.length === 1 ? '' : 's'} in the payload
			</p>
		</section>

		<SavedStudies {saved} onLoad={loadStudy} onRerun={rerunStudy} />

		<PythonScreenerEditor {serverError} />
	</aside>
</div>
