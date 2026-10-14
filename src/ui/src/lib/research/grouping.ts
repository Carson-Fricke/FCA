/** Pure presentation folding; canonical receipts and arrival positions are retained. */
import type { ScopeEntry } from '$lib/transcript';
import type { ToolResult } from '$lib/types';

export function groupStudyObservations(entries:ScopeEntry[]):ScopeEntry[]{
 const groups=new Map<string,{index:number;results:ToolResult[]}>();const output:ScopeEntry[]=[];
 const sketches=new Map<string,{version:string;sketch:Record<string,unknown>}>();
 for(let entry of entries){
  let result=entry.kind==='tool'?entry.result:entry.kind==='message'?entry.message.tool_result:null;
  const revision=result?.structured;
  if(result && revision && ['author_strategy_sketch','update_strategy_sketch','read_strategy_sketch'].includes(result.tool??'')){
   const ref=revision.reference as Record<string,unknown>|undefined;const sketch=revision.sketch;
   if(ref&&typeof ref.sketchId==='string'&&typeof ref.version==='string'&&sketch&&typeof sketch==='object'&&!Array.isArray(sketch)){
    const previous=sketches.get(ref.sketchId);
    if(previous&&previous.version!==ref.version){result={...result,previousSketch:previous.sketch};entry=entry.kind==='tool'?{...entry,result}:entry.kind==='message'?{...entry,message:{...entry.message,tool_result:result}}:entry;}
    sketches.set(ref.sketchId,{version:ref.version,sketch:sketch as Record<string,unknown>});
   }
  }
  const data=result?.structured;
  const job=data&&typeof data.job_id==='string'?data.job_id:null;
  const tool=result?.tool;
  const study=result&&job&&(['run_study'].includes(tool??'')||(['get_job_status','get_job_result'].includes(tool??'')&&(data?.kind==='study'||groups.has(job))));
  if(!study||!result||!job){output.push(entry);continue;}
  const group=groups.get(job);
  if(!group){groups.set(job,{index:output.length,results:[result]});output.push(entry);continue;}
  group.results.push(result);
  const original=output[group.index];
  const primary=group.results[0];const folded={...primary,observations:group.results.slice(1)};
  output[group.index]=original.kind==='tool'?{...original,result:folded}:original.kind==='message'?{...original,message:{...original.message,tool_result:folded}}:original;
 }
 return output;
}
