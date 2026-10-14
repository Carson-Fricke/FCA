<script lang="ts">
 import ScopeTranscript from '$lib/components/chat/ScopeTranscript.svelte';
 import ChatTranscript from '$lib/components/chat/ChatTranscript.svelte';
 import type { ScopeEntry } from '$lib/transcript';
 import type { ToolResult, ChatMessage } from '$lib/types';
 let rootChat=$state(false);
 const sketch={
  title:'Intraday scale reversion',premise:'Investigate whether unusually concentrated short-scale energy is followed by price reversion within the same session.',
  mechanism:'Short-lived liquidity shocks can displace price before inventory and order flow normalize. This is a hypothesis, not an established trading edge.',
  profitSource:'Reversion following temporary liquidity demand, after spread and fees.',conditioning:'Only consider liquid sessions with concentrated short-scale energy and no scheduled announcement.',
  entry:'Enter against the displacement after an explicitly confirmed signal.',exit:'Exit on reversion to the pre-event level or after the declared holding horizon.',abstention:'Skip wide spreads, scheduled events, and unavailable observations.',
  representations:[{nodeId:'scale-energy',inputs:['published bars'],description:'Wavelet energy by declared support.',informationTime:'After the final input bar closes.'}],
  assumptions:[{assumptionId:'temporary-shock',status:'untested',claim:'The displacement is temporary.',investigation:'Compare event-aligned paths with matched controls.',evidence:[]},{assumptionId:'execution-cost',status:'untested',claim:'Reversion exceeds spread, slippage, and fees.',investigation:'Measure costs around eligible entries.',evidence:[]},{assumptionId:'causal-timing',status:'untested',claim:'All inputs are available before entry.',investigation:'Audit event and availability timestamps.',evidence:[]}],
  alternatives:['Persistent information arrival rather than a liquidity shock.'],limitations:'Synthetic design example. No market evidence has been collected.'
 };
 const base:ToolResult={datumType:'tool_result',callId:null,tool:null,ok:true,failure:null,structured:{},summary:null,text:''};
 const entries:ScopeEntry[]=[
  {kind:'assistant',id:'intro',at:'',order:0,actorId:'Research agent',text:'I’ll read the research reference, validate the study, and update the strategy sketch.'},
  {kind:'tool',id:'reference',at:'',order:1,result:{...base,callId:'demo-reference',tool:'read_knowledge',datumType:'read_knowledge',operation:'read',durationMs:320,summary:'Read wavelet evidence reference',structured:{title:'Wavelet research reference',content:'Use explicit event and availability timestamps. Publish observations and a research view.',reference:'demo:wavelet-reference'}}},
  {kind:'tool',id:'validation',at:'',order:2,result:{...base,tool:'validate_study',datumType:'validate_study',durationMs:410,summary:'Study validation completed',structured:{accepted:true,programId:'intraday-wavelets',diagnostics:[]}}},
  {kind:'tool',id:'sketch',at:'',order:3,result:{...base,callId:'demo-sketch',tool:'read_strategy_sketch',datumType:'read_strategy_sketch',operation:'read',durationMs:82,summary:'Read strategy sketch',structured:{reference:{sketchId:'intraday-scale-reversion',version:'demo-revision-b'},sketch},previousSketch:{...sketch,exit:'Exit after a fixed holding horizon.'}}},
  {kind:'assistant',id:'followup',at:'',order:4,actorId:'Research agent',text:'Let me check the outputs and artifacts again.'},
  {kind:'tool',id:'failure',at:'',order:5,result:{...base,tool:'read_knowledge',datumType:'read_knowledge',operation:'read',ok:false,summary:'Reference could not be read',failure:{kind:'not_found',message:'The requested reference does not exist.'}}},
  {kind:'tool',id:'artifacts',at:'',order:6,result:{...base,tool:'list_artifacts',datumType:'list_artifacts',operation:'read',summary:'Listed output artifacts',structured:{artifacts:[{name:'metrics.json'},{name:'observations.parquet'},{name:'research_view.json'}]}}},
  {kind:'tool',id:'sdk',at:'',order:7,result:{...base,tool:'read_sdk_reference',datumType:'read_sdk_reference',operation:'read',arguments:{topic:'consumer'},summary:'Read consumer SDK',structured:{content:'pub trait MarketConsumer { /* serving SDK excerpt */ }',truncated:true,sdkIdentity:'demo-sdk'}}},
  {kind:'tool',id:'future-read',at:'',order:8,result:{...base,tool:'inspect_new_source',datumType:'inspect_new_source',operation:'read',summary:'Read an additional data source',text:'Full returned content from a newly registered read tool.',structured:{count:999}}},
  {kind:'tool',id:'search',at:'',order:9,result:{...base,tool:'search_knowledge',datumType:'search_knowledge',operation:'read',arguments:{query:'intraday seasonality'},structured:{results:[{title:'Session effects',excerpt:'First returned search excerpt.'},{title:'Time of day',excerpt:'Second returned search excerpt.'}]}}},
  {kind:'tool',id:'sketch-list',at:'',order:10,result:{...base,tool:'list_strategy_sketches',datumType:'list_strategy_sketches',operation:'read',structured:{items:[{title:'Alpha'},{title:'Beta'}],total:27,nextOffset:2}}},
  {kind:'tool',id:'empty-list',at:'',order:11,result:{...base,tool:'list_studies',datumType:'list_studies',operation:'read',structured:{programs:[],count:0}}}
 ];
 const messages:ChatMessage[]=entries.map(entry=>({message_id:entry.id,chat_id:'demo',conversation_id:'demo',root_conversation_id:'demo',parent_conversation_id:null,workspace_id:'',actor_id:'Research agent',role:entry.kind==='tool'?'tool':'assistant',body:entry.kind==='assistant'?entry.text:'',state:'accepted',attachment_ids:[],created_at:`2026-10-10T12:00:${String(entry.order).padStart(2,'0')}Z`,updated_at:null,execution_id:null,intent_epoch:0,kind:entry.kind==='tool'?'tool':'message',data:{},tool_result:entry.kind==='tool'?entry.result:null}));
</script>
<svelte:head><title>Chat activity rail — Thesis</title></svelte:head>
<main class="mx-auto max-w-5xl p-6 space-y-6">
 <header><p class="text-xs text-accent">Chat presentation</p><h1 class="mt-2 text-xl font-semibold">Activity rail &amp; compact strategy sketch</h1><p class="mt-2 text-sm text-slate-400">Synthetic examples rendered by the actual chat transcript and tool cards.</p></header>
 <nav aria-label="Transcript demonstration"><button class="text-xs text-accent mr-4" onclick={()=>rootChat=false}>Scoped chat</button><button class="text-xs text-accent" onclick={()=>rootChat=true}>Root chat</button></nav>
 {#if rootChat}<ChatTranscript {messages} workspaceId=""/>{:else}<ScopeTranscript {entries} workspaceId=""/>{/if}
</main>
