<script lang="ts">
	import { researchPanels } from './registry';
	import { display, type Evidence, type Panel, type Selection } from './contracts';
	import { mappingProblem, sortedEvidence, type SemanticOutput } from './semantics';
	let { output, panel, evidence = output.evidence, manualMapping=false, oninspect, selection = $bindable(null), cursor = $bindable(null) }: { output: SemanticOutput; panel: Panel; evidence?: Evidence; manualMapping?:boolean; oninspect?: (selection: Selection) => void; selection?:Selection|null; cursor?:string|null } = $props();
	const problem = $derived(mappingProblem(output, panel, evidence,manualMapping));
	const extension = $derived(researchPanels[panel.kind]);
	const plotted = $derived(sortedEvidence(output, panel, evidence));
	const missing = $derived(evidence.rows.filter(row => Object.values(panel.encodings).some(field => field && row[field] == null)).length);
</script>

<div class="evidence-panel" data-kind={panel.kind}>
	{#if evidence.partial}<p class="notice">Bounded preview: {output.evidence.rows.length} of {evidence.total} observations. Filters and mapping checks cover loaded observations only. Export the complete artifact for full evidence.</p>{/if}
	{#if problem}<p class="notice" role="status">{problem}</p>
	{:else if !evidence.rows.length}<p class="notice">No matching observations. Clear the filters to restore the view.</p>
	{:else if extension && extension.required.every(channel => panel.encodings[channel])}
		{@const Component = extension.component}
		<Component {panel} dataset={output.dataset} evidence={plotted} {selection} onselect={value => selection = value} {cursor} oncursor={value => cursor = value} />
		{#if missing}<p class="notice">{missing} observations have missing mapped values. Missing values are not replaced with zero.</p>{/if}
	{:else}<p class="notice">This panel extension is unavailable. Its source data remains accessible in Returned data.</p>{/if}
	{#if selection && selection.dataset === output.dataset.id}
		<div class="selection" data-testid="selected-evidence"><div><b>Selected observation</b><button onclick={() => selection = null}>Clear</button></div><dl>{#each Object.entries(selection.row) as [key,value]}<dt>{key}</dt><dd>{display(value)}</dd>{/each}</dl>{#if oninspect}<button class="inspect" onclick={() => { if(selection) oninspect(selection); }}>Inspect source row ↗</button>{/if}</div>
	{/if}
</div>

<style>
	.evidence-panel{min-width:0;font-size:11px}.notice{font-size:11px;line-height:1.6;color:var(--color-slate-400);padding:12px 0}
	.evidence-panel :global(svg){min-width:0;width:100%}.evidence-panel :global(.plot-wrap){overflow:visible}.evidence-panel :global(.legend){flex-wrap:wrap;font-size:10px}.evidence-panel :global(.note){font-size:10px;line-height:1.6}
	.selection{border-top:1px solid var(--border);margin-top:12px;padding-top:12px}.selection>div{display:flex;justify-content:space-between;font-size:10px}.selection button{background:none;border:0;color:var(--color-accent);font-size:10px;cursor:pointer}
	dl{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.5fr);gap:5px 14px;font-size:10px;max-height:150px;overflow:auto;margin:10px 0}dt{color:var(--color-slate-400);overflow-wrap:anywhere}dd{margin:0;overflow-wrap:anywhere}
</style>
