<script lang="ts">
 import { getHarnessJobResult, listHarnessArtifacts } from '$lib/api';
 import type { HarnessArtifactManifest, HarnessJobResult } from '$lib/types';
	import ParameterBarChart from '$lib/components/charts/ParameterBarChart.svelte';
	import { isSucceededState, metricValue, parameterCategories, runStateTone } from '$lib/components/charts/parameter-bar';
	import type { VariationReportData } from '$lib/transcript';

	/**
	 * The deterministic parameter-variation report card: one chart per varied
	 * parameter, the center configuration marked as the selected baseline, and
	 * explicit gaps for metrics a run did not record. Data is the decoded
	 * variation artifact; nothing is inferred beyond the payload.
	 */
	let { data }: { data: VariationReportData; workspaceId?: string } = $props();

	const center = $derived(data.center);
	const points = $derived(data.points ?? []);
	const parameters = $derived(data.parameters ?? []);
	const charts = $derived.by(() =>
		parameters.map((name) => ({ name, categories: parameterCategories(name, center, points) }))
	);
	const succeededCount = $derived(points.filter((point) => isSucceededState(point.state)).length);
 const allRuns = $derived([...(center ? [center] : []), ...points]);
 let jobDetails = $state<Record<string, {result?: HarnessJobResult; artifacts?: HarnessArtifactManifest; error?: string; loading?: boolean}>>({});
 async function inspectJob(jobId: string) {
   jobDetails[jobId] = {loading: true};
   try {
     const [result, artifacts] = await Promise.all([getHarnessJobResult(jobId), listHarnessArtifacts(jobId)]);
     if (result.job_id !== jobId || artifacts.job_id !== jobId) throw new Error('Returned evidence names a different job');
     jobDetails[jobId] = {result, artifacts};
   } catch (error) { jobDetails[jobId] = {error: error instanceof Error ? error.message : String(error)}; }
 }
 const exportHref = $derived(`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(data.raw ?? data, null, 2))}`);
 function metric(run: typeof center, key: string): string {
   if (!run) return 'untested';
   const value = run.metrics[key] ?? (key === 'total_return' ? run.metrics['netTotalReturn'] ?? run.metrics['totalReturn'] : run.metrics['profitFactor']);
   if (value !== undefined && value !== null) return String(value);
   return run.metricReasons?.[key] ? `undefined: ${run.metricReasons[key]}` : run.state === 'failed' ? 'failed' : run.jobId ? 'missing' : 'untested';
 }
 function matched(run: typeof center) {
   if (!run?.parameterIdentity || !data.matchedDevelopment) return null;
   return [data.matchedDevelopment.center, ...data.matchedDevelopment.points].find((point) => point?.parameterIdentity === run.parameterIdentity) ?? null;
 }
 function interactionCategories(group: string[]) {
   return allRuns.map((run, index) => ({
     key: run.parameterIdentity ?? run.jobId ?? String(index),
     label: group.map((key) => `${key}=${run.levelLabels[key] ?? String(run.parameters[key])}`).join(', '),
     value: index,
     center: run === center,
     state: run.state,
     jobId: run.jobId,
     error: run.error,
     totalReturn: metricValue(run.metrics, 'total_return', 'netTotalReturn', 'totalReturn'),
     profitFactor: metricValue(run.metrics, 'profit_factor', 'profitFactor'),
     totalReturnGap: metric(run, 'total_return'),
     profitFactorGap: metric(run, 'profit_factor')
   }));
 }
</script>

<section
	class="overflow-hidden rounded-xl border border-surface-700 bg-surface-900 shadow-sm"
	data-testid="variation-report-card"
>
	<header class="border-b border-surface-700 bg-surface-950/40 p-3.5">
		<div class="flex flex-wrap items-center justify-between gap-2">
			<h3 class="text-sm font-semibold text-slate-800">{data.dataset === 'forward' ? 'Forward' : 'Development'} Parameter Variation</h3>
            <a href={exportHref} download={`research-${data.dataset ?? 'development'}.json`} data-testid="research-export">Export complete evidence</a>
			<div class="flex flex-wrap items-center gap-2">
				{#if succeededCount < points.length}
					<span
						class="rounded-full border border-warn/40 bg-warn/10 px-2.5 py-0.5 font-mono text-[10px] font-semibold uppercase text-warn"
						data-testid="variation-state-summary"
					>
						{succeededCount}/{points.length} points succeeded
					</span>
				{/if}
				<span
					class="rounded-full border border-accent/40 bg-accent/10 px-2.5 py-0.5 font-mono text-[10px] font-semibold uppercase text-accent"
					data-testid="variation-summary"
				>
					{parameters.length} {parameters.length === 1 ? 'parameter' : 'parameters'} ·
					{points.length} {points.length === 1 ? 'point' : 'points'}
				</span>
			</div>
		</div>
		{#if center}
			<div
				class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-700"
				data-testid="variation-center"
			>
				<span class="font-semibold text-slate-500">Center:</span>
				<span
					class="font-mono text-[10px] font-semibold uppercase {runStateTone(center.state)}"
					data-testid="variation-center-state"
				>
					{center.state ?? 'unknown'}
				</span>
				{#if center.jobId}
					<span class="font-mono text-[10px] text-slate-500">job {center.jobId}</span>
				{/if}
				{#each Object.entries(center.parameters) as [key, value] (key)}
					<span class="rounded-md border border-surface-700 bg-surface-950/40 px-1.5 py-0.5 font-mono text-[10px]">
						<span class="text-slate-500">{key}</span>
						<span class="ml-1 font-semibold text-slate-800">{center.levelLabels[key] ?? String(value)}</span>
					</span>
				{/each}
			</div>
		{/if}
		{#if data.stability}
			<p class="mt-2 text-xs leading-snug text-slate-600" data-testid="variation-stability">
				{data.stability}
			</p>
		{/if}
	</header>
 {#if data.comparison}<p class="p-3.5 text-xs" data-testid="research-comparison">{data.comparison}</p>{/if}
 {#if data.forwardEvidenceScope === 'adaptive_holdout_reuse'}<p class="p-3.5 text-xs text-warn" role="status">This forward window was previously exposed under another design. These results reflect adaptive holdout reuse and do not establish performance on untouched data.</p>{/if}
 {#if data.limitations}<p class="p-3.5 text-xs">Limitations: {data.limitations}</p>{/if}
 {#each data.interactions ?? [] as group}
   <section class="p-3.5" data-testid="research-interaction">
     <h4>Joint interaction: {group.join(' × ')}</h4>
     <ParameterBarChart title={group.join(' × ')} categories={interactionCategories(group)} testId="research-interaction-chart" />
     <table><thead><tr><th>Assignment</th><th>Total return</th><th>Profit factor</th><th>State</th></tr></thead><tbody>
       {#each allRuns as run}
         <tr><td>{group.map((key) => `${key}=${String(run.parameters[key])}`).join(', ')}</td><td>{metric(run, 'total_return')}</td><td>{metric(run, 'profit_factor')}</td><td>{run.state ?? 'untested'}</td></tr>
       {/each}
     </tbody></table>
   </section>
 {/each}
 <details class="p-3.5" data-testid="research-evidence-table"><summary>Jobs, artifacts, and matched evidence</summary>
   <table><thead><tr><th>Parameters</th><th>Job / state</th><th>Total return</th><th>Profit factor</th>{#if data.matchedDevelopment}<th>Development return / profit factor</th>{/if}</tr></thead><tbody>
   {#each allRuns as run}
     <tr data-testid="research-evidence-row" data-parameter-identity={run.parameterIdentity}><td>{Object.keys(run.parameters).map((key) => `${key}=${run.levelLabels[key] ?? String(run.parameters[key])}`).join(', ')}</td><td><details><summary>{run.jobId ?? 'untested'} · {run.state ?? 'untested'}</summary><pre>{JSON.stringify(run.raw ?? run, null, 2)}</pre>{#if run.error}<p>{run.error}</p>{/if}</details></td><td>{metric(run, 'total_return')}</td><td>{metric(run, 'profit_factor')}</td>{#if data.matchedDevelopment}<td>{metric(matched(run), 'total_return')} / {metric(matched(run), 'profit_factor')}</td>{/if}</tr>
   {/each}
   </tbody></table>
   {#each allRuns.filter((run) => run.jobId) as run}
     <section class="mt-3">
       <button type="button" onclick={() => inspectJob(run.jobId!)} disabled={jobDetails[run.jobId!]?.loading}>Inspect job and artifacts: {run.jobId}</button>
       {#if jobDetails[run.jobId!]?.error}<p role="alert">{jobDetails[run.jobId!].error}</p>{/if}
       {#if jobDetails[run.jobId!]?.result}<pre>{JSON.stringify(jobDetails[run.jobId!].result, null, 2)}</pre>{/if}
       {#if jobDetails[run.jobId!]?.artifacts}
         {#each jobDetails[run.jobId!].artifacts!.artifacts as artifact}
           <p>{artifact.name} · {artifact.sha256} · {artifact.sizeBytes} bytes {#if artifact.sasUrl}<a href={artifact.sasUrl} target="_blank" rel="noreferrer">Open artifact</a>{/if}</p>
         {/each}
         {#if !jobDetails[run.jobId!].artifacts!.artifacts.length}<p>No artifacts published.</p>{/if}
       {/if}
     </section>
   {/each}
   <details><summary>Complete accepted evidence and artifact references</summary><pre>{JSON.stringify(data.raw ?? data, null, 2)}</pre></details>
 </details>

	{#if charts.length}
		<div class="space-y-3 p-3.5" data-testid="variation-charts">
            {#if data.interactions?.length}<p class="text-xs">Marginal charts include only single-parameter assignments. Declared joint assignments are shown in the interaction views.</p>{/if}
			{#each charts as chart (chart.name)}
				<ParameterBarChart
					title={chart.name}
					categories={chart.categories}
					testId={`variation-chart-${chart.name}`}
				/>
			{/each}
		</div>
	{:else}
		<p class="p-3.5 text-xs text-slate-500" data-testid="variation-empty">
			No varied parameters were recorded for this run.
		</p>
	{/if}
</section>
