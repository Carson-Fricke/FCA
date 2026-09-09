<script lang="ts">
	import { searchKnowledge } from '$lib/api';
	import type { KnowledgeHit } from '$lib/types';

	/** Knowledge-search affordance: global corpus with path/revision citations. */
	let {
		onInject
	}: {
		onInject: (hit: KnowledgeHit) => void;
	} = $props();

	let query = $state('');
	let results = $state<KnowledgeHit[]>([]);
	let searching = $state(false);
	let searched = $state(false);

	async function run(): Promise<void> {
		if (!query.trim()) return;
		searching = true;
		try {
			results = await searchKnowledge(query.trim());
			searched = true;
		} finally {
			searching = false;
		}
	}

	function inject(hit: KnowledgeHit): void {
		onInject(hit);
		query = '';
		results = [];
		searched = false;
	}
</script>

<div class="rounded-lg border border-white/5 bg-surface-900/60 p-2" data-testid="knowledge-search">
	<div class="flex gap-2">
		<input
			type="search"
			class="min-w-0 flex-1 rounded-md border border-surface-600/60 bg-surface-950/60 px-2 py-1.5 text-xs text-slate-200 outline-none placeholder:text-slate-600 focus:border-accent/50"
			placeholder="Search knowledge corpus…"
			bind:value={query}
			onkeydown={(e) => e.key === 'Enter' && run()}
		/>
		<button
			type="button"
			class="rounded-md border border-accent/40 bg-accent/15 px-2.5 py-1.5 text-xs font-medium text-accent hover:bg-accent/25"
			onclick={run}
			disabled={searching}
		>
			{searching ? '…' : 'Search'}
		</button>
	</div>
	{#if searched && results.length === 0}
		<p class="mt-2 text-xs text-slate-500">No knowledge hits.</p>
	{/if}
	{#if results.length}
		<ul class="mt-2 space-y-1.5">
			{#each results as hit (hit.path)}
				<li class="rounded-md border border-white/5 bg-surface-850/60 p-2">
					<div class="flex items-baseline gap-2">
						<span class="truncate text-xs font-medium text-slate-200">{hit.title}</span>
						<span
							class="ml-auto shrink-0 rounded-full border px-1.5 font-mono text-[9px]
								{hit.trust_tier === 'workspace' ? 'border-accent/40 text-accent' : 'border-surface-600 text-slate-400'}"
						>
							{hit.trust_tier}
						</span>
					</div>
					<p class="mt-1 line-clamp-2 text-[11px] text-slate-500">{hit.excerpt}</p>
					<div class="mt-1 flex items-center gap-2">
						<span class="truncate font-mono text-[10px] text-slate-600"
							>{hit.path}@{hit.revision}</span
						>
						<button
							type="button"
							class="ml-auto shrink-0 rounded border border-accent/30 px-1.5 py-0.5 text-[10px] text-accent hover:bg-accent/15"
							onclick={() => inject(hit)}
						>
							Use as context
						</button>
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</div>
