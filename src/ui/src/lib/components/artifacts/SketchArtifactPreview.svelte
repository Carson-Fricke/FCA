<script lang="ts">
	import type { SketchArtifactPreview } from '$lib/types';
	let { preview, workspaceId }: { preview: SketchArtifactPreview; workspaceId: string } = $props();
	const sketch = $derived(preview.sketch);
</script>

<section class="space-y-4 rounded-xl border border-surface-700 bg-surface-900 p-4" data-testid="strategy-sketch">
	<p class="font-mono text-xs text-slate-500">Revision {preview.reference.version.slice(0, 12)}</p>
	{#each [['Premise', sketch.premise], ['Mechanism', sketch.mechanism], ['Profit source', sketch.profitSource], ['Conditioning', sketch.conditioning], ['Entry', sketch.entry], ['Exit', sketch.exit], ['Abstention', sketch.abstention]] as [label, text]}
		<div><h3 class="text-sm font-semibold">{label}</h3><p class="whitespace-pre-wrap text-sm text-slate-600">{text}</p></div>
	{/each}
	<h3 class="text-sm font-semibold">Representation graph</h3>
	{#each sketch.representations as node (node.nodeId)}
		<article class="rounded border border-surface-600 p-3 text-sm">
			<p class="font-mono">{node.inputs.join(' + ') || 'Market inputs'} → {node.nodeId} · {node.timeframes.join(', ')}</p>
			<p>{node.description}</p><p class="text-xs text-slate-500">Available: {node.informationTime}</p>
			{#if node.componentReference}<a class="text-accent" href={`/w/${workspaceId}/artifacts?artifact=${node.componentReference.kind}:${node.componentReference.programId}`}>{node.componentReference.programId} · {node.componentReference.version.slice(0, 12)}</a>{/if}
		</article>
	{/each}
	<h3 class="text-sm font-semibold">Assumptions and investigations</h3>
	{#each sketch.assumptions as assumption (assumption.assumptionId)}
		<article class="rounded border border-surface-600 p-3 text-sm">
			<p><strong>{assumption.assumptionId}</strong> · {assumption.status}</p><p>{assumption.claim}</p><p class="text-slate-500">{assumption.investigation}</p>
			{#each assumption.evidence as evidence}<a class="mr-3 text-accent" href={`/w/${workspaceId}/exploration/${evidence.jobId}`}>{evidence.jobId}{evidence.artifactName ? ` / ${evidence.artifactName}` : ''}</a>{/each}
		</article>
	{/each}
	{#if sketch.alternatives.length}<h3 class="text-sm font-semibold">Competing explanations</h3>{#each sketch.alternatives as alternative}<p class="text-sm">{alternative}</p>{/each}{/if}
	<p class="text-sm text-slate-500">Limitations: {sketch.limitations}</p>
</section>
