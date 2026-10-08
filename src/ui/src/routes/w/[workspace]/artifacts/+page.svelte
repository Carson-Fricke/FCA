<script lang="ts">
	import ArtifactCard from '$lib/components/artifacts/ArtifactCard.svelte';
	import ArtifactDetail from '$lib/components/artifacts/ArtifactDetail.svelte';
	import ArtifactTree from '$lib/components/artifacts/ArtifactTree.svelte';
	import EmptyState from '$lib/components/common/EmptyState.svelte';
	import { listWorkspaceArtifacts } from '$lib/api';
	import type { ArtifactKind, WorkspaceArtifact, WorkspaceArtifactCatalog } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const workspaceId = $derived(data.workspaceId);

	let catalog = $state<WorkspaceArtifactCatalog | null>(null);
	let view = $state<'gallery' | 'explorer'>('gallery');
	let kindFilter = $state<'all' | ArtifactKind>('all');
	let query = $state('');
	let selectedId = $state<string | null>(null);
	let selectedPath = $state<string | null>(null);

	$effect(() => {
		let cancelled = false;
		listWorkspaceArtifacts(workspaceId).then((document) => {
			if (!cancelled) catalog = document;
		});
		return () => {
			cancelled = true;
		};
	});

	const items = $derived(catalog?.items ?? []);

	const counts = $derived.by((): Record<'all' | ArtifactKind, number> => {
		const byKind = { strategy: 0, study: 0, indicator: 0 };
		for (const item of items) byKind[item.kind] += 1;
		return { all: items.length, ...byKind };
	});

	const filtered = $derived(
		items.filter((item) => {
			if (kindFilter !== 'all' && item.kind !== kindFilter) return false;
			const term = query.trim().toLowerCase();
			if (!term) return true;
			return `${item.title} ${item.subtitle} ${item.description} ${item.artifact_path}`
				.toLowerCase()
				.includes(term);
		})
	);

	const selected = $derived(items.find((item) => item.id === selectedId) ?? null);

	const FILTERS: { id: 'all' | ArtifactKind; label: string }[] = [
		{ id: 'all', label: 'All' },
		{ id: 'strategy', label: 'Strategies' },
		{ id: 'study', label: 'Studies' },
		{ id: 'indicator', label: 'Indicators' }
	];

	const STATS: { id: ArtifactKind; label: string; dot: string }[] = [
		{ id: 'strategy', label: 'Strategies', dot: 'bg-accent' },
		{ id: 'study', label: 'Studies', dot: 'bg-warn' },
		{ id: 'indicator', label: 'Indicators', dot: 'bg-profit' }
	];

	function open(artifact: WorkspaceArtifact): void {
		selectedId = artifact.id;
		selectedPath = artifact.files[0]?.path ?? null;
		view = 'explorer';
	}

	function openArtifact(artifactId: string): void {
		const artifact = items.find((item) => item.id === artifactId);
		if (artifact) open(artifact);
	}

	function selectFromTree(artifact: WorkspaceArtifact, path: string): void {
		selectedId = artifact.id;
		selectedPath = path;
	}
</script>

<svelte:head><title>Workspace artifacts — Thesis</title></svelte:head>

<div class="mx-auto max-w-6xl space-y-5">
	<header class="flex flex-wrap items-end gap-3">
		<div>
			<h1 class="text-xl font-semibold text-slate-800">Workspace artifacts</h1>
			<p class="text-sm text-slate-500">
				Durable strategies, studies, and indicators committed to this workspace.
			</p>
		</div>
		<div class="ml-auto flex overflow-hidden rounded-lg border border-surface-600">
			<button
				type="button"
				class="px-3 py-1.5 text-xs {view === 'gallery' ? 'bg-accent/15 text-accent' : 'text-slate-500'}"
				onclick={() => (view = 'gallery')}
			>
				Gallery
			</button>
			<button
				type="button"
				class="px-3 py-1.5 text-xs {view === 'explorer' ? 'bg-accent/15 text-accent' : 'text-slate-500'}"
				onclick={() => (view = 'explorer')}
			>
				Explorer
			</button>
		</div>
	</header>

	{#if !catalog}
		<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
			{#each [0, 1, 2] as index (index)}
				<div class="h-44 animate-pulse rounded-xl border border-surface-700 bg-surface-900"></div>
			{/each}
		</div>
	{:else if items.length === 0}
		<EmptyState
			title="No artifacts committed yet"
			hint="Strategies, studies, and indicators appear here once they are committed to the workspace repository."
		/>
	{:else}
		<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" data-testid="artifact-stats">
			<div class="rounded-xl border border-surface-700 bg-surface-900 px-4 py-3">
				<div class="text-[10px] font-semibold tracking-wider text-slate-500 uppercase">Artifacts</div>
				<div class="mt-1 font-mono text-lg text-slate-800">{counts.all}</div>
			</div>
			{#each STATS as stat (stat.id)}
				<div class="rounded-xl border border-surface-700 bg-surface-900 px-4 py-3">
					<div class="flex items-center gap-1.5 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
						<span class="h-1.5 w-1.5 rounded-full {stat.dot}" aria-hidden="true"></span>
						{stat.label}
					</div>
					<div class="mt-1 font-mono text-lg text-slate-800">{counts[stat.id]}</div>
				</div>
			{/each}
		</div>

		<div class="flex flex-wrap items-center gap-2">
			{#each FILTERS as filter (filter.id)}
				<button
					type="button"
					onclick={() => (kindFilter = filter.id)}
					class="rounded-full border px-3 py-1 text-xs transition {kindFilter === filter.id
						? 'border-accent/50 bg-accent/10 text-accent'
						: 'border-surface-600 text-slate-500 hover:border-accent/30'}"
				>
					{filter.label}
					<span class="ml-0.5 font-mono text-[10px]">{counts[filter.id]}</span>
				</button>
			{/each}
			<input
				type="search"
				placeholder="Filter artifacts…"
				bind:value={query}
				class="ml-auto w-56 rounded-lg border border-surface-600 bg-surface-900 px-3 py-1.5 text-xs text-slate-700 outline-none"
				aria-label="Filter artifacts"
			/>
		</div>

		{#if filtered.length === 0}
			<EmptyState title="No artifacts match" hint="Adjust the kind filter or search term." />
		{:else if view === 'gallery'}
			<div class="grid items-start gap-3 sm:grid-cols-2 lg:grid-cols-3" data-testid="artifact-gallery">
				{#each filtered as artifact (artifact.id)}
					<ArtifactCard
						{artifact}
						active={artifact.id === selectedId}
						onSelect={() => open(artifact)}
					/>
				{/each}
			</div>
		{:else}
			<div class="grid items-start gap-4 lg:grid-cols-[17rem_1fr]" data-testid="artifact-explorer">
				<ArtifactTree
					artifacts={filtered}
					{selectedId}
					{selectedPath}
					onSelect={selectFromTree}
				/>
				{#if selected}
					<ArtifactDetail
						{workspaceId}
						artifact={selected}
						filePath={selectedPath}
						onSelectFile={(path) => (selectedPath = path)}
						onOpenArtifact={openArtifact}
					/>
				{:else}
					<EmptyState
						title="Select an artifact"
						hint="Pick an artifact or one of its files from the tree to inspect it."
					/>
				{/if}
			</div>
		{/if}
	{/if}
</div>
