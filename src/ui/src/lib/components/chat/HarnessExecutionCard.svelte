<script lang="ts">
    import { observeJob } from '$lib/research/jobs';
    import type { HarnessJobProgress, HarnessJobStatus } from '$lib/types';

    let { jobId }: { jobId: string } = $props();
    let job = $state<HarnessJobStatus | null>(null);
    let error = $state('');
    let now = $state(Date.now());
    const terminal = (state: string) => ['succeeded', 'failed', 'cancelled', 'completed'].includes(state);
    const labels: Record<HarnessJobProgress['stage'], string> = {
        compiling: 'Compiling program', preparing_data: 'Preparing market data',
        reading_data: 'Reading market data', running_study: 'Running study',
        running_backtest: 'Running backtest', writing_artifacts: 'Writing results', interrupted: 'Execution interrupted'
    };
    const progress = $derived(job?.progress ?? []);
    const current = $derived([...progress].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))[0]);
    const stopped = $derived(job ? terminal(job.state) : false);
    function elapsed(start?: string | null, end?: string | null): string {
        if (!start) return '';
        const seconds = Math.max(0, Math.floor(((end ? Date.parse(end) : now) - Date.parse(start)) / 1000));
        return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    }
    function bytes(value: number): string {
        return value >= 1e9 ? `${(value / 1e9).toFixed(2)} GB` : `${(value / 1e6).toFixed(1)} MB`;
    }
    $effect(() => {
        job = null; error = ''; now = Date.now();
        return observeJob(jobId, observation => {
            job = observation.job; error = observation.error; now = Date.now();
        });
    });

</script>

<section class="space-y-2 rounded-lg border border-surface-700 bg-surface-950/30 p-3" data-testid="harness-execution-card" aria-label="Harness execution">
    <p class="text-[10px] text-slate-500">Current execution status · observational overlay</p>
    <div class="flex flex-wrap justify-between gap-2 text-xs">
        <strong>{error && !stopped ? 'Execution status unavailable' : current?.stage === 'interrupted' ? 'Execution interrupted' : job?.state === 'failed' ? 'Execution failed' : stopped ? 'Execution finished' : current ? labels[current.stage] : job?.state === 'queued' ? 'Waiting to start' : job ? 'Running execution' : 'Loading execution status'}</strong>
        <span class="text-slate-500">{elapsed(job?.created_at, stopped ? job?.updated_at : null)}{job ? ` · ${job.state}` : ''}</span>
    </div>
    {#if current}
        {#if current.symbol}<p class="text-xs text-slate-600">{current.symbol} · {current.data_type} · {current.start_date} to {current.end_date}</p>{/if}
        {#if current.sessions_total != null && current.sessions_ready != null}
            <p class="text-xs text-slate-600">{current.sessions_ready} / {current.sessions_total} sessions ready{current.sessions_cached != null ? ` · ${current.sessions_cached} cached before acquisition` : ''}</p>
        {/if}
        {#if current.rows || current.bytes}<p class="text-xs text-slate-600">{current.rows.toLocaleString()} rows{current.bytes ? ` · ${bytes(current.bytes)} received` : ''}</p>{/if}
        {#if !stopped}<p class="text-[11px] text-slate-500">Last observation {elapsed(current.updated_at)} ago</p>{/if}
    {/if}
    {#if job?.error_message}<p class="text-xs text-loss" role="alert">{job.error_message}</p>{/if}
    {#if job?.state === 'failed' && current && ['compiling', 'preparing_data', 'reading_data'].includes(current.stage)}
        <p class="text-xs text-slate-600">Execution stopped before the study or backtest ran. No results were produced.</p>
    {/if}
    {#if error}<p class="text-xs text-slate-500" role="status">Status unavailable: {error}{job ? ' · Showing the last observation.' : ''}</p>{/if}
    <details class="text-[11px] text-slate-500">
        <summary class="cursor-pointer">Execution details</summary>
        <p class="mt-2 break-all font-mono">{jobId}</p>
        <ol class="mt-2 space-y-2">
            {#each progress as entry}
                <li>
                    <strong>{labels[entry.stage]}</strong>{entry.symbol ? ` · ${entry.symbol} ${entry.data_type}` : ''}
                    <span> · {elapsed(entry.started_at, entry.finished_at ?? (stopped ? job?.updated_at : null))}</span>
                    {#if entry.start_date}<p>{entry.start_date} to {entry.end_date}</p>{/if}
                    {#if entry.sessions_total != null && entry.sessions_ready != null}
                        <p>{entry.sessions_ready} / {entry.sessions_total} sessions ready{entry.sessions_cached != null ? ` · ${entry.sessions_cached} cached before acquisition` : ''}</p>
                    {/if}
                    {#if entry.rows}<span> · {entry.rows.toLocaleString()} rows</span>{/if}
                    {#if entry.bytes}<span> · {bytes(entry.bytes)}</span>{/if}
                    {#if entry.request_timeout_seconds}<p>Idle transfer limit: {entry.request_timeout_seconds}s</p>{/if}
                </li>
            {/each}
        </ol>
    </details>
</section>
