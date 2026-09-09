<script lang="ts">
	import { page } from '$app/state';
</script>

<div class="grid-bg flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
	<p class="font-mono text-sm text-loss">{page.status}</p>
	<h1 class="text-xl font-semibold text-white">{page.error?.message ?? 'Something went wrong'}</h1>
	<a href="/w" class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-sm text-accent hover:bg-accent/25">
		Back to workspaces
	</a>
</div>
