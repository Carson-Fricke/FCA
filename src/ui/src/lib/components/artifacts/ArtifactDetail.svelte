<script lang="ts">
	import ArtifactKindBadge from './ArtifactKindBadge.svelte';
	import FileViewer from './FileViewer.svelte';
	import HistoryTimeline from './HistoryTimeline.svelte';
	import IndicatorArtifactPreview from './IndicatorArtifactPreview.svelte';
	import StrategyArtifactPreview from './StrategyArtifactPreview.svelte';
	import StudyArtifactPreview from './StudyArtifactPreview.svelte';
	import { shortDate, shortHash } from '$lib/format';
	import type { WorkspaceArtifact } from '$lib/types';

	/** Full artifact detail: provenance header, kind hero, history, file viewer. */
	let {
		workspaceId,
		artifact,
		filePath,
		onSelectFile,
		onOpenArtifact
	}: {
		workspaceId: string;
		artifact: WorkspaceArtifact;
		filePath: string | null;
		onSelectFile: (path: string) => void;
		onOpenArtifact?: (artifactId: string) => void;
	} = $props();

	const activeFile = $derived(
		artifact.files.find((file) => file.path === filePath) ?? artifact.files[0] ?? null
	);
</script>

<div class="space-y-4" data-testid="artifact-detail">
	<header class="rounded-xl border border-surface-700 bg-surface-900 p-4">
		<div class="flex flex-wrap items-center gap-2">
			<ArtifactKindBadge kind={artifact.kind} />
			<h2 class="text-base font-semibold text-slate-800">{artifact.title}</h2>
			<span class="rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500">
				{artifact.version}
			</span>
			<span class="rounded-full border border-surface-600 px-2 py-0.5 font-mono text-[10px] text-slate-500">
				{artifact.status}
			</span>
			<span class="ml-auto truncate font-mono text-[10px] text-slate-500">{artifact.artifact_path}</span>
		</div>
		{#if artifact.description}
			<p class="mt-2 text-xs text-slate-500">{artifact.description}</p>
		{/if}
		<div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] text-slate-500">
			<span>source {shortHash(artifact.digests.source_sha256)}</span>
			<span>bundle {shortHash(artifact.digests.bundle_sha256)}</span>
			{#if artifact.updated_at}
				<span>updated {shortDate(artifact.updated_at)}</span>
			{/if}
		</div>
	</header>

	{#if artifact.preview.type === 'strategy'}
		<StrategyArtifactPreview preview={artifact.preview} inputs={artifact.inputs} />
	{:else if artifact.preview.type === 'study'}
		<StudyArtifactPreview
			preview={artifact.preview}
			inputs={artifact.inputs}
			{onOpenArtifact}
		/>
	{:else}
		<IndicatorArtifactPreview
			preview={artifact.preview}
			inputs={artifact.inputs}
			{onOpenArtifact}
		/>
	{/if}

	<HistoryTimeline history={artifact.history} />

	<section class="overflow-hidden rounded-xl border border-surface-700 bg-surface-900" data-testid="artifact-files">
		<div class="flex flex-wrap items-center gap-2 border-b border-surface-700 px-4 py-2">
			<h3 class="text-sm font-semibold text-slate-800">Files</h3>
			<div class="ml-auto flex flex-wrap gap-1">
				{#each artifact.files as file (file.path)}
					<button
						type="button"
						onclick={() => onSelectFile(file.path)}
						class="rounded-md px-2 py-0.5 font-mono text-[10px] transition {activeFile?.path ===
						file.path
							? 'bg-accent/15 text-accent'
							: 'text-slate-500 hover:bg-surface-800'}"
						data-testid="artifact-file-tab"
					>
						{file.path}
					</button>
				{/each}
			</div>
		</div>
		{#if activeFile}
			{#key artifact.id + activeFile.path}
				<FileViewer {workspaceId} artifactId={artifact.id} file={activeFile} />
			{/key}
		{:else}
			<p class="p-4 text-xs text-slate-500">This artifact has no readable files.</p>
		{/if}
	</section>
</div>
