<script lang="ts">
	import ArtifactKindBadge from './ArtifactKindBadge.svelte';
	import IngestChips from './IngestChips.svelte';
	import type { ArtifactInput, IndicatorArtifactPreview } from '$lib/types';

	/** Indicator hero: current value state, declared ingestion, consumers. */
	let {
		preview,
		inputs,
		onOpenArtifact
	}: {
		preview: IndicatorArtifactPreview;
		inputs: ArtifactInput[];
		onOpenArtifact?: (artifactId: string) => void;
	} = $props();
</script>

<section class="space-y-4" data-testid="indicator-preview">
	<div class="grid gap-4 md:grid-cols-[1fr_2fr]">
		<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Value</h4>
			<p class="mt-2 font-mono text-3xl font-medium text-slate-700">—</p>
			<span class="mt-2 inline-block rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500">
				no recorded series
			</span>
			<p class="mt-2 text-xs text-slate-500">
				Indicators expose an in-memory value during a replay; no per-event series is persisted yet.
			</p>
		</div>
		<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Ingests</h4>
			<div class="mt-2">
				<IngestChips {inputs} />
			</div>
		</div>
	</div>

	<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
		<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
			Used by
		</h4>
		{#if preview.used_by.length}
			<ul class="mt-2 space-y-1.5">
				{#each preview.used_by as usage (usage.id)}
					<li>
						<button
							type="button"
							onclick={() => onOpenArtifact?.(usage.id)}
							class="flex w-full items-center gap-2 rounded-lg border border-surface-700 px-3 py-2 text-left transition hover:border-accent/40"
						>
							<ArtifactKindBadge kind={usage.kind} />
							<span class="text-xs text-slate-700">{usage.title}</span>
							<span class="ml-auto font-mono text-[10px] text-slate-500">{usage.id}</span>
						</button>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="mt-2 text-xs text-slate-500">
				No committed strategy or study declares this indicator yet.
			</p>
		{/if}
	</div>
</section>
