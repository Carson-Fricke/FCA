<script lang="ts">
	import { untrack } from 'svelte';
	import type { Panel } from './contracts';
	import EvidencePanel from './EvidencePanel.svelte';
	import { researchPanels } from './registry';
	import { manualCandidates, defaultPanel, filteredEvidence, recommendation, type SemanticOutput } from './semantics';
	import { transform, type Reduction } from './transforms';
	let { outputs, initialDataset = outputs[0].dataset.id, initialQuery = '', hideDatasetPicker = false }: { outputs: SemanticOutput[]; initialDataset?: string; initialQuery?: string; hideDatasetPicker?:boolean } = $props();
	let datasetId = $state(untrack(() => initialDataset));
	let choice = $state(untrack(() => initialQuery ? 'table' : 'recommended'));
	let query = $state(untrack(() => initialQuery));
	let page = $state(0);
	let filters = $state<Record<string,string>>({});
	let custom = $state<Panel | null>(null);
	let bins=$state<Record<string,number>>({});let reduction=$state<Reduction>('none');
	const output = $derived(outputs.find(o => o.dataset.id === datasetId) ?? outputs[0]);
	const suggested = $derived(recommendation(output));
	const panel = $derived(choice === 'recommended' ? suggested.panel : choice.startsWith('suggestion:') ? output.views.find(p=>p.id===choice.slice(11))??suggested.panel : custom ?? defaultPanel(output, choice));
	const data = $derived(filteredEvidence(output, query, filters));
	const display=$derived(transform(output,panel,data,bins,reduction));
	const visible = $derived(data.rows.slice(page * 6, page * 6 + 6));
	const viewKinds=Object.entries(researchPanels).map(([kind,extension])=>({kind,label:extension.label??kind}));
	const channels=$derived([...new Set([...(researchPanels[panel.kind]?.required??[]),...(['line','scatter','comparison','events'].includes(panel.kind)?['color','series']:[])])]);
	const manual=$derived(choice!=='recommended'&&!choice.startsWith('suggestion:'));
	function changeDataset(value: string) { datasetId = value; choice = 'recommended'; custom = null; query = ''; page = 0; filters = {}; bins={};reduction='none'; }
	function changeView(value: string) { choice = value; custom = value === 'recommended'||value.startsWith('suggestion:') ? null : defaultPanel(output,value);if(custom){const fields=manualCandidates(output);for(const [index,channel] of (researchPanels[value]?.required??[]).entries())if(!custom.encodings[channel])custom.encodings[channel]=fields[index]??fields[0]??'';} page = 0;bins={};reduction='none'; }
	function changeField(channel: string, value: string) { custom = { ...panel, encodings: { ...panel.encodings, [channel]: value } }; bins={}; }
	const fieldLabel = (name: string) => { const field = output.dataset.fields.find(f => f.name === name); return `${field?.label ?? name}${field?.unit ? ` · ${field.unit}` : ''}`; };
</script>

<section class="returned-browser" aria-label="Returned data browser">
	<div class="controls">
		{#if !hideDatasetPicker}<label>Data<select aria-label="Data" value={datasetId} onchange={event => changeDataset(event.currentTarget.value)}>{#each outputs as item}<option value={item.dataset.id}>{item.dataset.id} · {item.evidence.rows.length}</option>{/each}</select></label>{/if}
		<label>View<select aria-label="View" value={choice} onchange={event => changeView(event.currentTarget.value)}><option value="recommended">Recommended · {viewKinds.find(k => k.kind === suggested.panel.kind)?.label ?? suggested.panel.kind}</option>{#each output.views.filter((p,i,all)=>all.findIndex(v=>v.id===p.id)===i) as p}<option value={`suggestion:${p.id}`}>{p.kind} · {p.title}</option>{/each}{#each viewKinds as kind}<option value={kind.kind}>{kind.label}</option>{/each}</select></label>
		<label class="search">Filter<input aria-label="Filter returned data" placeholder="Find a field or value…" bind:value={query} oninput={() => page = 0} /></label>
	</div>
	<p class="coverage">{output.evidence.rows.length} loaded{output.evidence.partial ? ` of ${output.evidence.total}` : ''} observations · {output.evidence.partial ? 'filters and mapping checks cover this preview only' : 'complete returned dataset'}</p>
	<details class="why"><summary>Why this view?</summary><p>{choice === 'recommended' ? suggested.reason : `Custom mapping. ${output.note}`}</p><p>Recommendations use published meaning and observed shape. Manual views allow every field, including identifiers and repeated axes. Heatmap collisions need an explicit aggregation or dimension filter.</p></details>
	{#if output.profiles?.length}<details class="why"><summary>Field meanings & distributions · complete output</summary>{#each output.profiles as field}<p><strong>{field.label}</strong> — {field.description}<br/>{field.field_type} · {field.cardinality} distinct · {field.missing} missing{field.range?` · range ${field.range.join(' … ')}`:''}{field.quantiles?` · quartiles ${field.quantiles.join(', ')}`:''}</p>{#if field.histogram.length}<div class="histogram" aria-label={`${field.label} distribution`}>{#each field.histogram as bucket}<span title={`${bucket.lower}…${bucket.upper}: ${bucket.count}`} style:height={`${Math.max(2,32*bucket.count/Math.max(...field.histogram.map(b=>b.count)))}px`}></span>{/each}</div>{/if}{/each}</details>{/if}
	{#if manual && panel.kind !== 'table'}
		<div class="mappings">{#each channels as channel}<label>{channel === 'value' ? 'Color measure' : channel==='color'?'Color field':channel==='series'?'Series field':`${channel.toUpperCase()} field`}<select aria-label={channel === 'value' ? 'Color measure' : channel==='color'?'Color field':channel==='series'?'Series field':`${channel.toUpperCase()} field`} value={panel.encodings[channel]??''} onchange={event => changeField(channel,event.currentTarget.value)}>{#if !researchPanels[panel.kind]?.required.includes(channel)}<option value="">None</option>{/if}{#each manualCandidates(output) as name}<option value={name}>{fieldLabel(name)}</option>{/each}</select></label>{/each}</div>
		{#if ['heatmap','scatter'].includes(panel.kind)}<div class="mappings">{#each ['x','y'] as channel}<label>{channel.toUpperCase()} buckets<select aria-label={`${channel.toUpperCase()} buckets`} value={bins[channel]??0} onchange={event=>bins={...bins,[channel]:Number(event.currentTarget.value)}}>{#each [0,5,10,20,40] as count}<option value={count}>{count?`${count} equal-width buckets`:'Exact values'}</option>{/each}</select></label>{/each}{#if panel.kind==='heatmap'}<label>Cell aggregation<select aria-label="Cell aggregation" bind:value={reduction}>{#each ['none','mean','sum','min','max','count'] as method}<option value={method}>{method==='none'?'None · one observation per cell':method}</option>{/each}</select></label>{/if}</div>{/if}
	{/if}
	{#if panel.kind !== 'table' && output.dimensions.length}
		<details class="slice"><summary>Filter dimensions</summary><div class="mappings">{#each output.dimensions as name}<label>{fieldLabel(name)}<select aria-label={`Filter ${name}`} value={filters[name] ?? ''} onchange={event => { filters = { ...filters, [name]: event.currentTarget.value }; page = 0; }}><option value="">All</option>{#each [...new Set(output.evidence.rows.map(row => JSON.stringify(row[name] ?? null)))] as value}<option {value}>{JSON.parse(value) ?? 'Missing'}</option>{/each}</select></label>{/each}</div></details>
	{/if}
	{#if panel.kind === 'table'}
		<div class="table-scroll"><table><thead><tr>{#each output.evidence.columns as column}<th>{column}</th>{/each}</tr></thead><tbody>{#each visible as row}<tr>{#each output.evidence.columns as column}<td>{row[column] == null ? 'Unavailable' : String(row[column])}</td>{/each}</tr>{/each}</tbody></table>{#if !data.rows.length}<p class="empty">No matching rows. Clear the filter to see all data.</p>{/if}</div>
		<div class="pagination"><span>{data.rows.length ? `${page * 6 + 1}–${Math.min(page * 6 + 6,data.rows.length)}` : '0'} of {data.rows.length} {datasetId === 'Metrics' ? 'metrics' : 'rows'}</span><div><button aria-label="Previous page" disabled={page === 0} onclick={() => page--}>←</button><button aria-label="Next page" disabled={(page + 1)*6 >= data.rows.length} onclick={() => page++}>→</button></div></div>
	{:else}
		{#if display.problem}<p class="coverage" role="status">{display.problem}</p>{:else}{#key JSON.stringify([datasetId,panel.kind,panel.encodings,filters,query,bins,reduction])}<EvidencePanel output={display.output} panel={display.panel} evidence={display.evidence} manualMapping={manual} />{/key}{/if}
		{#if Object.values(bins).some(Boolean)||reduction!=='none'}<p class="coverage">Exploratory display: {reduction==='none'?'no aggregation':reduction} · {display.evidence.rows.length} displayed cells. Calculated from matching loaded observations; source rows and exported evidence are unchanged.</p>{/if}
		<p class="row-count">{data.rows.length} matching loaded observations · charts use all matching loaded rows, not a table page.</p>
	{/if}
	{#if Object.values(filters).some(Boolean)}<button class="clear" onclick={() => filters = {}}>Clear dimension filters</button>{/if}
</section>

<style>
	.returned-browser{min-width:0}.controls,.mappings{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px}label{display:flex;flex-direction:column;gap:5px;font-size:9px;color:var(--color-slate-400);min-width:0;max-width:100%}.search{flex:1;min-width:120px}select,input{font:inherit;font-size:10px;height:30px;border:1px solid var(--border);border-radius:4px;background:var(--color-surface-900);color:var(--text);padding:0 7px;min-width:0;max-width:100%}.controls>label:not(.search){flex:1;min-width:115px}
	.histogram{display:flex;gap:2px;height:34px;align-items:end;max-width:280px}.histogram span{flex:1;background:var(--color-accent);opacity:.6}
	.why,.slice{font-size:10px;color:var(--color-slate-400);margin:10px 0 14px}summary{cursor:pointer}.why p{line-height:1.7;margin:8px 0}.slice .mappings{margin-top:12px}.mappings label{flex:1;min-width:95px}.table-scroll{height:238px;overflow:auto;border-bottom:1px solid var(--border)}table{border-collapse:collapse;white-space:nowrap;font-size:10px;width:100%;font-variant-numeric:tabular-nums}th{text-align:left;color:var(--color-slate-400);font-weight:500;background:var(--color-surface-800);position:sticky;top:0}td,th{padding:9px 12px;border-bottom:1px solid var(--border)}
	.pagination{display:flex;justify-content:space-between;align-items:center;font-size:10px;color:var(--color-slate-400);margin-top:10px}.pagination button{border:1px solid var(--border);background:white;border-radius:3px;padding:2px 8px;margin-left:4px;cursor:pointer}.pagination button:disabled{opacity:.35;cursor:default}.coverage,.empty,.row-count{font-size:10px;color:var(--color-slate-400);line-height:1.6}.empty{padding:20px}.clear{font-size:10px;color:var(--color-accent);border:0;background:none;cursor:pointer;margin-top:10px}
</style>
