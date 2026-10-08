<script lang="ts">
	import type { ArtifactKind, WorkspaceArtifact } from '$lib/types';

	/** Grouped artifact/file tree; selection drives the detail pane. */
	let {
		artifacts,
		selectedId,
		selectedPath,
		onSelect
	}: {
		artifacts: WorkspaceArtifact[];
		selectedId: string | null;
		selectedPath: string | null;
		onSelect: (artifact: WorkspaceArtifact, path: string) => void;
	} = $props();

	const GROUPS: { kind: ArtifactKind; label: string }[] = [
		{ kind: 'strategy', label: 'Strategies' },
		{ kind: 'study', label: 'Studies' },
		{ kind: 'indicator', label: 'Indicators' }
	];

	const grouped = $derived(
		GROUPS.map((group) => ({
			...group,
			items: artifacts.filter((artifact) => artifact.kind === group.kind)
		})).filter((group) => group.items.length > 0)
	);
</script>

<div class="self-start rounded-xl border border-surface-700 bg-surface-900 p-3" data-testid="artifact-tree">
	{#each grouped as group (group.kind)}
		<div class="mb-2 last:mb-0">
			<p class="px-2 py-1 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
				{group.label}
			</p>
			{#each group.items as artifact (artifact.id)}
				<div>
					<button
						type="button"
						onclick={() => onSelect(artifact, artifact.files[0]?.path ?? '')}
						class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs transition {selectedId ===
						artifact.id
							? 'bg-accent/10 text-accent'
							: 'text-slate-700 hover:bg-surface-800'}"
						data-testid="artifact-tree-item"
					>
						<span class="truncate">{artifact.title}</span>
						<span class="ml-auto shrink-0 font-mono text-[10px] text-slate-500">
							{artifact.version}
						</span>
					</button>
					{#if selectedId === artifact.id}
						<ul class="mt-0.5 mb-1 ml-3 space-y-0.5 border-l border-surface-600 pl-2">
							{#each artifact.files as file (file.path)}
								<li>
									<button
										type="button"
										onclick={() => onSelect(artifact, file.path)}
										class="block w-full truncate rounded px-2 py-1 text-left font-mono text-[10px] transition {selectedPath ===
										file.path
											? 'bg-accent/10 text-accent'
											: 'text-slate-500 hover:bg-surface-800 hover:text-slate-700'}"
										data-testid="artifact-tree-file"
									>
										{file.path}
									</button>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/each}
		</div>
	{/each}
</div>
