/** Display coordinates only. Statistical summaries must be published evidence. */
import { finite, integer, type Dataset, type Evidence, type Panel, type Row } from '../contracts';

export function plotModel(panel: Panel, dataset: Dataset, evidence: Evidence) {
 const e = panel.encodings;
 function axis(field: string | undefined, start: number, size: number) {
  const values = evidence.rows.flatMap((r) => field ? [r[field], ...(field===e.x && panel.kind==='intervals'?[r[e.end]]:[])] : []).filter((v) => v != null);
  const timestamp = dataset.fields.find((f) => f.name === field)?.field_type === 'timestamp_ns';
  const raw = values.map((v) => timestamp ? integer(v) : finite(v));
  const numeric = values.length > 0 && raw.every((v) => v !== null) && !(panel.kind==='heatmap'&&field===e.y);
  const categories = [...new Set(values.map(String))];
  const big = timestamp ? raw.filter((v): v is bigint => typeof v === 'bigint') : [];
  const base = big.length ? big.reduce((a,b) => a < b ? a : b) : 0n;
  const numbers = numeric ? values.map((v) => timestamp ? Number(integer(v)! - base) : finite(v)!) : [];
  const baseline = field === e.y && ['comparison','distribution'].includes(panel.kind);
  const min = numbers.length ? Math.min(...numbers, ...(baseline ? [0] : [])) : 0;
  const max = numbers.length ? Math.max(...numbers, ...(baseline ? [0] : [])) : 0;
  return {
   categories, numeric, min: timestamp ? String(base) : String(min), max: timestamp ? String(base + BigInt(Math.round(max))) : String(max),
   at(value: unknown) {
    if (value == null) return null;
    const n = numeric ? timestamp ? (integer(value) === null ? null : Number(integer(value)! - base)) : finite(value) : categories.indexOf(String(value));
    if (n == null) return null;
    return start + (numeric ? (max === min ? .5 : (n-min)/(max-min)) : (n+.5)/Math.max(1,categories.length))*size;
   }
  };
 }
 const x = axis(e.x, 54, 592); const y = axis(e.y, 250, -204);
 const values = evidence.rows.flatMap((r) => { const v = finite(r[e.value ?? e.color]); return v === null ? [] : [v]; });
 const lo = values.length ? Math.min(...values) : 0; const hi = values.length ? Math.max(...values) : 1;
 const colorField=e.value??e.color;
 const colorCategories=[...new Set(evidence.rows.flatMap(r=>r[colorField]==null?[]:[String(r[colorField])]))];
 const categoricalColor=['string','boolean'].includes(dataset.fields.find(f=>f.name===colorField)?.field_type??'')||evidence.rows.some(r=>r[colorField]!=null&&finite(r[colorField])===null);
 const categoryColor=(name:string)=>`hsl(${(colorCategories.indexOf(name)*137.5)%360} 40% 48%)`;
 const color = (r: Row) => {
  if(categoricalColor)return r[colorField]==null?'#cbd5cd':categoryColor(String(r[colorField]));
  const v = finite(r[colorField]); if (v === null) return '#cbd5cd';
  const t = hi === lo ? .5 : (v-lo)/(hi-lo); return `hsl(${28 + t*132} 35% ${82-t*42}%)`;
 };
 const nodes = [...new Set(evidence.rows.flatMap((r) => [String(r[e.source]), String(r[e.target])]))];
 const node = (name: string) => { const i = nodes.indexOf(name); const a = i / Math.max(1,nodes.length) * Math.PI * 2; return { x: 350 + 220*Math.cos(a), y: 150 + 105*Math.sin(a) }; };
 const marks = evidence.rows.map((row,index) => {
  let px = x.at(row[e.x]); let py = y.at(row[e.y]);
  if (panel.kind === 'composition') {
   const a=finite(row[e.x]); const b=finite(row[e.y]); const c=finite(row[e.z]);
   if (a===null || b===null || c===null || a<0 || b<0 || c<0 || a+b+c<=0) { px=null; py=null; }
   else { const sum=a+b+c; px=54 + 592*(b/sum+c/sum*.5); py=250-204*c/sum; }
  }
  return { row, index, x:px, y:py, end:x.at(row[e.end]), color:color(row), id: dataset.identity ? JSON.stringify(row[dataset.identity]) : null };
 });
 // Nulls split paths; published gaps must never be visually interpolated.
 const segments: string[]=[];
 const groups = new Map<string, typeof marks>();
 for (const mark of marks) { const key=e.series?JSON.stringify(mark.row[e.series]):''; const group=groups.get(key)??[];group.push(mark);groups.set(key,group); }
 for(const group of groups.values()) {
  let segment:string[]=[];
  for (const m of group) { if(m.x===null || m.y===null) { if(segment.length) segments.push(segment.join(' ')); segment=[]; } else segment.push(`${m.x},${m.y}`); }
  if(segment.length) segments.push(segment.join(' '));
 }
 const ux=[...new Set(marks.flatMap((m)=>m.x===null?[]:[m.x]))].sort((a,b)=>a-b);
 const uy=[...new Set(marks.flatMap((m)=>m.y===null?[]:[m.y]))].sort((a,b)=>a-b);
 const spacing=(v:number[],fallback:number)=>v.length<2?fallback:Math.max(2,Math.min(...v.slice(1).map((n,i)=>n-v[i])));
 return { x,y,marks,segments,nodes,node,lo,hi,categoricalColor,colorCategories,categoryColor, width:spacing(ux,32),height:spacing(uy,22) };
}
