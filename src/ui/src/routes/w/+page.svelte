<script lang="ts">
	import { goto } from '$app/navigation';
	import { listWorkspaces } from '$lib/api';
	import { setLastWorkspace, ui } from '$lib/workspace.svelte';
	import type { WorkspaceSummary } from '$lib/types';

	let workspaces = $state<WorkspaceSummary[]>([]);
	let loading = $state(true);
	let redirected = $state(false);

	$effect(() => {
		let cancelled = false;
		listWorkspaces().then((items) => {
			if (cancelled) return;
			workspaces = items;
			loading = false;
			if (!redirected && ui.lastWorkspace && items.some((w) => w.id === ui.lastWorkspace)) {
				redirected = true;
				goto(`/w/${ui.lastWorkspace}`, { replaceState: true });
			}
		});
		return () => {
			cancelled = true;
		};
	});

	function enter(id: string): void {
		setLastWorkspace(id);
		goto(`/w/${id}`);
	}
</script>

<svelte:head><title>Thesis — workspaces</title></svelte:head>

<div class="grid-bg flex min-h-screen items-center justify-center p-6">
	<div class="w-full max-w-3xl">
		<div class="mb-8 flex items-center gap-3">
			<span
				class="flex h-11 w-11 items-center justify-center rounded-xl border border-accent/40 bg-accent/10 font-mono text-lg font-bold text-accent glow-cyan"
				aria-hidden="true"
			>
				Θ
			</span>
			<div>
				<h1 class="text-2xl font-semibold tracking-tight text-white">
					thesis<span class="text-accent">.</span>
				</h1>
				<p class="text-sm text-slate-500">Pick a workspace to continue. Every view is workspace-scoped.</p>
			</div>
		</div>

		{#if loading}
			<div class="h-24 animate-pulse rounded-xl border border-white/5 bg-surface-850/40"></div>
		{:else}
			<ul class="grid gap-3 sm:grid-cols-2">
				{#each workspaces as workspace (workspace.id)}
					<li>
						<button
							type="button"
							class="w-full rounded-xl border border-white/5 bg-surface-850/60 p-4 text-left transition hover:border-accent/40 hover:bg-surface-800/70"
							onclick={() => enter(workspace.id)}
							data-testid="picker-workspace"
							data-workspace={workspace.id}
						>
							<div class="flex items-center gap-3">
								<span
									class="flex h-10 w-10 items-center justify-center rounded-lg border border-accent/30 bg-accent/10 font-mono text-sm font-semibold text-accent"
								>
									{workspace.initials}
								</span>
								<div>
									<p class="font-semibold text-slate-100">{workspace.name}</p>
									<p class="text-xs text-slate-500">{workspace.description}</p>
								</div>
							</div>
						</button>
					</li>
				{/each}
			</ul>
			<p class="mt-6 text-center text-xs text-slate-600">
				Create additional workspaces from the sidebar switcher.
			</p>
		{/if}
	</div>
</div>
