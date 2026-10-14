/** Wire contract for SDK research views. No execution or calculated research claims. */
export type Cell = string | number | boolean | null;
export type Row = Record<string, Cell>;
export interface Field { name: string; label: string; description: string; field_type: 'number' | 'string' | 'boolean' | 'timestamp_ns'; unit: string | null }
export interface Dataset { id: string; output: string; fields: Field[]; identity: string | null; selection_group: string | null; event_time: string | null; available_at: string | null }
export interface Panel { id: string; kind: string; title: string; dataset: string; encodings: Record<string, string>; description: string | null }
export interface ResearchView { schema_version: 1; title: string; description: string; limitations: string[]; datasets: Dataset[]; panels: Panel[] }
export interface Evidence { output: Record<string, unknown>; rows: Row[]; columns: string[]; total: number; partial: boolean }
export interface Exhibit { instanceId: string; name: string; view: ResearchView }
export interface Selection { group: string; identity: string; row: Row; dataset: string }

const record = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0;
function require(condition: unknown, message: string): asserts condition { if (!condition) throw new Error(message); }
function keys(value: Record<string, unknown>, allowed: string[]) { require(Object.keys(value).every((k) => allowed.includes(k)), 'Unknown research-view property'); }

/** Strict saved-view decoder. Unknown panel kinds remain inspectable as extensions. */
export function decodeView(value: unknown): ResearchView {
 require(record(value), 'Research view must be an object');
 keys(value, ['schema_version', 'title', 'description', 'limitations', 'datasets', 'panels']);
 require(value.schema_version === 1, 'Unsupported research-view version');
 require(text(value.title) && typeof value.description === 'string', 'Research view requires a title and description');
 require(Array.isArray(value.limitations) && value.limitations.every((s) => typeof s === 'string'), 'Invalid limitations');
 require(Array.isArray(value.datasets) && value.datasets.length > 0 && value.datasets.length <= 32, 'Invalid dataset count');
 require(Array.isArray(value.panels) && value.panels.length > 0 && value.panels.length <= 24, 'Invalid panel count');
 const datasets = new Map<string, Set<string>>();
 for (const raw of value.datasets) {
  const d=decodeDataset(raw);
  require(!datasets.has(d.id), 'Duplicate dataset identity');
  datasets.set(d.id, new Set(d.fields.map(f=>f.name)));
 }
 const ids = new Set<string>();
 for (const p of value.panels) {
  require(record(p), 'Invalid panel'); keys(p, ['id', 'kind', 'title', 'dataset', 'encodings', 'description']);
  require(text(p.id) && !ids.has(p.id) && text(p.kind) && text(p.title) && text(p.dataset) && datasets.has(p.dataset) && record(p.encodings) && (p.description == null || typeof p.description === 'string'), 'Invalid panel descriptor'); ids.add(p.id);
  require(Object.values(p.encodings).every((f) => text(f) && datasets.get(p.dataset as string)!.has(f)), 'Unknown encoding field');
 }
 return value as unknown as ResearchView;
}

/** Preserve canonical cells, including exact timestamp strings and missing states. */
export function decodeEvidence(output: Record<string, unknown>, total: number, partial: boolean): Evidence {
 let columns: string[] = []; let rows: Row[] = [];
 const scalar = (v: unknown): v is Cell => v === null || typeof v === 'string' || typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v));
 if (output.kind === 'table') {
  require(Array.isArray(output.columns) && output.columns.every(text), 'Invalid evidence columns'); columns = output.columns as string[];
  require(new Set(columns).size === columns.length && Array.isArray(output.rows), 'Invalid evidence table');
  rows = output.rows.map((r) => { require(Array.isArray(r) && r.length === columns.length && r.every(scalar), 'Invalid evidence row'); return Object.fromEntries(columns.map((c, i) => [c, r[i]])); });
 } else if (output.kind === 'series' || output.kind === 'events') {
  const values = output.kind === 'series' ? output.points : output.events;
  require(Array.isArray(values), 'Invalid evidence records');
  rows = values.map((r) => {
   require(record(r), 'Invalid evidence record');
   const fields = output.kind === 'events' && record(r.fields) ? r.fields : {};
   const source = { ...fields, ...r }; delete source.fields;
   return Object.fromEntries(Object.entries(source).filter(([,v]) => scalar(v))) as Row;
  });
  columns = [...new Set([...(output.kind==='series'?['symbol','interval','event_time_ns','available_at_ns','value','undefined_reason']:['symbol','event_time_ns','available_at_ns']),...rows.flatMap((r) => Object.keys(r))])];
 }
 return { output, rows, columns, total, partial };
}

export function validateBinding(dataset: Dataset, evidence: Evidence): void {
 for (const field of dataset.fields) {
  require(evidence.columns.includes(field.name), `Output lacks field ${field.name}`);
  for (const row of evidence.rows) {
   const v = row[field.name]; if (v == null) continue;
   require(field.field_type === 'number' ? finite(v) !== null : field.field_type === 'timestamp_ns' ? integer(v) !== null : typeof v === field.field_type, `Invalid value for ${field.name}`);
  }
 }
 if (dataset.identity) {
  const values = evidence.rows.map((r) => r[dataset.identity!]);
  require(values.every((v) => v != null) && new Set(values.map((v) => JSON.stringify(v))).size === values.length, 'Dataset identities must be nonnull and unique');
 }
 if (dataset.event_time && dataset.available_at) require(evidence.rows.every((r) => {
  const e = integer(r[dataset.event_time!]); const a = integer(r[dataset.available_at!]); return e !== null && a !== null && e <= a;
 }), 'Evidence has invalid causal timing');
}

export function finite(value: unknown): number | null { if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return null; const n = Number(value); return Number.isFinite(n) ? n : null; }
export function integer(value: unknown): bigint | null { try { return typeof value === 'string' && /^-?\d+$/.test(value) ? BigInt(value) : typeof value === 'number' && Number.isSafeInteger(value) ? BigInt(value) : null; } catch { return null; } }
export function display(value: unknown): string { return value == null ? 'Unavailable' : typeof value === 'number' ? value.toLocaleString(undefined, { maximumSignificantDigits: 6 }) : String(value); }

/** Standalone field binding shared by exhibits and semantic output descriptions. */
export function decodeDataset(value: unknown): Dataset {
 require(record(value), 'Invalid dataset');
 keys(value,['id','output','fields','identity','selection_group','event_time','available_at']);
 require(text(value.id) && text(value.output) && Array.isArray(value.fields) && value.fields.length>0,'Missing dataset identity or fields');
 const fields=new Set<string>();
 for(const f of value.fields) {
  require(record(f),'Invalid field'); keys(f,['name','label','description','field_type','unit']);
  require(text(f.name) && text(f.label) && text(f.description) && !fields.has(f.name) && ['number','string','boolean','timestamp_ns'].includes(String(f.field_type)) && (f.unit==null || typeof f.unit==='string'),'Invalid field descriptor: fields require scientific descriptions'); fields.add(f.name);
 }
 for(const key of ['identity','event_time','available_at']) require(value[key]==null || (text(value[key]) && fields.has(value[key])),`Unknown ${key} field`);
 require(value.selection_group==null || (text(value.selection_group) && text(value.identity)),'Linked selections require explicit identity');
 return value as unknown as Dataset;
}
export interface DatasetSemantics { schema_version:1; dataset:Dataset; row_meaning:string; grain:string[]; dimensions:string[]; measures:string[]; ordered:string[]; preferred_views:Panel[] }
/** Publisher-supplied meaning. Numeric-looking metadata never supplies field roles. */
export function decodeSemantics(value:unknown):DatasetSemantics {
 require(record(value),'Invalid dataset semantics');keys(value,['schema_version','dataset','row_meaning','grain','dimensions','measures','ordered','preferred_views']);
 require(value.schema_version===1 && text(value.row_meaning),'Dataset semantics require schema 1 and row meaning');
 const dataset=decodeDataset(value.dataset);const fields=new Map(dataset.fields.map(f=>[f.name,f]));
 for(const role of ['grain','dimensions','measures','ordered']) {
  const names=value[role]; require(Array.isArray(names) && names.every(n=>text(n)&&fields.has(n)) && new Set(names).size===names.length,`Invalid ${role}`);
 }
 const v=value as unknown as DatasetSemantics;
 require(v.grain.length>0,'Dataset semantics require row grain');
 require(v.measures.every(n=>fields.get(n)?.field_type==='number' && !v.dimensions.includes(n) && dataset.identity!==n) && v.dimensions.every(n=>dataset.identity!==n),'Incompatible field roles');
 require(v.ordered.every(n=>v.dimensions.includes(n) && ['number','timestamp_ns'].includes(fields.get(n)?.field_type??'')),'Ordered fields require numeric or timestamp dimensions');
 require(Array.isArray(v.preferred_views) && v.preferred_views.length<=24,'Invalid preferred views');
 const ids=new Set<string>();
 for(const p of v.preferred_views) {
  require(record(p),'Invalid preferred view');keys(p,['id','kind','title','dataset','encodings','description']);
  require(text(p.id) && !ids.has(p.id) && text(p.title) && text(p.kind) && p.dataset===dataset.id && record(p.encodings) && Object.values(p.encodings).every(f=>text(f)&&fields.has(f)) && (p.description==null||typeof p.description==='string'),'Invalid preferred view binding'); ids.add(p.id);
 }
 return {...v,dataset};
}
/** Checks loaded evidence only; a partial preview cannot establish global uniqueness. */
export function validateSemanticsEvidence(semantics:DatasetSemantics,evidence:Evidence):void {
 validateBinding(semantics.dataset,evidence);const seen=new Set<string>();
 for(const row of evidence.rows){const values=semantics.grain.map(name=>{const field=semantics.dataset.fields.find(f=>f.name===name);const value=row[name];require(value!=null,'Dataset grain must be nonnull');return field?.field_type==='number'?finite(value):field?.field_type==='timestamp_ns'?String(integer(value)):value;});const key=JSON.stringify(values);require(!seen.has(key),'Dataset grain must be unique within loaded evidence');seen.add(key);}
}
