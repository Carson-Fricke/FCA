<script lang="ts">
	import { goto, afterNavigate } from '$app/navigation';
	import { createWorkspace, listWorkspaces } from '$lib/api';
	import CreateWorkspaceDialog from '$lib/components/shell/CreateWorkspaceDialog.svelte';
	import DataModeBadge from '$lib/components/shell/DataModeBadge.svelte';
	import { notify, setLastWorkspace, ui } from '$lib/workspace.svelte';
	import type { WorkspaceSummary } from '$lib/types';

	let workspaces = $state<WorkspaceSummary[]>([]);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let createOpen = $state(false);
	/** Remembered id surfaced as a notice instead of a blind redirect. */
	let staleNotice = $state<string | null>(null);

	afterNavigate((navigation) => {
		// Auto-resume only on a fresh page load; in-app back navigation to
		// this picker must stay where the user put it.
		if (navigation.type !== 'enter') return;
		if (ui.lastWorkspace) staleNotice = ui.lastWorkspace;
	});

	$effect(() => {
		let cancelled = false;
		listWorkspaces()
			.then((items) => {
				if (cancelled) return;
				workspaces = items;
				loading = false;
				if (ui.lastWorkspace && items.some((w) => w.id === ui.lastWorkspace)) {
					goto(`/w/${ui.lastWorkspace}`, { replaceState: true });
				} else if (ui.lastWorkspace && staleNotice === null) {
					staleNotice = ui.lastWorkspace;
				}
			})
			.catch((error) => {
				if (cancelled) return;
				loadError = error instanceof Error ? error.message : String(error);
				loading = false;
			});
		return () => {
			cancelled = true;
		};
	});

	function enter(id: string): void {
		setLastWorkspace(id);
		staleNotice = null;
		goto(`/w/${id}`);
	}

	function dismissStale(): void {
		staleNotice = null;
		if (typeof localStorage !== 'undefined') localStorage.removeItem('thesis.workspace');
	}

	async function onCreate(input: { name: string; description: string }): Promise<void> {
		try {
			const workspace = await createWorkspace(input);
			notify(`Workspace “${workspace.name}” created`);
			enter(workspace.id);
		} catch (error) {
			notify(`Workspace creation failed: ${error instanceof Error ? error.message : error}`, true);
		}
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
			<div class="flex-1">
				<h1 class="text-2xl font-semibold tracking-tight text-white">
					thesis<span class="text-accent">.</span>
				</h1>
				<p class="text-sm text-slate-500">Pick a workspace to continue. Every view is workspace-scoped.</p>
			</div>
			<DataModeBadge />
		</div>

		{#if staleNotice}
			<div
				class="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-amber-400/30 bg-amber-400/10 p-3 text-sm"
				data-testid="stale-workspace-notice"
			>
				<span class="text-amber-200">
					Last workspace <span class="font-mono">{staleNotice}</span> is no longer available.
				</span>
				<button
					type="button"
					class="ml-auto rounded-lg border border-amber-400/40 bg-amber-400/15 px-2.5 py-1 text-xs font-semibold text-amber-200 hover:bg-amber-400/25"
					onclick={() => enter(staleNotice!)}
				>
					Open anyway
				</button>
				<button
					type="button"
					class="rounded border border-white/10 px-2.5 py-1 text-xs text-slate-400 hover:text-slate-200"
					onclick={dismissStale}
				>
					Dismiss
				</button>
			</div>
		{/if}

		{#if loading}
			<div class="h-24 animate-pulse rounded-xl border border-white/5 bg-surface-850/40"></div>
		{:else if loadError}
			<div class="rounded-xl border border-loss/30 bg-loss/10 p-4 text-sm text-slate-300">
				<p class="font-semibold text-loss">Workspace list unavailable</p>
				<p class="mt-1 text-xs text-slate-400">{loadError}</p>
				<div class="mt-3 flex gap-2">
					<button
						type="button"
						class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/25"
						onclick={() => location.reload()}
					>
						Retry
					</button>
					{#if !fixtureMode}
						<button
							type="button"
							class="rounded-lg border border-white/10 px-3 py-1.5 text-xs text-slate-300 hover:border-accent/40 hover:text-accent"
							onclick={() => setFixtureMode(true)}
							data-testid="picker-use-fixtures"
						>
							Continue with fixture data
						</button>
					{/if}
				</div>
			</div>
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
			<div class="mt-6 flex items-center justify-center gap-3">
				<button
					type="button"
					class="rounded-lg border border-accent/40 bg-accent/15 px-3 py-1.5 text-sm font-semibold text-accent hover:bg-accent/25"
					onclick={() => (createOpen = true)}
					data-testid="picker-create-workspace"
				>
					+ New workspace
				</button>
			</div>
		{/if}
	</div>
</div>

{#if createOpen}
	<CreateWorkspaceDialog onClose={() => (createOpen = false)} onCreate={onCreate} />
{/if}
