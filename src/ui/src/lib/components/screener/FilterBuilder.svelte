<script lang="ts">
	/**
	 * Filter builder: one row per condition with a grouped kind selector and
	 * per-kind parameter editors driven by the filter catalog. Every edit is
	 * emitted as a fresh row list so the panel can regenerate the screening
	 * source deterministically; row validation mirrors the declaration-time
	 * checks that guard the generated Rust fragment.
	 */
	import {
		DEFAULT_KIND,
		FILTER_GROUPS,
		FILTER_KINDS,
		defaultParams,
		kindSpec,
		newRow,
		validateRow
	} from './filterCatalog';
	import type { FilterRow } from './filterCatalog';

	let {
		rows,
		onChange,
		metricOptions = []
	}: {
		rows: FilterRow[];
		onChange: (rows: FilterRow[]) => void;
		metricOptions?: string[];
	} = $props();

	function replace(next: FilterRow[]): void {
		onChange(next);
	}

	function changeKind(row: FilterRow, kind: string): void {
		replace(
			rows.map((candidate) =>
				candidate.uid === row.uid ? { uid: candidate.uid, kind, params: defaultParams(kind) } : candidate
			)
		);
	}

	function updateParam(row: FilterRow, name: string, value: string): void {
		replace(
			rows.map((candidate) =>
				candidate.uid === row.uid
					? { ...candidate, params: { ...candidate.params, [name]: value } }
					: candidate
			)
		);
	}

	function addRow(): void {
		replace([...rows, newRow(DEFAULT_KIND)]);
	}

	function removeRow(uid: number): void {
		replace(rows.filter((row) => row.uid !== uid));
	}
</script>

<section class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="filter-builder">
	<div class="flex flex-wrap items-center gap-2">
		<h3 class="text-sm font-semibold text-slate-800">Filters</h3>
		<span class="font-mono text-[10px] text-slate-500">
			{rows.length} row{rows.length === 1 ? '' : 's'} · generated source reads the SDK context only
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
				class="rounded-lg border bg-surface-950 px-3 py-2 {error ? 'border-loss/50' : 'border-surface-700'}"
				data-testid="filter-row"
				data-kind={row.kind}
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
								{#each FILTER_KINDS.filter((entry) => entry.group === group.id) as entry (entry.kind)}
									<option value={entry.kind}>{entry.label}</option>
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
									value={row.params[field.name] ?? field.def}
									onchange={(event) =>
										updateParam(row, field.name, (event.target as HTMLSelectElement).value)}
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
									value={row.params[field.name] ?? ''}
									placeholder={field.optional ? 'optional' : ''}
									oninput={(event) =>
										updateParam(row, field.name, (event.target as HTMLInputElement).value)}
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
			<p class="text-[11px] text-slate-500">
				No filters — the program records every symbol's last close.
			</p>
		{/each}
	</div>

	{#if metricOptions.length}
		<p class="mt-3 border-t border-surface-700 pt-2 font-mono text-[10px] text-slate-500" data-testid="filter-metrics">
			metrics recorded: {metricOptions.join(', ')}
		</p>
	{/if}
</section>
