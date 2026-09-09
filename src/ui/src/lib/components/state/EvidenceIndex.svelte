<script lang="ts">
	import EvidenceBadge from '$lib/components/chat/EvidenceBadge.svelte';
	import type { EvidenceItem } from '$lib/types';

	/** Evidence index: content-addressed references with trust tiers. */
	let { evidence }: { evidence: EvidenceItem[] } = $props();
</script>

<div class="rounded-xl border border-white/5 bg-surface-850/60 p-4" data-testid="evidence-index">
	<h3 class="mb-3 text-sm font-semibold text-slate-200">
		Evidence index <span class="font-mono text-[10px] text-slate-500">({evidence.length})</span>
	</h3>
	<ul class="space-y-1.5">
		{#each evidence as item (item.id)}
			<li class="flex flex-wrap items-center gap-2 rounded-lg border border-white/5 bg-surface-900/60 px-3 py-2">
				<EvidenceBadge evidence={item} />
				<span class="font-mono text-[10px] text-slate-600">{item.reference}</span>
				{#if item.digest}
					<span class="ml-auto truncate font-mono text-[10px] text-slate-600" title={item.digest}>
						sha256:{item.digest.slice(0, 16)}…
					</span>
				{/if}
			</li>
		{:else}
			<li class="text-xs text-slate-600">No evidence recorded yet.</li>
		{/each}
	</ul>
</div>
