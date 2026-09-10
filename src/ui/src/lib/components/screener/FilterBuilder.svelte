<script lang="ts">
	/**
	 * Declarative filter builder: one row per filter with a grouped kind
	 * selector, per-kind parameter editors driven by the filter catalog,
	 * client-side validation feedback, and the run control (full filter set
	 * + as-of date, defaulting to today).
	 */
	import {
		FILTER_GROUPS,
		FILTER_KINDS,
		DEFAULT_KIND,
		defaultParams,
		kindSpec,
		newRow,
		validateRow
	} from './filterCatalog';
	import type { FilterRow } from './filterCatalog';

	let {
		rows = $bindable(),
		asOf = $bindable(),
		running = false,
		canRun = false,
		onRun,
		serverError = null
	}: {
		rows: FilterRow[];
		asOf: string;
		running?: boolean;
		canRun?: boolean;
		onRun: () => void;
		serverError?: string | null;
	} = $props();

	function changeKind(row: FilterRow, kind: string): void {
		row.kind = kind;
		row.params = defaultParams(kind);
	}

	function addRow(): void {
		rows = [...rows, newRow(DEFAULT_KIND)];
	}

	function removeRow(uid: number): void {
		rows = rows.filter((row) => row.uid !== uid);
	}
</script>

<section class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="filter-builder">
	<div class="flex flex-wrap items-center gap-2">
		<h3 class="text-sm font-semibold text-slate-800">Filters</h3>
		<span class="font-mono text-[10px] text-slate-500">
			{rows.length} filter{rows.length === 1 ? '' : 's'} declared
		</span>
		<button
			type="button"
			class="ml-auto rounded-lg border border-surface-600 px-2.5 py-1 text-xs text-slate-500 hover:border-white/20 hover:text-slate-800"
			onclick={addRow}
			data-testid="add-filter"
		>
			＋ Add filter
		</button>
	</div>

	<div class="mt-3 space-y-2">
		{#each rows as row (row.uid)}
			{@const spec = kindSpec(row.kind)}
			{@const error = validateRow(row)}
			<div
				class="rounded-lg border bg-surface-950 px-3 py-2 {error
					? 'border-loss/50'
					: 'border-surface-700'}"
				data-testid="filter-row"
			>
				<div class="flex flex-wrap items-center gap-2">
					<select
						class="rounded border border-surface-600 bg-surface-900 px-1.5 py-1 text-xs text-slate-700 outline-none"
						value={row.kind}
						onchange={(event) => changeKind(row, (event.target as HTMLSelectElement).value)}
						data-testid="filter-kind-select"
						aria-label="Filter kind"
					>
						{#if !spec}
							<option value={row.kind}>{row.kind} (unknown)</option>
						{/if}
						{#each FILTER_GROUPS as group (group.id)}
							<optgroup label={group.label}>
								{#each FILTER_KINDS.filter((k) => k.group === group.id) as kindSpecEntry (kindSpecEntry.kind)}
									<option value={kindSpecEntry.kind}>{kindSpecEntry.label}</option>
								{/each}
							</optgroup>
						{/each}
					</select>
					<button
						type="button"
						class="ml-auto rounded px-1.5 py-0.5 text-[10px] text-slate-500 hover:text-loss"
						onclick={() => removeRow(row.uid)}
						data-testid="remove-filter"
					>
						Remove
					</button>
				</div>
				<div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
					{#each spec?.fields ?? [] as field (field.name)}
						<label class="flex items-center gap-1 text-[10px] text-slate-500">
							{field.label}
							{#if field.type === 'choice'}
								<select
									class="rounded border border-surface-600 bg-surface-900 px-1.5 py-1 text-xs text-slate-700 outline-none"
									bind:value={row.params[field.name]}
									data-testid="filter-param"
									data-param={field.name}
								>
									{#each field.choices ?? [] as choice (choice)}
										<option value={choice}>{choice}</option>
									{/each}
								</select>
							{:else}
								<input
									type={field.type === 'text' ? 'text' : 'number'}
									class="w-28 rounded border border-surface-600 bg-surface-900 px-1.5 py-1 font-mono text-xs text-slate-700 outline-none"
									bind:value={row.params[field.name]}
									placeholder={field.optional ? 'optional' : ''}
									data-testid="filter-param"
									data-param={field.name}
								/>
							{/if}
						</label>
					{/each}
				</div>
				{#if error}
					<p class="mt-1.5 font-mono text-[10px] text-loss" data-testid="filter-row-error">{error}</p>
				{/if}
			</div>
		{:else}
			<p class="text-[11px] text-slate-500">No filters — add one to screen the universe.</p>
		{/each}
	</div>

	<div class="mt-3 flex flex-wrap items-center gap-3 border-t border-surface-700 pt-3">
		<button
			type="button"
			class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25 disabled:opacity-40"
			onclick={onRun}
			disabled={!canRun}
			data-testid="screener-run"
		>
			{running ? 'Screening…' : 'Run screener'}
		</button>
		<label class="flex items-center gap-1.5 text-[11px] text-slate-500">
			As of
			<input
				type="date"
				class="rounded border border-surface-600 bg-surface-950 px-2 py-1 font-mono text-xs text-slate-700 outline-none"
				bind:value={asOf}
				data-testid="screener-asof"
			/>
		</label>
		<span class="font-mono text-[10px] text-slate-500">point-in-time run: no lookahead past the as-of date</span>
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
