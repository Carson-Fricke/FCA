<script lang="ts">
	import { canUseRealApi, fixtureMode, setFixtureMode } from '$lib/api';

	/** Shows the active data mode and offers the runtime escape hatch. */
</script>

<span class="inline-flex items-center gap-2 rounded-lg border border-surface-600 bg-surface-900 px-2.5 py-1 font-mono text-[11px] text-slate-400">
	<span
		class="h-1.5 w-1.5 rounded-full {fixtureMode ? 'bg-amber-400' : 'bg-emerald-400'}"
		aria-hidden="true"
	></span>
	{fixtureMode ? 'fixture data' : 'thesis api'}
	{#if fixtureMode && canUseRealApi}
		<button
			type="button"
			class="ml-1 rounded border border-surface-600 px-1.5 py-0.5 text-[10px] text-slate-400 hover:border-accent/40 hover:text-accent"
			onclick={() => setFixtureMode(false)}
			title="Load data from the Thesis API"
			data-testid="mode-use-api"
		>
			use api
		</button>
	{:else if !fixtureMode}
		<button
			type="button"
			class="ml-1 rounded border border-surface-600 px-1.5 py-0.5 text-[10px] text-slate-400 hover:border-accent/40 hover:text-accent"
			onclick={() => setFixtureMode(true)}
			title="Render from bundled fixtures when the API is unreachable"
			data-testid="mode-use-fixtures"
		>
			use fixtures
		</button>
	{/if}
</span>
