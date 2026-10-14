<script lang="ts">
 import type { ToolResult } from '$lib/types';
 import { toolResultComponent, toolResultPresentation, toolResultHeader } from './tool-results/registry';
 import ActivityMarker from './tool-results/ActivityMarker.svelte';
 import { isRecord, prettyJson } from './tool-results/safe';
 import StudyToolCard from './tool-results/StudyToolCard.svelte';
 import GenericToolCard from './tool-results/GenericToolCard.svelte';

 let { result, workspaceId, connectRail=false }: { result: ToolResult; workspaceId: string; connectRail?:boolean } = $props();
 const studyObservation=$derived(isRecord(result.structured)&&result.structured.kind==='study'&&['get_job_status','get_job_result'].includes(result.tool??''));
 const Card=$derived(result.pending||result.outcome||!result.ok?GenericToolCard:studyObservation?StudyToolCard:toolResultComponent(result.datumType));
 const prominent=$derived(toolResultPresentation(Card,result)==='result');
 const structured=$derived(isRecord(result.structured)?result.structured:{});
 const startedAt=$derived(result.startedAt??structured.startedAt);
 const durationMs=$derived(result.durationMs??structured.durationMs);
 const reused=$derived(structured.reused);
 const argumentsData=$derived(result.arguments??structured.arguments);
 const status=$derived(result.pending?'pending':result.outcome??(result.ok?'completed':'failed'));
 const header=$derived(toolResultHeader(result));
 const label=$derived(header.subject ? `${header.action}: ${header.subject}` : header.action);
</script>

<section class="tool-activity" class:connectRail class:prominent class:failed={!result.ok&&!result.pending} data-testid="tool-result-card" data-tool={result.tool} data-datum-type={result.datumType}>
 <span class="activity-marker"><ActivityMarker icon={header.icon}/></span>
 {#if prominent}
  <div class="activity-label">{@render receiptLabel()}</div>
  <div class="activity-result"><Card {result} {workspaceId} hideSummary={true}/></div>
  {#if startedAt!=null||durationMs!=null||reused!=null||argumentsData!=null||result.callId}<details class="metadata-disclosure"><summary>Execution receipt</summary>{@render metadata()}</details>{/if}
 {:else}
  <details class="activity-receipt" open={!result.ok&&!result.pending}>
   <summary>{@render receiptLabel()}<span class="disclosure" aria-hidden="true">›</span></summary>
   <div class="receipt-content"><Card {result} {workspaceId} hideSummary={true}/>{@render metadata()}</div>
  </details>
 {/if}
</section>

{#snippet receiptLabel()}
 <span class="tool-label" title={label}>{label}</span>
 {#if header.count}<span class="receipt-count" data-testid="tool-result-count">{header.count}</span>{/if}
 <span class="tool-status" data-testid="tool-result-status">{status}</span>
 {#if durationMs!=null}<span class="receipt-time">{String(durationMs)} ms</span>{/if}
{/snippet}

{#snippet metadata()}
 <div class="tool-metadata" data-testid="tool-metadata">
  {#if result.callId}<p>Call: <span class="font-mono">{result.callId}</span></p>{/if}
  {#if result.tool}<p>Tool: {result.tool}</p>{/if}
  {#if result.operation}<p>Operation: {result.operation}</p>{/if}
  {#if startedAt!=null}<p data-testid="tool-started-at">started: {String(startedAt)}</p>{/if}
  {#if durationMs!=null}<p>duration: {String(durationMs)} ms</p>{/if}
  {#if reused!=null}<p>reused: {String(reused)}</p>{/if}
  {#if argumentsData!=null}<details><summary>arguments</summary><pre>{prettyJson(argumentsData)}</pre></details>{/if}
 </div>
{/snippet}

<style>
 .tool-activity{position:relative;width:min(100%,44rem);min-width:0;margin-right:auto;border-left:1px solid var(--color-surface-600);padding:0 0 8px 20px;text-align:left;overflow-wrap:anywhere;color:var(--color-slate-200)}
 .activity-marker{position:absolute;left:-9px;top:7px;width:17px;height:17px;display:flex;align-items:center;justify-content:center;background:var(--color-surface-950);color:var(--color-accent)}
 .connectRail::before{content:'';position:absolute;left:-1px;top:-.75rem;height:.75rem;border-left:1px solid var(--color-surface-600);pointer-events:none}
 .failed .activity-marker{color:var(--color-loss)}
 .activity-label,.activity-receipt>summary{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;min-height:32px;padding:6px 0;font-size:12px;list-style:none;text-align:left}
 .activity-receipt>summary::-webkit-details-marker{display:none}.activity-receipt>summary{cursor:pointer}
 .tool-label{font-weight:500;min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tool-status,.receipt-time,.receipt-count,.disclosure{font-size:11px;color:var(--color-slate-400)}.failed .tool-status{color:var(--color-loss)}
 .disclosure{transition:transform .15s}.activity-receipt[open] .disclosure{transform:rotate(90deg)}
 .receipt-content{padding:10px 0 6px}.activity-result{margin-top:6px;min-width:0}
 .metadata-disclosure{margin-top:8px;font-size:11px;color:var(--color-slate-400)}.tool-metadata{margin-top:10px;font-size:11px;color:var(--color-slate-400)}
 .tool-metadata p{margin:4px 0}.tool-metadata pre{max-height:200px;overflow:auto;white-space:pre-wrap;font-size:11px}
 @media(max-width:480px){.tool-activity{padding-left:12px}.activity-label,.activity-receipt>summary{gap:7px}}
</style>
