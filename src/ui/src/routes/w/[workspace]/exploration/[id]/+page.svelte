<script lang="ts">
 import { page } from '$app/state';
 import ResearchExplorer from '$lib/research/ResearchExplorer.svelte';
</script>
<svelte:head><title>Research explorer — Thesis</title></svelte:head>
<div class="mx-auto max-w-7xl p-4"><ResearchExplorer jobId={page.params.id??''} workspaceId={page.params.workspace??''}/></div>
