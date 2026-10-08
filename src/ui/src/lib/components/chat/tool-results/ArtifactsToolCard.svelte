<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import { formatValue, isRecord, readArray, readString } from './safe';

	interface ArtifactRow {
		name?: unknown;
		uri?: unknown;
		sha256?: unknown;
		sizeBytes?: unknown;
		sas_url?: unknown;
		sasUrl?: unknown;
	}

	let { result }: { result: ToolResult; workspaceId: string } = $props();

	const data = $derived(isRecord(result.structured) ? result.structured : {});
	const jobId = $derived(readString(data, 'job_id'));
	const artifacts = $derived(readArray<ArtifactRow>(data, 'artifacts'));
	const rows = $derived(
		artifacts.map((artifact) => {
			const row = isRecord(artifact) ? artifact : {};
			return {
				name: readString(row, 'name') ?? readString(row, 'uri') ?? '(unnamed)',
				uri: readString(row, 'uri'),
				sha256: readString(row, 'sha256'),
				sizeBytes: row['sizeBytes'],
				link: readString(row, 'sas_url') ?? readString(row, 'sasUrl')
			};
		})
	);
</script>

<div class="space-y-2" data-testid="artifacts-tool-card">
	{#if jobId}
		<p class="font-mono text-[11px] text-slate-500">job {jobId}</p>
	{/if}

	{#if rows.length}
		<ul class="space-y-1">
			{#each rows as row, index (row.name + index)}
				<li class="rounded border border-surface-700 px-2 py-1 font-mono text-[11px]">
					<span class="text-slate-800">{row.name}</span>
					{#if row.sizeBytes !== undefined && row.sizeBytes !== null}
						<span class="text-slate-500"> · {formatValue(row.sizeBytes)} B</span>
					{/if}
					{#if row.sha256}
						<span class="text-slate-500"> · {row.sha256.slice(0, 12)}…</span>
					{/if}
					{#if row.link}
						<a
							class="ml-2 text-accent underline"
							href={row.link}
							target="_blank"
							rel="noreferrer"
						>
							open
						</a>
					{/if}
				</li>
			{/each}
		</ul>
	{:else}
		<p class="text-xs text-slate-500">{result.summary || 'No artifacts reported.'}</p>
	{/if}
</div>