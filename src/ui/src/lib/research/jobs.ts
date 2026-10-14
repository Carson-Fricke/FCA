/** Shared observational status ownership. This module cannot submit or replay jobs. */
import { apiBaseUrl, backendKind, getHarnessJob } from '$lib/api';
import type { HarnessJobStatus } from '$lib/types';
export interface JobObservation {job:HarnessJobStatus|null;error:string}
type Listener=(value:JobObservation)=>void;
export type JobReader=typeof getHarnessJob;
const readerIds=new WeakMap<JobReader,number>();let nextReaderId=0;
const owners=new Map<string,{listeners:Set<Listener>;value:JobObservation;timer:ReturnType<typeof setTimeout>|null;disposed:boolean}>();

export function observeJob(jobId:string,listener:Listener,reader:JobReader=getHarnessJob):()=>void {
 if(!readerIds.has(reader))readerIds.set(reader,++nextReaderId);
 const key=JSON.stringify([backendKind,apiBaseUrl,jobId,readerIds.get(reader)]);
 let owner=owners.get(key);
 if(!owner){
  owner={listeners:new Set(),value:{job:null,error:''},timer:null,disposed:false};owners.set(key,owner);
  const current=owner;
  async function refresh(){
   try{const job=await reader(jobId);if(current.disposed)return;if(job.job_id!==jobId)throw new Error('Job status returned a different job ID.');current.value={job,error:''};}
   catch(cause){if(current.disposed)return;current.value={...current.value,error:cause instanceof Error?cause.message:String(cause)};}
   for(const subscriber of current.listeners)subscriber(current.value);
   if(!current.disposed&&!['succeeded','failed','cancelled','completed'].includes(current.value.job?.state??''))current.timer=setTimeout(refresh,2000);
  }
  void refresh();
 }
 owner.listeners.add(listener);listener(owner.value);
 const current=owner;
 return()=>{current.listeners.delete(listener);if(!current.listeners.size){current.disposed=true;if(current.timer)clearTimeout(current.timer);owners.delete(key);}};
}
