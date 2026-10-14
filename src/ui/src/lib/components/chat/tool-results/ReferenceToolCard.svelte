<script lang="ts">
 import type { ToolResult } from '$lib/types';
 import MarkdownBody from '../MarkdownBody.svelte';
 import {isRecord,readString} from './safe';
 let {result}:{result:ToolResult;workspaceId:string}=$props();
 const data=$derived(isRecord(result.structured)?result.structured:{});
 const hits=$derived(Array.isArray(data.results)?data.results.filter(isRecord):[]);
</script>
<div class="space-y-2" data-testid="reference-tool-card">
 {#if hits.length}{#each hits as hit}<details class="rounded-lg border border-surface-600 bg-surface-950/20 p-3"><summary class="cursor-pointer text-xs font-semibold text-slate-700">{readString(hit,'title')??'Knowledge reference'}</summary><p class="mt-2 text-[10px] text-slate-500">{readString(hit,'source')??''} · version {String(hit.version??'')} · {isRecord(hit.provenance)?readString(hit.provenance,'trust_tier')??'':''}</p><p class="mt-2 text-xs text-slate-600">{readString(hit,'excerpt')??''}</p><p class="mt-2 break-all font-mono text-[10px] text-slate-500">{readString(hit,'reference')??''}</p></details>{/each}
 {:else}<div class="rounded-lg border border-surface-600 p-3"><h3 class="text-xs font-semibold text-slate-700">{readString(data,'title')??(result.tool==='read_sdk_reference'?'Authoring SDK reference':'Knowledge reference')}</h3><div class="mt-3 max-h-80 overflow-auto">{#if result.tool==='read_sdk_reference'}<pre class="whitespace-pre-wrap font-mono text-[11px]">{readString(data,'content')??readString(data,'source')??result.text??''}</pre>{:else}<MarkdownBody text={readString(data,'content')??result.text??''}/>{/if}</div>{#if data.truncated}<p class="mt-2 text-xs text-slate-500">Bounded excerpt. Read the next source range for the remaining content.</p>{/if}<p class="mt-2 break-all font-mono text-[10px] text-slate-500">{readString(data,'reference')??readString(data,'sdkIdentity')??''}</p></div>{/if}
 <details class="text-[10px] text-slate-500"><summary>Source provenance</summary><pre class="max-h-40 overflow-auto">{JSON.stringify(data.provenance??data.sourceHashes??{},null,2)}</pre></details>
 <details class="text-[10px] text-slate-500"><summary>Returned payload</summary><pre class="max-h-80 overflow-auto whitespace-pre-wrap">{JSON.stringify({structured:result.structured,text:result.text},null,2)}</pre></details>
</div>
