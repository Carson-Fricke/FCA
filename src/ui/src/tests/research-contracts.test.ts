import {describe,it,expect} from 'vitest';
import fixture from '$lib/fixtures/research-gallery.json';
import {decodeView,decodeEvidence,decodeSemantics,validateSemanticsEvidence,validateBinding,integer} from '$lib/research/contracts';
import {researchResources} from '$lib/research/resources';
import type {ExploratoryOutputPage} from '$lib/api';
import {groupStudyObservations} from '$lib/research/grouping';
import type {ScopeEntry} from '$lib/transcript';
import type {ToolResult} from '$lib/types';

const pages=fixture.pages as Record<string,ExploratoryOutputPage>;
const reader=async (_job:string,instance?:string,name?:string,offset=0)=>name?pages[`${instance}/${name}/${offset}`]:fixture.catalog as ExploratoryOutputPage;
describe('untrusted published research evidence',()=>{
 it('strictly decodes standalone SDK semantics and rejects invalid roles and duplicate grain',()=>{
  const raw=pages['wavelets/energy_semantics/0'].output!.semantics;
  const metadata=decodeSemantics(raw);
  const page=pages['wavelets/energy/0'];const data=decodeEvidence(page.output!,page.total,true);
  validateSemanticsEvidence(metadata,data);
  expect(()=>validateSemanticsEvidence({...metadata,grain:['event_time_ns','support']},{...data,rows:[data.rows[0],{...data.rows[0],observation_id:'different'}]})).toThrow('grain');
  expect(()=>decodeSemantics({...metadata,script:'execute()'})).toThrow('Unknown');
  expect(()=>decodeSemantics({...metadata,measures:['observation_id']})).toThrow('roles');
 });

 it('decodes actual SDK-generated family contracts and preserves exact timestamps',()=>{
  for(const p of Object.values(pages))if(p.output?.kind==='research_view')expect(decodeView(p.output.view).panels.length).toBeGreaterThan(0);
  expect(integer('1783958220000000001')).toBe(1783958220000000001n);
  expect(integer(1783958220000000001)).toBeNull();
  const v=structuredClone(pages['wavelets/research_view/0'].output!.view) as Record<string,unknown>;
  v.schema_version=2;expect(()=>decodeView(v)).toThrow('Unsupported');
 });
 it('rejects dangling fields, duplicate identities and invalid causal timing',()=>{
  const v=decodeView(pages['wavelets/research_view/0'].output!.view);
  const d=v.datasets.find(d=>d.id==='energy')!;
  const p=pages['wavelets/energy/0'];const e=decodeEvidence(p.output!,p.total,false);
  validateBinding(d,e);
  expect(()=>validateBinding({...d,fields:[{name:'missing',label:'Missing',description:'Absent test field.',field_type:'number',unit:null}]},e)).toThrow('lacks field');
  expect(()=>validateBinding(d,{...e,rows:[e.rows[0],e.rows[0]]})).toThrow('unique');
  const future={...e.rows[0],available_at_ns:'-1'};expect(()=>validateBinding(d,{...e,rows:[future]})).toThrow('causal');
  const invalid=structuredClone(v) as unknown as Record<string,unknown>;invalid.script='execute()';expect(()=>decodeView(invalid)).toThrow('Unknown');
 });
 it('reads every page and rejects cross-job and changing-artifact evidence',async()=>{
  const resources=researchResources('sdk-research-demonstration',reader);
  const data=await resources.evidence('wavelets','energy',new AbortController().signal);
  expect(data.rows.length).toBe(data.total);expect(data.partial).toBe(false);
  const mismatch=researchResources('another-job',reader);
  await expect(mismatch.catalog(new AbortController().signal)).rejects.toThrow('identity mismatch');
  let n=0;const changing=researchResources('sdk-research-demonstration',async(...args)=>({...await reader(...args),artifactSha256:(n++?'b':'a').repeat(64)}));
  await expect(changing.evidence('wavelets','energy',new AbortController().signal)).rejects.toThrow('changed');
 });
 it('retains unknown panel extensions without treating their content as executable code',()=>{
  const v=structuredClone(pages['wavelets/research_view/0'].output!.view) as {panels:{kind:string}[]};v.panels[0].kind='custom/topology/v1';expect(decodeView(v).panels[0].kind).toBe('custom/topology/v1');
 });
 it('bounds long evidence loads and stops cancelled inspections before another request',async()=>{
  let calls=0;
  const bounded=researchResources('sdk-research-demonstration',async(_job,instance,name,offset=0)=>{
   calls++;return {job_id:'sdk-research-demonstration',artifactName:'exploratory_outputs.json',artifactSha256:'a'.repeat(64),instanceId:instance,name,total:5100,nextOffset:offset+100<5100?offset+100:null,output:{kind:'table',columns:['id','value'],rows:Array.from({length:100},(_,i)=>[String(offset+i),i])}};
  });
  const data=await bounded.evidence('study','observations',new AbortController().signal);
  expect(data.partial).toBe(true);expect(data.rows).toHaveLength(5000);expect(calls).toBe(50);
  const controller=new AbortController();controller.abort();
  await expect(bounded.evidence('study','observations',controller.signal)).rejects.toThrow('Cancelled');expect(calls).toBe(50);
  const wrong=researchResources('sdk-research-demonstration',async(...args)=>({...await reader(...args),name:'another-output'}));
  await expect(wrong.exhibit('wavelets','research_view',new AbortController().signal)).rejects.toThrow('Output identity mismatch');
 });
 it('retains canonical receipts while folding observations and comparing displayed revisions',()=>{
  const entry=(id:string,tool:string,structured:Record<string,unknown>):ScopeEntry=>({kind:'tool',id,at:id,order:Number(id),result:{ok:true,tool,structured} as ToolResult});
  const source=[entry('1','run_study',{job_id:'one'}),entry('2','get_job_status',{job_id:'one',state:'running'}),entry('3','run_backtest',{job_id:'two'}),entry('4','author_strategy_sketch',{reference:{sketchId:'idea',version:'a'},sketch:{entry:'A'}}),entry('5','update_strategy_sketch',{reference:{sketchId:'idea',version:'b'},sketch:{entry:'B'}})];
  const original=JSON.stringify(source);const grouped=groupStudyObservations(source);
  expect(grouped.map(e=>e.id)).toEqual(['1','3','4','5']);
  expect(grouped[0].kind==='tool'&&grouped[0].result.observations?.[0].structured?.state).toBe('running');
  expect(grouped[3].kind==='tool'&&grouped[3].result.previousSketch).toEqual({entry:'A'});
  expect(JSON.stringify(source)).toBe(original);
  expect(groupStudyObservations([source[4]])[0]).toEqual(source[4]);
 });
});
