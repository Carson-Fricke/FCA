<script lang="ts">
 import type { ToolResult } from '$lib/types';
 import {isRecord,readString} from './safe';
 import ResearchExplorer from '$lib/research/ResearchExplorer.svelte';
 let {result,workspaceId}:{result:ToolResult;workspaceId:string}=$props();
 const data=$derived(isRecord(result.structured)?result.structured:{});
 const sketch=$derived(isRecord(data.sketch)?data.sketch:{});
 const reference=$derived(isRecord(data.reference)?data.reference:{});
 const representations=$derived(Array.isArray(sketch.representations)?sketch.representations.filter(isRecord):[]);
 const assumptions=$derived(Array.isArray(sketch.assumptions)?sketch.assumptions.filter(isRecord):[]);
 let inspectJob=$state<string|null>(null);
 const changes=$derived(result.previousSketch?Object.keys(sketch).filter(key=>JSON.stringify(sketch[key])!==JSON.stringify(result.previousSketch![key])):[]);
 const assumptionStatuses=$derived([...new Set(assumptions.map(a=>readString(a,'status')??'Not declared'))].map(status=>({status,count:assumptions.filter(a=>(readString(a,'status')??'Not declared')===status).length})));
</script>
<section class="sketch-card" data-testid="sketch-tool-card">
 <header><p class="text-[10px] tracking-widest text-accent uppercase">Strategy sketch · {String(reference.version??'').slice(0,12)}</p><h3 class="mt-1 text-sm font-semibold">{readString(sketch,'title')??'Committed strategy sketch'}</h3></header>
 <p class="sketch-premise">{readString(sketch,'premise')}</p>
 <dl class="decision-summary">{#each [['Entry','entry'],['Exit','exit'],['Abstain','abstention']] as [label,key]}<div><dt>{label}</dt><dd>{readString(sketch,key)??'Not declared'}</dd></div>{/each}</dl>
 {#if changes.length}<details class="sketch-section"><summary>Changed in this revision · {changes.length} {changes.length===1?'section':'sections'}</summary>{#each changes as key}<div class="mt-3"><strong>{key}</strong><div class="grid gap-2 sm:grid-cols-2"><pre class="mt-1 max-h-36 overflow-auto whitespace-pre-wrap rounded bg-loss/5 p-2">{JSON.stringify(result.previousSketch?.[key],null,2)}</pre><pre class="mt-1 max-h-36 overflow-auto whitespace-pre-wrap rounded bg-profit/5 p-2">{JSON.stringify(sketch[key],null,2)}</pre></div></div>{/each}</details>{/if}
 <details class="sketch-section"><summary>Mechanism &amp; trading rules</summary><dl class="full-rules">{#each [['Premise','premise'],['Mechanism','mechanism'],['Profit source','profitSource'],['Conditioning','conditioning'],['Entry','entry'],['Exit','exit'],['Abstention','abstention']] as [label,key]}<dt>{label}</dt><dd>{readString(sketch,key)??'Not declared'}</dd>{/each}</dl></details>
 <details><summary class="text-xs text-accent">Representations and information timing · {representations.length}</summary><div class="mt-2 space-y-2">{#each representations as node}<article class="rounded border border-surface-600 p-2 text-xs"><strong>{readString(node,'nodeId')}</strong><p>{Array.isArray(node.inputs)?node.inputs.join(' + '):''} → {readString(node,'description')}</p><p class="text-slate-500">Available: {readString(node,'informationTime')}</p></article>{/each}</div></details>
 <details class="sketch-section"><summary>Assumptions · {assumptions.length}{#each assumptionStatuses as item}<span class="assumption-count">{item.count} {item.status}</span>{/each}</summary><div class="space-y-2">{#each assumptions as a}<article class="assumption text-xs"><div class="flex flex-wrap gap-2"><strong>{readString(a,'assumptionId')}</strong><span class="text-accent">{readString(a,'status')}</span></div><p class="mt-1">{readString(a,'claim')}</p><p class="mt-1 text-slate-500">{readString(a,'investigation')}</p>{#if Array.isArray(a.evidence)}<div class="mt-2 flex flex-wrap gap-2">{#each a.evidence.filter(isRecord) as e}{#if typeof e.jobId==='string'}<button class="text-accent" onclick={()=>inspectJob=e.jobId as string}>Inspect evidence · {e.jobId.slice(-8)}</button>{/if}{/each}</div>{/if}</article>{/each}</div></details>
 {#if inspectJob}<div class="inspected-evidence"><button class="text-xs text-accent" onclick={()=>inspectJob=null}>Close evidence</button><ResearchExplorer jobId={inspectJob}/></div>{/if}
 {#if Array.isArray(sketch.alternatives)&&sketch.alternatives.length}<details class="text-xs"><summary>Alternative explanations</summary><ul class="mt-2 list-disc pl-4">{#each sketch.alternatives as alternative}<li>{String(alternative)}</li>{/each}</ul></details>{/if}
 {#if readString(sketch,'limitations')}<details class="sketch-section"><summary>Limitations</summary><p class="text-xs text-slate-500">{readString(sketch,'limitations')}</p></details>{/if}
 <details class="text-[10px] text-slate-500"><summary>Exact revision and source</summary><pre class="max-h-56 overflow-auto">{JSON.stringify(data,null,2)}</pre></details>
</section>

<style>
 .sketch-card{border:1px solid var(--color-surface-600);border-radius:12px;background:var(--color-surface-900);padding:16px;text-align:left;min-width:0;overflow-wrap:anywhere}
 .sketch-premise{font-size:12px;line-height:1.6;margin:10px 0 12px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .decision-summary{margin-bottom:12px;font-size:12px}.decision-summary>div{display:grid;grid-template-columns:52px minmax(0,1fr);gap:8px;margin:6px 0}.decision-summary dt{color:var(--color-slate-400)}.decision-summary dd{display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden}
 .sketch-card :global(summary){font-size:12px;color:var(--color-accent);cursor:pointer;padding:6px 0}.sketch-card :global(details){border-top:1px solid var(--color-surface-700)}
 .assumption-count{font-size:11px;color:var(--color-slate-400);margin-left:8px}.assumption{border-bottom:1px solid var(--color-surface-700);padding:10px 0}
 .full-rules{font-size:12px;line-height:1.6;margin:8px 0 12px}.full-rules dt{font-weight:500;margin-top:10px}.full-rules dd{margin-top:3px;color:var(--color-slate-300)}.inspected-evidence{margin-top:12px}
</style>
