<script lang="ts">
	import type { ToolResult } from '$lib/types';
	import ResearchExplorer from '$lib/research/ResearchExplorer.svelte';
	import ReturnedData from '$lib/research/ReturnedData.svelte';
	import { describeOutput } from '$lib/research/semantics';
	import type { StudyRecommendations } from '$lib/research/recommendations';
	import type { PageReader } from '$lib/research/resources';
	import { observeJob, type JobObservation, type JobReader } from '$lib/research/jobs';
	import { isRecord,readString } from './safe';
	let {result,workspaceId,reader,recommendationReader,jobReader,exportUrl,hideSummary=false}:{result:ToolResult;workspaceId:string;reader?:PageReader;recommendationReader?:(job:string)=>Promise<StudyRecommendations>;jobReader?:JobReader;exportUrl?:string;hideSummary?:boolean}=$props();
	const data=$derived(isRecord(result.structured)?result.structured:{});
	const jobId=$derived(readString(data,'job_id'));
	const observations=$derived(result.observations??[]);
	const latest=$derived(observations.at(-1)??result);
	const metrics=$derived(isRecord(latest.structured?.metrics)?latest.structured.metrics:{});
	const program=$derived(readString(data,'programId'));
	const metricRows=$derived(Object.entries(metrics).map(([metric,value])=>({metric,value:String(value??'Unavailable')})));
	const extraOutputs=$derived(metricRows.length?[describeOutput({output:{},columns:['metric','value'],rows:metricRows,total:metricRows.length,partial:false},{id:'Metrics',output:'Metrics',fields:['metric','value'].map(name=>({name,label:name,description:'Exact metric returned by the study tool.',field_type:'string',unit:null})),identity:'metric',selection_group:null,event_time:null,available_at:null})]:[]);
	let execution=$state<JobObservation>({job:null,error:''});
	let observedAt=$state(Date.now());
	const tracksExecution=$derived(result.tool==='run_study');
	const terminal=$derived(['succeeded','failed','cancelled','completed'].includes(execution.job?.state??''));
	const awaiting=$derived(tracksExecution&&!terminal);
	const progress=$derived([...(execution.job?.progress??[])].sort((a,b)=>Date.parse(b.updated_at)-Date.parse(a.updated_at))[0]);
	const labels:Record<string,string>={compiling:'Compiling program',preparing_data:'Preparing market data',reading_data:'Reading market data',running_study:'Running study',writing_artifacts:'Writing results',interrupted:'Execution interrupted'};
	const status=$derived(!tracksExecution?'Published evidence':execution.error&&!execution.job?'Status unavailable':execution.job?.state==='failed'?'Failed':execution.job?.state==='cancelled'?'Cancelled':terminal?'Completed':execution.job?.state==='queued'?'Queued':execution.job?'Running':'Loading status');
	const elapsed=$derived(execution.job?.created_at?Math.max(0,Math.floor(((terminal?Date.parse(execution.job.updated_at??''):observedAt)-Date.parse(execution.job.created_at))/1000)):null);
	$effect(()=>{execution={job:null,error:''};if(jobId&&tracksExecution)return observeJob(jobId,observation=>{execution=observation;observedAt=Date.now();},jobReader);});
</script>

<article class:receipt={!jobId} class="study-result" data-testid="study-tool-card">
	<header><div><p class="tool-label">{jobId?'Study evidence':'Study'}{#if data.version}<span>Revision {String(data.version).slice(0,12)}</span>{/if}</p><h2>{program??'Research evidence'}</h2>{#if !hideSummary&&!jobId}<p class="summary">{result.summary??result.tool}</p>{/if}</div>
		{#if jobId}<div class="status" class:failed={execution.job?.state==='failed'||execution.job?.state==='cancelled'}><span>{status}</span>{#if elapsed!==null&&Number.isFinite(elapsed)}<small>{elapsed<60?`${elapsed}s`:`${Math.floor(elapsed/60)}m ${elapsed%60}s`}</small>{/if}</div>{:else if data.accepted!==undefined}<span class="status">{data.accepted?'Validated':'Needs revision'}</span>{/if}
	</header>
	{#if result.failure}<p class="error" role="alert" data-testid="tool-failure">{String(result.failure.message??result.summary)}</p>{/if}
	{#if jobId}
		{#if tracksExecution&&(awaiting||execution.job?.state==='failed'||execution.job?.state==='cancelled')&&(progress||execution.error||terminal)}
			<div class="execution" role="status">{#if progress}<strong>{labels[progress.stage]??progress.stage}</strong>{#if progress.sessions_total!=null&&progress.sessions_ready!=null}<p>{progress.sessions_ready} / {progress.sessions_total} sessions ready</p>{/if}{#if progress.rows||progress.bytes}<p>{progress.rows.toLocaleString()} rows{progress.bytes?` · ${(progress.bytes/1e6).toFixed(1)} MB received`:''}</p>{/if}{/if}
			{#if execution.job?.error_message}<p class="error">{execution.job.error_message}</p>{/if}{#if execution.error}<p>{execution.error}{execution.job?' · Showing the last observation.':''}</p>{/if}
			{#if terminal}<p>{execution.job?.state==='cancelled'?'Execution was cancelled.':'Execution stopped.'} Any published evidence remains inspectable below.</p>{/if}</div>
		{/if}
		<ResearchExplorer {jobId} {reader} {recommendationReader} {exportUrl} {awaiting} embedded {extraOutputs} initialInstance={readString(data,'instanceId')??undefined} initialOutput={readString(data,'name')??undefined} {returnedData}/>
		{#if tracksExecution}<details class="execution-details"><summary>Execution details</summary><p>{jobId}</p><pre>{JSON.stringify(execution.job,null,2)}</pre></details>{/if}
	{:else}
		<div class="receipt-content">{#if data.artifactPath}<a href={`/w/${workspaceId}/artifacts?artifact=study:${encodeURIComponent(program??'')}`}>Inspect committed study</a>{/if}
		{#if extraOutputs.length}<ReturnedData outputs={extraOutputs}/>{/if}{@render returnedData()}</div>
	{/if}
</article>
{#snippet returnedData()}
	{#if result.text}<details><summary>Returned text</summary><pre>{result.text}</pre></details>{/if}
	{#if Array.isArray(data.diagnostics)&&data.diagnostics.length}<details open={!!result.failure}><summary>Diagnostics</summary><pre>{JSON.stringify(data.diagnostics,null,2)}</pre></details>{/if}
	{#if observations.length}<details><summary>{observations.length} retained execution observations</summary><pre>{JSON.stringify(observations,null,2)}</pre></details>{/if}
	<details><summary>Tool evidence</summary><pre>{JSON.stringify(data,null,2)}</pre></details>
{/snippet}

<style>
	.study-result{border:1px solid var(--border);border-radius:8px;background:var(--color-surface-900);color:var(--text);overflow:hidden;min-width:0;max-width:100%}header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:16px 18px}.tool-label{font-size:10px;color:var(--color-slate-400);margin:0 0 5px}.tool-label span{margin-left:10px;font-size:9px}h2{font-size:14px;font-weight:600;letter-spacing:-.2px;margin:0;overflow-wrap:anywhere}.summary{font-size:11px;color:var(--color-slate-400);margin:6px 0 0;line-height:1.6}.status{font-size:10px;color:var(--color-accent);text-align:right;white-space:nowrap}.status small{display:block;color:var(--color-slate-400);margin-top:4px;font-size:10px}.failed,.error{color:var(--color-loss)}.error{font-size:11px;margin:0 18px 12px;overflow-wrap:anywhere}.execution{padding:12px 18px;border-top:1px solid var(--border);font-size:11px;color:var(--color-slate-400);line-height:1.7}.execution p{margin:5px 0}.execution .error{margin:5px 0}.execution strong{color:var(--text);font-weight:500}.execution-details{border-top:1px solid var(--border);padding:12px 18px}.receipt-content{padding:0 18px 16px}details{font-size:10px;color:var(--color-slate-400);margin-top:8px}summary{cursor:pointer}pre{font-size:10px;overflow:auto;max-height:240px;line-height:1.6;padding:10px;background:var(--color-surface-800);border-radius:4px;white-space:pre-wrap;overflow-wrap:anywhere}a{font-size:11px;color:var(--color-accent)}@media(max-width:760px){header{padding:14px 12px}.execution,.execution-details{padding:12px}.receipt-content{padding:0 12px 14px}}
</style>
