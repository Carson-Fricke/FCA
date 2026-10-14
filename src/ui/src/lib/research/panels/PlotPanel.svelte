<script lang="ts">
 import type { PanelProps } from '../registry';
 import { display } from '../contracts';
 import { plotModel } from './plot';
 let { panel, dataset, evidence, selection, onselect, cursor, oncursor }: PanelProps = $props();
 const model = $derived(plotModel(panel,dataset,evidence));
 const field = (channel: string) => dataset.fields.find((f) => f.name === panel.encodings[channel]);
 const label = (channel: string) => { const f=field(channel); return f ? `${f.label}${f.unit ? ` · ${f.unit}` : ''}` : panel.encodings[channel] ?? ''; };
 function select(mark: typeof model.marks[number]) {
  if (!dataset.identity || mark.id === null) return;
  const group=dataset.selection_group ?? dataset.id;
  onselect(selection?.group===group && selection.identity===mark.id ? null : {group,identity:mark.id,row:mark.row,dataset:dataset.id});
 }
 const selected = (id:string|null) => id!==null && selection?.identity===id && selection.group===(dataset.selection_group ?? dataset.id);
 const title = (row:typeof evidence.rows[number]) => Object.entries(panel.encodings).map(([channel,f])=>`${label(channel)}: ${display(row[f])}`).join(' · ');
 const tick=(value:string,numeric:boolean)=>numeric&&field('y')?.field_type!=='timestamp_ns'?display(Number(value)):value;
</script>

<div class="plot-wrap">
 <svg viewBox="0 0 700 305" role="group" aria-label={panel.title} data-testid="research-plot">
  <title>{panel.title}</title>
  {#if panel.kind === 'graph'}
   {#each model.marks as mark}
    {@const row=mark.row}
    {@const a=model.node(String(row[panel.encodings.source]))}{@const b=model.node(String(row[panel.encodings.target]))}
    {#snippet edge()}<line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="var(--color-accent, #2a8761)" stroke-width={selected(mark.id)?6:3} opacity={selected(mark.id)?1:.4}><title>{title(row)}</title></line>{/snippet}
    {#if dataset.identity}<g role="button" tabindex="0" aria-label={title(row)} aria-pressed={selected(mark.id)} onclick={()=>select(mark)} onkeydown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();select(mark);}}}>{@render edge()}</g>{:else}{@render edge()}{/if}
   {/each}
   {#each model.nodes as name}
    {@const p=model.node(name)}<circle cx={p.x} cy={p.y} r="7" fill="var(--color-accent, #2a8761)"/><text x={p.x} y={p.y-12} text-anchor="middle">{name}</text>
   {/each}
  {:else}
   {#if panel.kind!=='composition'}<path d="M54 42V250H650" fill="none" stroke="#c8d2c9"/>{/if}
   {#if panel.kind === 'composition'}<path d="M54 250L350 46L646 250Z" fill="none" stroke="#c8d2c9"/><text x="350" y="28" text-anchor="middle">{label('z')}</text>{/if}
   {#if panel.kind === 'line'}{#each model.segments as points}<polyline {points} fill="none" stroke="var(--color-accent, #2a8761)" stroke-width="2"/>{/each}{/if}
   {#each model.marks as mark}
    {#if mark.x !== null && mark.y !== null}
     {@const x=mark.x}{@const y=mark.y}
     {#snippet shape()}
      <title>{title(mark.row)}</title>
      {#if panel.kind === 'heatmap'}
       <rect x={x-model.width*.48} y={y-model.height*.48} width={model.width*.96} height={model.height*.96} rx="2" fill={mark.color} stroke={selected(mark.id)?'#102e20':'transparent'} stroke-width="2"/>
      {:else if panel.kind === 'comparison' || panel.kind === 'distribution'}
       {@const zero=model.y.at(0) ?? 250}
       <rect x={x-model.width*.36} y={Math.min(zero,y)} width={model.width*.72} height={Math.max(1,Math.abs(zero-y))} rx="3" fill="var(--color-accent, #2a8761)" opacity={selected(mark.id)?1:.7}/>
      {:else if panel.kind === 'intervals' && mark.end !== null}
       <rect x={Math.min(x,mark.end)} y={y-5} width={Math.max(2,Math.abs(mark.end-x))} height="10" rx="5" fill={mark.color}/>
      {:else}
       <circle cx={x} cy={y} r={selected(mark.id)?6:panel.kind==='line'?2.5:4} fill={panel.encodings.color ? mark.color : 'var(--color-accent, #2a8761)'} stroke={selected(mark.id)?'#102e20':'white'} stroke-width="1" opacity=".85"/>
      {/if}
     {/snippet}
     {#if dataset.identity}
      <g role="button" tabindex="0" aria-label={title(mark.row)} aria-pressed={selected(mark.id)} onclick={()=>select(mark)} onkeydown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();select(mark);}}} onpointerenter={()=>oncursor(dataset.event_time?String(mark.row[dataset.event_time]):null)} onpointerleave={()=>oncursor(null)}>{@render shape()}</g>
     {:else}
      <g role="img" aria-label={title(mark.row)} onpointerenter={()=>oncursor(dataset.event_time?String(mark.row[dataset.event_time]):null)} onpointerleave={()=>oncursor(null)}>{@render shape()}</g>
     {/if}
    {/if}
   {/each}
   {#if cursor !== null && dataset.event_time === panel.encodings.x && model.x.at(cursor)!==null}
    <line x1={model.x.at(cursor)!} x2={model.x.at(cursor)!} y1="40" y2="250" stroke="#9b7b33" stroke-dasharray="4 4"/>
   {/if}
   {#if panel.kind==='composition'}
    <text x="54" y="270">{label('x')}</text><text x="646" y="270" text-anchor="end">{label('y')}</text>
   {:else}
    <text x="350" y="296" text-anchor="middle">{label('x')}</text><text transform="translate(15 150) rotate(-90)" text-anchor="middle">{label('y')}</text>
    <text x="54" y="270">{model.x.numeric ? model.x.min : model.x.categories[0] ?? ''}</text><text x="646" y="270" text-anchor="end">{model.x.numeric ? model.x.max : model.x.categories.at(-1) ?? ''}</text>
    {#if !model.y.numeric && model.y.categories.length<=12}{#each model.y.categories as category}<text x="48" y={model.y.at(category)!+3} text-anchor="end">{category}</text>{/each}
    {:else}<text x="48" y="48" text-anchor="end">{tick(model.y.max,model.y.numeric)}</text><text x="48" y="250" text-anchor="end">{tick(model.y.min,model.y.numeric)}</text>{/if}
   {/if}
  {/if}
 </svg>
 {#if panel.kind==='heatmap' || panel.encodings.color}<div class="legend"><span>{label(panel.kind==='heatmap'?'value':'color')}</span>{#if model.categoricalColor}{#each model.colorCategories.slice(0,12) as category}<span><i style:background={model.categoryColor(category)}></i>{category}</span>{/each}{#if model.colorCategories.length>12}<span>+{model.colorCategories.length-12} categories</span>{/if}{:else}<span class="gradient"></span><span>{display(model.lo)} → {display(model.hi)}</span>{/if}</div>{/if}
 <p class="note">{evidence.rows.length.toLocaleString()} published observations · {dataset.identity ? 'Select an observation to inspect its evidence.' : 'No linked identity declared.'}</p>
</div>

<style>
 .plot-wrap{overflow:auto}svg{width:100%;min-width:380px;display:block}text{font:10px Inter, sans-serif;fill:#637369}g[role=button]{cursor:pointer}g[role=button]:focus{outline:none}g[role=button]:focus circle,g[role=button]:focus rect{stroke:#102e20;stroke-width:3}.legend{display:flex;align-items:center;gap:10px;font-size:11px;color:#637369}.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}.gradient{width:90px;height:8px;border-radius:8px;background:linear-gradient(90deg,hsl(28 35% 82%),hsl(160 35% 40%))}.note{font-size:11px;color:#718077;margin-top:10px}
</style>
