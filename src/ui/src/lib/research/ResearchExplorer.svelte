<script lang="ts">
	import type { Snippet } from 'svelte';
	import { apiBaseUrl, getStudyRecommendations } from '$lib/api';
	import type { StudyRecommendations } from './recommendations';
	import { researchResources, type PageReader } from './resources';
	import { validateBinding, validateSemanticsEvidence, type DatasetSemantics, type Evidence, type Exhibit, type Selection } from './contracts';
	import { describeOutput, type SemanticOutput } from './semantics';
	import EvidencePanel from './EvidencePanel.svelte';
	import ReturnedData from './ReturnedData.svelte';
	let { jobId, workspaceId='', reader, recommendationReader, initialInstance, initialOutput, exportUrl, returnedData, awaiting=false, embedded=false, extraOutputs=[] }: {
		jobId:string;workspaceId?:string;reader?:PageReader;initialInstance?:string;initialOutput?:string;exportUrl?:string;returnedData?:Snippet;
		awaiting?:boolean;embedded?:boolean;extraOutputs?:SemanticOutput[];recommendationReader?:(job:string)=>Promise<StudyRecommendations>;
	}=$props();
	type Output={instanceId:string;name:string;kind:string;count:number|null};
	let catalog=$state<Output[]>([]);
	let sha=$state('');let error=$state('');let loading=$state(true);let busy=$state(false);let full=$state(false);
	let exhibit=$state<Exhibit|null>(null);
	let metadata=$state<Record<string,DatasetSemantics>>({});
	let recommendations=$state<StudyRecommendations|null>(null);let recommending=$state(false);let recommendationError=$state('');let navigated=false;
	let evidence=$state<Record<string,Evidence>>({});
	let selection=$state<Selection|null>(null);let cursor=$state<string|null>(null);
	let tab=$state('returned-data');let rawKey=$state('');let rawQuery=$state('');
	let viewOutput=$state<{instance:string;name:string}|null>(null);
	let reload=$state(0);let inspection:AbortController|undefined;
	const id=$props.id();
	const key=(instance:string,name:string)=>JSON.stringify([instance,name]);
	const resources=$derived(researchResources(jobId,reader));
	const views=$derived(catalog.filter(o=>o.kind==='research_view'));
	const allPanels=$derived([...(exhibit?.view.panels??[]),...(recommendations?.datasets.flatMap(d=>d.panels)??[]).slice(0,6)]);
	const panel=$derived(allPanels.find(p=>`panel:${p.id}`===tab));
	const panelSource=$derived(recommendations?.datasets.find(d=>d.panels.some(p=>p.id===panel?.id)));
	const panelInstance=$derived(panelSource?.instanceId??viewOutput?.instance);
	const binding=$derived(panelSource?.dataset??exhibit?.view.datasets.find(d=>d.id===panel?.dataset));
	const panelEvidence=$derived(binding&&panelInstance?evidence[key(panelInstance,binding.output)]:undefined);
	const panelOutput=$derived(binding&&panelEvidence&&panelInstance?describeOutput(panelEvidence,binding,exhibit?.view.panels.filter(p=>p.dataset===binding.id)??[],metadata[key(panelInstance,binding.output)],panelSource):null);
	const rawEntry=$derived(catalog.find(o=>key(o.instanceId,o.name)===rawKey));
	const rawEvidence=$derived(rawEntry?evidence[rawKey]:undefined);
	const rawBinding=$derived(rawEntry?.instanceId===viewOutput?.instance?exhibit?.view.datasets.find(d=>d.output===rawEntry?.name):undefined);
	const rawProfile=$derived(recommendations?.datasets.find(d=>key(d.instanceId,d.name)===rawKey));
	const rawOutput=$derived(rawKey.startsWith('receipt:')?extraOutputs[Number(rawKey.slice(8))]:rawEvidence?describeOutput(rawEvidence,rawBinding,rawBinding?exhibit?.view.panels.filter(p=>p.dataset===rawBinding.id)??[]:[],metadata[rawKey],rawProfile):null);
	const exportHref=$derived(exportUrl??`${apiBaseUrl}/v1/jobs/${encodeURIComponent(jobId)}/artifacts/exploratory_outputs.json`);
	const canonical=(value:unknown):string=>JSON.stringify(value,(_key,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b))):v);
	$effect(()=>{
		void reload;const r=resources;const controller=new AbortController();
		catalog=[];sha='';error='';loading=true;exhibit=null;metadata={};evidence={};selection=null;viewOutput=null;tab='returned-data';rawKey='';rawQuery='';
		recommendations=null;recommending=false;recommendationError='';navigated=false;
		if(awaiting){loading=false;return()=>controller.abort();}
		(async()=>{
			const result=await r.catalog(controller.signal);if(controller.signal.aborted)return;
			catalog=result.outputs;sha=result.sha;const resultIdentity=result.sha;
			const descriptions=await Promise.all(catalog.filter(o=>o.kind==='dataset_semantics').map(async o=>({instance:o.instanceId,value:await r.semantics(o.instanceId,o.name,controller.signal)})));
			if(controller.signal.aborted)return;
			const described:Record<string,DatasetSemantics>={};
			for(const {instance,value} of descriptions){const k=key(instance,value.dataset.output);if(described[k])throw new Error('Output has conflicting dataset semantics');described[k]=value;}
			metadata=described;
			const requested=catalog.find(o=>o.instanceId===initialInstance&&o.name===initialOutput);
			const view=(requested?.kind==='research_view'?requested:undefined)??catalog.find(o=>o.instanceId===initialInstance&&o.kind==='research_view')??catalog.find(o=>o.kind==='research_view');
			if(view)await inspectView(view.instanceId,view.name);
			if(controller.signal.aborted)return;
			if(requested&&requested.kind!=='research_view')await showReturned(key(requested.instanceId,requested.name));
			else if(!view)await showReturned();
			if(!reader||recommendationReader){
				recommending=true;
				void (recommendationReader??getStudyRecommendations)(jobId).then(async result=>{
					if(controller.signal.aborted)return;
					if(result.job_id!==jobId||result.artifactSha256!==resultIdentity)throw new Error('Recommendation evidence identity mismatch');
					recommendations=result;
					if(!view&&!navigated&&result.datasets.some(d=>d.panels.length))await activatePanel(result.datasets.flatMap(d=>d.panels)[0].id);
				}).catch(cause=>{if(!controller.signal.aborted)recommendationError=cause instanceof Error?cause.message:String(cause);}).finally(()=>{if(!controller.signal.aborted)recommending=false;});
			}
		})().catch(cause=>{if(!controller.signal.aborted){error=cause instanceof Error?cause.message:String(cause);if(extraOutputs.length&&!rawKey)rawKey='receipt:0';}}).finally(()=>{if(!controller.signal.aborted)loading=false;});
		return()=>{controller.abort();inspection?.abort();};
	});
	async function load(instance:string,name:string,signal:AbortSignal){
		const k=key(instance,name);if(evidence[k])return evidence[k];
		const data=await resources.evidence(instance,name,signal);if(signal.aborted)return data;
		const descriptor=metadata[k];if(descriptor)validateSemanticsEvidence(descriptor,data);
		evidence={...evidence,[k]:data};return data;
	}
	async function inspectView(instance:string,name:string){
		inspection?.abort();const controller=new AbortController();inspection=controller;busy=true;error='';
		try{
			const saved=await resources.exhibit(instance,name,controller.signal);if(controller.signal.aborted)return;
			for(const dataset of saved.view.datasets){const descriptor=metadata[key(instance,dataset.output)];if(descriptor&&canonical(descriptor.dataset)!==canonical(dataset))throw new Error('Research view conflicts with canonical dataset semantics');}
			exhibit=saved;viewOutput={instance,name};selection=null;cursor=null;
			if(saved.view.panels[0])await activatePanel(saved.view.panels[0].id);else await showReturned();
		}catch(cause){if(!controller.signal.aborted)error=cause instanceof Error?cause.message:String(cause);}
		finally{if(!controller.signal.aborted)busy=false;}
	}
	async function activatePanel(panelId:string){
		const p=allPanels.find(p=>p.id===panelId);const source=recommendations?.datasets.find(d=>d.panels.some(p=>p.id===panelId));const dataset=source?.dataset??exhibit?.view.datasets.find(d=>d.id===p?.dataset);const instance=source?.instanceId??viewOutput?.instance;
		if(!p||!dataset||!instance)return;
		inspection?.abort();const controller=new AbortController();inspection=controller;busy=true;error='';tab=`panel:${panelId}`;
		try{const data=await load(instance,dataset.output,controller.signal);if(!controller.signal.aborted)validateBinding(dataset,data);}
		catch(cause){if(!controller.signal.aborted)error=cause instanceof Error?cause.message:String(cause);}
		finally{if(!controller.signal.aborted)busy=false;}
	}
	async function showReturned(requestedKey?:string,query?:string){
		tab='returned-data';rawQuery=query??rawQuery;
		const first=catalog.find(o=>!['research_view','dataset_semantics'].includes(o.kind))??catalog[0];
		const next=requestedKey??rawKey;rawKey=next|| (first?key(first.instanceId,first.name):extraOutputs.length?'receipt:0':'');
		if(rawKey.startsWith('receipt:')){busy=false;return;}
		const output=catalog.find(o=>key(o.instanceId,o.name)===rawKey);if(!output)return;
		inspection?.abort();const controller=new AbortController();inspection=controller;busy=true;error='';
		try{await load(output.instanceId,output.name,controller.signal);}
		catch(cause){if(!controller.signal.aborted)error=cause instanceof Error?cause.message:String(cause);}
		finally{if(!controller.signal.aborted)busy=false;}
	}
	function inspectSource(s:Selection){if(binding&&panelInstance){navigated=true;void showReturned(key(panelInstance,binding.output),binding.identity?String(s.row[binding.identity]):'');}}
	function moveTab(event:KeyboardEvent){
		const buttons=Array.from((event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('[role="tab"]'));const index=buttons.indexOf(event.target as HTMLButtonElement);
		const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:event.key==='ArrowRight'?(index+1)%buttons.length:event.key==='ArrowLeft'?(index-1+buttons.length)%buttons.length:-1;
		if(next<0)return;event.preventDefault();buttons[next].focus();buttons[next].click();
	}
</script>

<svelte:window onkeydown={event=>{if(event.key==='Escape')full=false;}}/>
<section class:full class:embedded class="research-explorer" data-testid="research-explorer" aria-label="Research evidence">
	{#if !embedded}<header><h2>{exhibit?.view.title??'Research evidence'}</h2><button onclick={()=>full=!full}>{full?'Close expanded view':'Expand'}</button></header>{/if}
	{#if views.length>1}<label class="view-picker">Research exhibit<select value={viewOutput?key(viewOutput.instance,viewOutput.name):''} onchange={event=>{const [instance,name]=JSON.parse(event.currentTarget.value);void inspectView(instance,name);}}>{#each views as output}<option value={key(output.instanceId,output.name)}>{output.instanceId} · {output.name}</option>{/each}</select></label>{/if}
	{#if !awaiting && (catalog.length||extraOutputs.length||returnedData)}
		<div class="tabs" role="tablist" tabindex="-1" aria-label="Research views" onkeydown={moveTab}>
			{#each allPanels as p}<button role="tab" id={`${id}-tab-${p.id}`} aria-controls={`${id}-content`} aria-selected={tab===`panel:${p.id}`} tabindex={tab===`panel:${p.id}`?0:-1} onclick={()=>{navigated=true;void activatePanel(p.id);}}>{p.title}</button>{/each}
			<button role="tab" id={`${id}-tab-returned`} aria-controls={`${id}-content`} aria-selected={tab==='returned-data'} tabindex={tab==='returned-data'?0:-1} onclick={()=>{navigated=true;void showReturned();}}>Returned data</button>
		</div>
	{/if}
	<div class="content" role="tabpanel" id={`${id}-content`} aria-labelledby={tab==='returned-data'?`${id}-tab-returned`:`${id}-tab-${tab.slice(6)}`} tabindex="0">
		{#if awaiting}<p class="state" role="status">Research views will appear here when execution finishes.</p>
		{:else}
			{#if loading||busy}<p class="state" role="status">Loading evidence…</p>{/if}
			{#if recommending}<p class="state" role="status">Recommending research views…</p>{/if}
			{#if recommendationError}<p class="state">Recommendations unavailable: {recommendationError}. Manual views remain available.</p>{/if}
			{#if recommendations}<p class="state" data-testid="recommendation-status">{recommendations.status==='ranked'?`Suggested by ${recommendations.receipt.model}`:`Deterministic suggestions · ${recommendations.note}`}</p>{/if}
			{#if error}<div class="state error" role="alert"><strong>Evidence unavailable</strong><p>{error}</p><button onclick={()=>{if(panel)void activatePanel(panel.id);else if(rawKey)void showReturned(rawKey);else reload++;}}>Retry evidence</button></div>{/if}
			{#if !loading&&!catalog.length&&!error}<p class="state">This execution has no published exploratory outputs.</p>{/if}
			<div hidden={tab!=='returned-data'}>
				{#if !allPanels.length&&!loading&&!error}<p class="state">No research views were published. Explore the returned data or use the recommendations when ready.</p>{/if}
				{#if catalog.length||extraOutputs.length}<label class="data-picker">Data<select aria-label="Data" value={rawKey} onchange={event=>showReturned(event.currentTarget.value,'')}>{#each catalog as output}<option value={key(output.instanceId,output.name)}>{output.name} · {output.instanceId}{output.count!==null?` · ${output.count}`:''}</option>{/each}{#each extraOutputs as output,index}<option value={`receipt:${index}`}>{output.dataset.output}</option>{/each}</select></label>{/if}
				{#if rawOutput&&rawOutput.evidence.columns.length}{#key `${rawKey}:${rawQuery}`}<ReturnedData outputs={[rawOutput]} initialQuery={rawQuery} hideDatasetPicker />{/key}
				{:else if !busy&&rawEvidence}<pre>{JSON.stringify(rawEvidence.output,null,2)}</pre>{/if}
				{#if returnedData}<div class="tool-evidence">{@render returnedData()}</div>{/if}
			</div>
			{#if tab!=='returned-data'&&!busy&&panel&&panelOutput}
				<article data-panel={panel.kind}><div class="view-heading"><h3>{panel.title}</h3><span>{panel.kind}</span></div>{#if panel.description}<p class="state">{panel.description}</p>{/if}
				<EvidencePanel output={panelOutput} {panel} bind:selection bind:cursor oninspect={inspectSource}/></article>
			{/if}
		{/if}
	</div>
	{#if !awaiting}<footer><details><summary>Evidence provenance & interpretation</summary><p>Job: {jobId}</p><p>Artifact: exploratory_outputs.json · SHA-256: {sha||'Not loaded'}</p>{#each exhibit?.view.limitations??[] as limitation}<p>{limitation}</p>{/each}<p>Read-only published evidence. Filtering and mapping checks cover loaded observations; no implicit aggregation is performed.</p></details><div><button onclick={()=>full=!full}>{full?'Close expanded view':'Expand'}</button><a href={exportHref} download="exploratory_outputs.json">Export evidence ↓</a></div></footer>{/if}
</section>

<style>
	.research-explorer{color:var(--text);min-width:0;background:var(--color-surface-900);border:1px solid var(--border);border-radius:8px;overflow:hidden}.embedded{border:0;border-radius:0}.full{position:fixed;inset:24px;z-index:100;overflow:auto;box-shadow:0 24px 100px #18342545;border:1px solid var(--border);border-radius:8px}
	header{padding:16px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px}h2{font-size:14px;font-weight:600;margin:0}button,select{font:inherit}button{cursor:pointer;background:none;border:0;color:var(--color-accent);font-size:10px}button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--color-accent);outline-offset:2px}
	.tabs{display:flex;gap:20px;overflow:auto;border-bottom:1px solid var(--border);padding:0 18px}.tabs button{padding:11px 0;border-bottom:2px solid transparent;white-space:nowrap;color:var(--color-slate-400);font-size:11px}.tabs button[aria-selected=true]{border-color:var(--color-accent);color:var(--color-accent)}
	.content{padding:18px;min-width:0}.view-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px}h3{font-size:12px;font-weight:600;margin:0}.view-heading span{font-size:9px;color:var(--color-slate-400)}.state{font-size:11px;color:var(--color-slate-400);line-height:1.7;margin:0 0 12px}.error{color:var(--color-loss)}
	.view-picker,.data-picker{display:flex;flex-direction:column;gap:5px;font-size:9px;color:var(--color-slate-400);max-width:100%;margin-bottom:12px}.view-picker{padding:12px 18px 0}.data-picker select,.view-picker select{font-size:10px;color:var(--text);background:var(--color-surface-900);border:1px solid var(--border);border-radius:4px;padding:7px;min-width:0;max-width:100%}
	pre{font-size:10px;line-height:1.6;overflow:auto;max-height:280px;background:var(--color-surface-800);padding:12px;border-radius:4px}.tool-evidence{margin-top:14px}footer{display:flex;gap:12px;align-items:start;border-top:1px solid var(--border);padding:12px 18px;font-size:10px;color:var(--color-slate-400)}footer details{flex:1;min-width:0}summary{cursor:pointer}footer p{line-height:1.7;margin-top:8px;overflow-wrap:anywhere}footer>div{display:flex;gap:12px;flex-wrap:wrap}footer a{color:var(--color-slate-400);text-decoration:none;white-space:nowrap}
	@media(max-width:760px){.content{padding:14px 12px}.tabs{padding:0 12px;gap:14px}.tabs button{font-size:10px}footer{padding:12px;flex-wrap:wrap}.full{inset:8px}}
</style>
