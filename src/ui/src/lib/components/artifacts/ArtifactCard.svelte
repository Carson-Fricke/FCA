<script lang="ts">
	import ArtifactKindBadge from './ArtifactKindBadge.svelte';
	import { shortHash } from '$lib/format';
	import type { WorkspaceArtifact } from '$lib/types';

	/** One gallery card: identity, declared ingestion, and provenance. */
	let {
		artifact,
		active = false,
		onSelect
	}: { artifact: WorkspaceArtifact; active?: boolean; onSelect: () => void } = $props();
</script>

<button
	type="button"
	onclick={onSelect}
	class="group flex w-full flex-col rounded-xl border bg-surface-900 p-4 text-left transition hover:border-accent/40 hover:shadow-sm {active
		? 'border-accent/50'
		: 'border-surface-700'}"
	data-testid="artifact-card"
>
	<div class="flex w-full items-center gap-2">
		<ArtifactKindBadge kind={artifact.kind} />
		<span class="ml-auto font-mono text-[10px] text-slate-500">{artifact.version}</span>
	</div>
	<h2 class="mt-2.5 truncate text-sm font-semibold text-slate-800">{artifact.title}</h2>
	<p class="truncate font-mono text-[11px] text-slate-500">{artifact.subtitle}</p>
	<p class="mt-2 line-clamp-2 min-h-8 text-xs text-slate-500">
		{artifact.description || artifact.artifact_path}
	</p>
	<div class="mt-3 flex flex-wrap gap-1.5">
		{#each artifact.inputs as input}
			<span class="rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500">
				{input.data_type} {input.symbols.join('/')}{input.interval ? ` · ${input.interval}` : ''}
			</span>
		{:else}
			<span class="text-[10px] text-slate-500">No declared inputs</span>
		{/each}
	</div>
	<div class="mt-3 flex w-full items-center gap-3 border-t border-surface-700 pt-2 font-mono text-[10px] text-slate-500">
		<span>{artifact.file_count} files</span>
		{#if artifact.history.length > 1}
			<span>{artifact.history.length} versions</span>
		{/if}
		<span class="ml-auto">{shortHash(artifact.digests.bundle_sha256)}</span>
	</div>
</button>
