<script lang="ts">
 import type { PanelProps } from '../registry';
 import { display } from '../contracts';
 let { panel, dataset, evidence, selection, onselect }: PanelProps = $props();
 let limit=$state(50);
 const group=$derived(dataset.selection_group ?? dataset.id);
</script>
<div class="overflow-auto max-h-96">
 <table class="w-full text-left text-xs" aria-label={panel.title}>
  <thead><tr>{#if dataset.identity}<th class="p-2">Inspect</th>{/if}{#each evidence.columns as name}<th class="p-2 border-b border-surface-600 whitespace-nowrap">{dataset.fields.find((f)=>f.name===name)?.label ?? name}</th>{/each}</tr></thead>
  <tbody>{#each evidence.rows.slice(0,limit) as row}
   {@const identity=dataset.identity ? JSON.stringify(row[dataset.identity]) : null}
   <tr class={identity!==null && selection?.group===group && selection.identity===identity ? 'bg-accent/10' : ''}>
    {#if identity!==null}<td class="p-2"><button class="text-accent" aria-label={`Inspect ${display(row[dataset.identity!])}`} onclick={()=>onselect({group,identity,row,dataset:dataset.id})}>Inspect</button></td>{/if}
    {#each evidence.columns as name}<td class="p-2 border-b border-surface-700 whitespace-nowrap font-mono">{display(row[name])}</td>{/each}
   </tr>
  {/each}</tbody>
 </table>
 {#if limit<evidence.rows.length}<button class="p-2 text-xs text-accent" onclick={()=>limit+=50}>Show 50 more loaded observations</button>{/if}
</div>
