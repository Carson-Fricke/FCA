<script lang="ts">
	import { page } from '$app/state';
	import DataModeBadge from '$lib/components/shell/DataModeBadge.svelte';
	import { fixtureMode, setBackend } from '$lib/api';
</script>

<div class="grid-bg flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
	<p class="font-mono text-sm text-loss">{page.status}</p>
	<h1 class="text-xl font-semibold text-slate-800">{page.error?.message ?? 'Something went wrong'}</h1>
	<a href="/w" class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-sm text-accent hover:bg-accent/25">
		Back to workspaces
	</a>
	<div class="mt-2 flex items-center gap-2">
		<DataModeBadge />
		{#if !fixtureMode}
			<button
				type="button"
				class="rounded-lg border border-surface-600 px-3 py-1.5 text-xs text-slate-300 hover:border-accent/40 hover:text-accent"
				onclick={() => setBackend('fixtures')}
				data-testid="error-use-fixtures"
			>
				Continue with fixture data
			</button>
		{/if}
	</div>
</div>
