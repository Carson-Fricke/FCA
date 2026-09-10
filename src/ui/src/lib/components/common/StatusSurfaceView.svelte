<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { StatusSurface } from '$lib/types';

	/**
	 * Renders one harness status surface: the mapped record when available,
	 * or the explicit absence — never a fabricated substitute.
	 */
	let {
		surface,
		label = 'Status',
		children
	}: {
		surface: StatusSurface<unknown>;
		label?: string;
		children?: Snippet<[unknown]>;
	} = $props();
</script>

{#if surface.state === 'unavailable' || surface.value === null}
	<div
		class="rounded-lg border border-dashed border-surface-600/60 bg-surface-950 px-4 py-3 text-xs"
		data-testid="surface-unavailable"
	>
		<div class="flex items-center gap-2 text-slate-400">
			<span class="h-1.5 w-1.5 rounded-full bg-warn/80" aria-hidden="true"></span>
			<span class="font-semibold uppercase tracking-wider">{label} unavailable</span>
			{#if surface.reason}
				<span class="font-mono text-[11px] text-slate-500">{surface.reason}</span>
			{/if}
		</div>
		<p class="mt-1 text-slate-500">
			This surface has not been published. No substitute values are shown.
		</p>
	</div>
{:else if children}
	{@render children(surface.value)}
{/if}
