/** Explicit exploratory display transforms. Source evidence is never changed. */
import { finite, integer, type Evidence, type Panel, type Row } from './contracts';
import type { SemanticOutput } from './semantics';
export type Reduction='none'|'mean'|'sum'|'min'|'max'|'count';
export function transform(output:SemanticOutput,panel:Panel,evidence:Evidence,bins:Record<string,number>,reduction:Reduction) {
 const rows=evidence.rows.map(row=>({...row}));
 const fields=output.dataset.fields.map(f=>({...f}));
 const mapped={...panel,encodings:{...panel.encodings}};let problem='';
 for(const channel of ['x','y']) {
  const source=panel.encodings[channel],count=bins[channel]??0;
  if(!source||!count)continue;
  const descriptor=output.dataset.fields.find(f=>f.name===source);
  const alias=`__bucket_${channel}`;
  const extent=output.profiles?.find(f=>f.name===source)?.range;
  if(descriptor?.field_type==='timestamp_ns'){
   const values=output.evidence.rows.map(r=>integer(r[source])).filter((v):v is bigint=>v!==null);
   if(!values.length){problem='No valid timestamps are available for bucketing.';continue;}
   const low=extent?BigInt(extent[0]):values.reduce((a,b)=>a<b?a:b),high=extent?BigInt(extent[1]):values.reduce((a,b)=>a>b?a:b);
   for(const row of rows){const value=integer(row[source]);if(value===null){row[alias]=null;continue;}const index=high===low?0n:((value-low)*BigInt(count)/(high-low));const i=index>=BigInt(count)?BigInt(count-1):index;row[alias]=`${low+(high-low)*i/BigInt(count)}…${low+(high-low)*(i+1n)/BigInt(count)}`;}
  }else{
   const values=output.evidence.rows.map(r=>finite(r[source])).filter((v):v is number=>v!==null);
   if(!values.length){problem='Bucketing requires a numeric or timestamp field. Choose exact values for categories.';continue;}
   const low=Number(extent?.[0]??Math.min(...values)),high=Number(extent?.[1]??Math.max(...values));
   const at=(fraction:number)=>(1-fraction)*low+fraction*high;
   for(const row of rows){const value=finite(row[source]);if(value===null){row[alias]=null;continue;}const index=high===low?0:Math.max(0,Math.min(count-1,Math.floor(((value/2-low/2)/(high/2-low/2))*count)));row[alias]=`${at(index/count).toPrecision(5)}…${at((index+1)/count).toPrecision(5)}`;}
  }
  mapped.encodings[channel]=alias;
  fields.push({name:alias,label:`${descriptor?.label??source} · ${count} buckets`,description:`Explicit display buckets of ${source}.`,field_type:'string',unit:descriptor?.unit??null});
 }
 let plotted=rows;
 if(panel.kind==='heatmap'&&reduction!=='none'){
  const x=mapped.encodings.x,y=mapped.encodings.y,source=panel.encodings.value;
  const groups=new Map<string,Row[]>();
  for(const row of rows){if(row[x]==null||row[y]==null)continue;const key=JSON.stringify([row[x],row[y]]);const group=groups.get(key)??[];group.push(row);groups.set(key,group);}
  plotted=[...groups.values()].map(group=>{const values=group.map(r=>finite(r[source])).filter((v):v is number=>v!==null);const sum=values.reduce((a,b)=>a+b,0);const value=reduction==='count'?group.length:!values.length?null:reduction==='mean'?sum/values.length:reduction==='sum'?sum:reduction==='min'?Math.min(...values):Math.max(...values);return {[x]:group[0][x],[y]:group[0][y],__cell_value:value};});
  mapped.encodings.value='__cell_value';
  fields.push({name:'__cell_value',label:`${reduction} · ${source}`,description:`Explicit ${reduction} of matching loaded observations.`,field_type:'number',unit:reduction==='count'?'observations':output.dataset.fields.find(f=>f.name===source)?.unit??null});
 }
 const changed=Object.values(bins).some(Boolean)||reduction!=='none';
 return {panel:mapped,problem,output:{...output,dataset:{...output.dataset,fields,identity:changed?null:output.dataset.identity,selection_group:changed?null:output.dataset.selection_group}},evidence:{...evidence,rows:plotted}};
}
