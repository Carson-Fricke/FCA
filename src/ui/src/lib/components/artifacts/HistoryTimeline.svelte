<script lang="ts">
	import { shortDate } from '$lib/format';
	import type { ArtifactHistoryEntry } from '$lib/types';

	/** Version/revision timeline for one artifact, newest last. */
	let { history }: { history: ArtifactHistoryEntry[] } = $props();
</script>

<div class="rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="artifact-history">
	<h3 class="text-sm font-semibold text-slate-800">History</h3>
	{#if history.length}
		<ol class="mt-3 flex flex-wrap items-start gap-x-4 gap-y-3">
			{#each history as entry, index (entry.label + index)}
				<li class="flex items-center gap-2">
					<span
						class="h-2 w-2 shrink-0 rounded-full {index === history.length - 1
							? 'bg-accent'
							: 'bg-surface-600'}"
					></span>
					<div>
						<p class="font-mono text-[11px] text-slate-700">{entry.label}</p>
						<p class="font-mono text-[10px] text-slate-500">
							{entry.created_at ? shortDate(entry.created_at) : '—'}{entry.note
								? ` · ${entry.note}`
								: ''}
						</p>
					</div>
					{#if index < history.length - 1}
						<span class="text-surface-600" aria-hidden="true">—</span>
					{/if}
				</li>
			{/each}
		</ol>
	{:else}
		<p class="mt-2 text-xs text-slate-500">No recorded history for this artifact.</p>
	{/if}
</div>
