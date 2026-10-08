<script lang="ts">
	import IngestChips from './IngestChips.svelte';
	import { shortDate } from '$lib/format';
	import type { ArtifactInput, StudyArtifactPreview } from '$lib/types';

	/** Study hero: replay window, data diet, indicator dependencies, metrics. */
	let {
		preview,
		inputs,
		onOpenArtifact
	}: {
		preview: StudyArtifactPreview;
		inputs: ArtifactInput[];
		onOpenArtifact?: (artifactId: string) => void;
	} = $props();
</script>

<section class="space-y-4" data-testid="study-preview">
	<div class="grid gap-4 md:grid-cols-[1fr_2fr]">
		<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
				Replay window
			</h4>
			<p class="mt-2 font-mono text-lg text-slate-800">
				{preview.window.start ? shortDate(preview.window.start) : '—'}
				<span class="text-slate-500">→</span>
				{preview.window.end ? shortDate(preview.window.end) : '—'}
			</p>
			<p class="mt-1 text-xs text-slate-500">
				Deterministic no-execution replay; the study observes and publishes metrics.
			</p>
		</div>
		<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Data diet</h4>
			<div class="mt-2">
				<IngestChips {inputs} />
			</div>
			<p class="mt-2 text-xs text-slate-500">
				Only these subscriptions are acquired for the replay; missing bars surface as gaps in the
				recorded samples.
			</p>
		</div>
	</div>

	<div class="grid gap-4 md:grid-cols-2">
		<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
				Indicator dependencies
			</h4>
			{#if preview.indicator_ids.length}
				<div class="mt-2 flex flex-wrap gap-1.5">
					{#each preview.indicator_ids as indicatorId}
						<button
							type="button"
							onclick={() => onOpenArtifact?.(`indicator:${indicatorId}`)}
							class="rounded-full border border-profit/30 bg-profit/5 px-2 py-0.5 font-mono text-[10px] text-profit transition hover:bg-profit/10"
						>
							∿ {indicatorId}
						</button>
					{/each}
				</div>
			{:else}
				<p class="mt-2 text-xs text-slate-500">No indicator dependencies declared.</p>
			{/if}
		</div>
		<div class="rounded-xl border border-surface-700 bg-surface-900 p-4">
			<h4 class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
				Published metrics
			</h4>
			{#if preview.metrics.length}
				<ul class="mt-2 space-y-1">
					{#each preview.metrics as metric}
						<li class="flex items-center justify-between font-mono text-xs text-slate-700">
							<span>{metric.name}</span><span>{metric.value}</span>
						</li>
					{/each}
				</ul>
			{:else}
				<div class="mt-2 rounded-lg border border-dashed border-surface-600 bg-surface-950 px-3 py-4 text-center">
					<p class="text-xs text-slate-500">
						No published metrics recorded yet. A completed study run publishes its custom metrics
						here.
					</p>
				</div>
			{/if}
		</div>
	</div>
</section>
