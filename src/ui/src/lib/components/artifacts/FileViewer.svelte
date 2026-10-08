<script lang="ts">
	import MarkdownBody from '$lib/components/chat/MarkdownBody.svelte';
	import { getArtifactFile } from '$lib/api';
	import { shortHash } from '$lib/format';
	import type { ArtifactFileContent, ArtifactFileEntry } from '$lib/types';

	/** Bounded UTF-8 artifact file viewer: code with line numbers, JSON, markdown. */
	let {
		workspaceId,
		artifactId,
		file
	}: { workspaceId: string; artifactId: string; file: ArtifactFileEntry } = $props();

	const MAX_VIEW_LINES = 400;

	let content = $state<ArtifactFileContent | null>(null);
	let loading = $state(true);
	let error = $state('');

	$effect(() => {
		const targetWorkspace = workspaceId;
		const targetArtifact = artifactId;
		const targetPath = file.path;
		let cancelled = false;
		loading = true;
		error = '';
		content = null;
		getArtifactFile(targetWorkspace, targetArtifact, targetPath)
			.then((document) => {
				if (!cancelled) content = document;
			})
			.catch((cause: unknown) => {
				if (!cancelled) error = cause instanceof Error ? cause.message : String(cause);
			})
			.finally(() => {
				if (!cancelled) loading = false;
			});
		return () => {
			cancelled = true;
		};
	});

	const prettyText = $derived.by(() => {
		if (!content) return '';
		if (file.viewer === 'json') {
			try {
				return JSON.stringify(JSON.parse(content.text), null, 2);
			} catch {
				return content.text;
			}
		}
		return content.text;
	});

	const lines = $derived(prettyText ? prettyText.replace(/\n$/, '').split('\n') : []);

	function formatBytes(value: number): string {
		if (value < 1024) return `${value} B`;
		if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
		return `${(value / (1024 * 1024)).toFixed(1)} MB`;
	}
</script>

<div data-testid="file-viewer">
	<div class="flex flex-wrap items-center gap-3 border-b border-surface-700 px-4 py-2 font-mono text-[10px] text-slate-500">
		<span class="text-slate-700">{file.path}</span>
		{#if content}
			<span>{formatBytes(content.size_bytes)}</span>
			<span>{shortHash(content.sha256)}</span>
			{#if content.truncated}
				<span class="text-warn">truncated by the API</span>
			{/if}
		{/if}
	</div>
	{#if loading}
		<div class="h-36 animate-pulse bg-surface-950"></div>
	{:else if error}
		<p class="p-4 text-xs text-loss">{error}</p>
	{:else if file.viewer === 'markdown'}
		<div class="max-h-96 overflow-auto p-4">
			<MarkdownBody text={prettyText} />
		</div>
	{:else if file.viewer === 'json'}
		<pre class="max-h-96 overflow-auto p-4 font-mono text-[11px] leading-5 text-slate-700">{prettyText}</pre>
	{:else}
		<div class="max-h-96 overflow-auto">
			<table class="w-full border-collapse font-mono text-[11px] leading-5">
				<tbody>
					{#each lines.slice(0, MAX_VIEW_LINES) as line, index (index)}
						<tr>
							<td class="w-10 border-r border-surface-700 px-2 text-right align-top text-slate-400 select-none">
								{index + 1}
							</td>
							<td class="px-3 whitespace-pre align-top text-slate-700">{line}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		{#if lines.length > MAX_VIEW_LINES}
			<p class="border-t border-surface-700 px-4 py-2 font-mono text-[10px] text-slate-500">
				Showing the first {MAX_VIEW_LINES} of {lines.length} lines.
			</p>
		{/if}
	{/if}
</div>
