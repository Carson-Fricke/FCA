<script lang="ts">
	import type { ToolRowHeader } from './headers';
	let { icon }: { icon: ToolRowHeader['icon'] } = $props();
</script>

{#if icon === 'dot'}
	<span class="marker-dot" aria-hidden="true" data-activity-icon="dot"></span>
{:else}
	<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" data-activity-icon={icon}>
		{#if icon === 'book'}
			<path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Z"/><path d="M12 5v15"/>
		{:else if icon === 'search'}
			<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>
		{:else}
			<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>
		{/if}
	</svg>
{/if}

<style>
	.marker-dot{display:block;width:7px;height:7px;border-radius:50%;background:currentColor}
</style>
