import { render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import HarnessExecutionCard from '$lib/components/chat/HarnessExecutionCard.svelte';
import BacktestToolCard from '$lib/components/chat/tool-results/BacktestToolCard.svelte';
import type { HarnessJobProgress, HarnessJobStatus, ToolResult } from '$lib/types';
import { getBacktest, getHarnessJob } from '$lib/api';

vi.mock('$lib/api', () => ({ getHarnessJob: vi.fn(), getBacktest: vi.fn(), backendKind: 'local', apiBaseUrl: '/api' }));

const reading: HarnessJobProgress = {
    stage: 'reading_data', symbol: 'MSFT', data_type: 'quote',
    start_date: '2026-02-12', end_date: '2026-08-28',
    sessions_ready: null, sessions_total: null, sessions_cached: null,
    rows: 54782541, bytes: 4516270000, request_timeout_seconds: 60,
    completed: false, started_at: '2026-10-09T23:59:37Z',
    updated_at: '2026-10-10T00:06:04Z', finished_at: '2026-10-10T00:06:04Z'
};

describe('shared harness execution status', () => {
    it('shows observed transfer volume, stage duration and truthful pre-execution failure', async () => {
        vi.mocked(getHarnessJob).mockResolvedValue({ job_id: 'job-1', state: 'failed',
            created_at: reading.started_at, updated_at: reading.updated_at,
            progress: [reading], error_message: 'idle read timed out after 60 seconds' });
        render(HarnessExecutionCard, { props: { jobId: 'job-1' } });
        await screen.findByText('Execution failed');
        expect(screen.getByText(/54,782,541 rows · 4.52 GB received/)).toBeInTheDocument();
        expect(screen.getByText(/No results were produced/)).toBeInTheDocument();
        expect(screen.getByText(/Idle transfer limit: 60s/)).toBeInTheDocument();
        expect(screen.getByText('idle read timed out after 60 seconds')).toBeInTheDocument();
        expect(getHarnessJob).toHaveBeenCalledWith('job-1');
    });

    it('renders cached session counts without deriving an ETA', async () => {
        vi.mocked(getHarnessJob).mockResolvedValue({ job_id: 'job-2', state: 'running', progress: [{
            ...reading, stage: 'preparing_data', sessions_ready: 97, sessions_total: 137,
            sessions_cached: 40, bytes: 0, finished_at: null
        }] });
        render(HarnessExecutionCard, { props: { jobId: 'job-2' } });
        await screen.findAllByText(/97 \/ 137 sessions ready/);
        expect(screen.getAllByText('97 / 137 sessions ready · 40 cached before acquisition').length).toBeGreaterThan(0);
        expect(screen.queryByText(/ETA/)).toBeNull();
    });

    it.each([
        ['computing_results', 'Computing results'],
        ['writing_artifacts', 'Writing results'],
        ['publishing_artifacts', 'Publishing results'],
    ] as const)('shows %s progress separately', async (stage, label) => {
        vi.mocked(getHarnessJob).mockResolvedValue({job_id: `results-${stage}`, state:'running',
            progress:[{...reading,stage,rows:stage==='publishing_artifacts'?0:reading.rows,bytes:stage==='publishing_artifacts'?2000000:0,finished_at:null}]});
        render(HarnessExecutionCard,{props:{jobId:`results-${stage}`}});
        await screen.findAllByText(label);
        if(stage==='publishing_artifacts') expect(screen.getByText(/2.0 MB uploaded/)).toBeInTheDocument();
    });

    it('ignores a stale response after navigation to a different job', async () => {
        let finish!: (job: HarnessJobStatus) => void;
        vi.mocked(getHarnessJob).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
            .mockResolvedValueOnce({ job_id: 'new-job', state: 'succeeded' });
        const view = render(HarnessExecutionCard, { props: { jobId: 'old-job' } });
        await view.rerender({ jobId: 'new-job' });
        await screen.findByText('Execution finished');
        finish({ job_id: 'old-job', state: 'failed', error_message: 'stale failure' });
        await waitFor(() => expect(screen.queryByText('stale failure')).toBeNull());
        expect(screen.getByText('new-job')).toBeInTheDocument();
    });
    it('surfaces a lost execution owner without continuing to display ingestion', async () => {
        vi.mocked(getHarnessJob).mockResolvedValue({job_id:'interrupted-job',state:'failed',error_message:'Execution worker disappeared; automatic replay is prohibited',progress:[{...reading,stage:'interrupted',symbol:null,data_type:null}]});
        render(HarnessExecutionCard,{props:{jobId:'interrupted-job'}});
        await screen.findAllByText('Execution interrupted');
        expect(screen.getByText('Execution worker disappeared; automatic replay is prohibited')).toBeInTheDocument();
        expect(screen.queryByText('Preparing market data')).toBeNull();
    });
    it('labels unavailable observations without inventing a running phase', async () => {
        vi.mocked(getHarnessJob).mockRejectedValue(new Error('harness unavailable'));
        render(HarnessExecutionCard, { props: { jobId: 'unavailable-job' } });
        await screen.findByText('Execution status unavailable');
        expect(screen.getByText(/Status unavailable: harness unavailable/)).toBeInTheDocument();
        expect(screen.queryByText('Preparing market data')).toBeNull();
    });

    it('keeps a backtest card observing beyond two minutes, recovers read failures and stops on navigation', async () => {
        vi.useFakeTimers();
        const id = 'long-backtest';
        vi.mocked(getHarnessJob).mockReset().mockResolvedValue({job_id:id,state:'running'});
        const result: ToolResult = {callId:'submit',datumType:'tool_result',failure:null,summary:'Accepted',text:'',tool:'run_backtest',ok:true,structured:{job_id:id,state:'queued'}};
        const view = render(BacktestToolCard,{props:{result,workspaceId:'research'}});
        try {
            await vi.advanceTimersByTimeAsync(125_000);
            expect(getHarnessJob).toHaveBeenCalledTimes(63);
            expect(screen.getByTestId('backtest-tool-card')).toHaveTextContent('running');
            expect(screen.queryByText(/Reopen the conversation/)).toBeNull();
            vi.mocked(getHarnessJob).mockRejectedValueOnce(new Error('temporary read failure'));
            await vi.advanceTimersByTimeAsync(2000);
            expect(screen.getByTestId('backtest-tool-card')).toHaveTextContent('temporary read failure');
            expect(screen.getByTestId('backtest-tool-card')).toHaveTextContent('running');
            await vi.advanceTimersByTimeAsync(2000);
            expect(screen.queryByText(/temporary read failure/)).toBeNull();
            expect(getBacktest).not.toHaveBeenCalled();
            view.unmount();
            const calls = vi.mocked(getHarnessJob).mock.calls.length;
            await vi.advanceTimersByTimeAsync(10_000);
            expect(getHarnessJob).toHaveBeenCalledTimes(calls);
        } finally { view.unmount(); vi.useRealTimers(); }
    });

    it('updates a queued backtest receipt to the observed terminal failure', async () => {
        const id='failed-backtest';
        vi.mocked(getHarnessJob).mockResolvedValue({job_id:id,state:'failed',error_message:'worker interrupted'});
        render(BacktestToolCard,{props:{workspaceId:'research',result:{callId:'submit',datumType:'tool_result',failure:null,summary:'Accepted',text:'',tool:'run_backtest',ok:true,structured:{job_id:id,state:'queued'}}}});
        await screen.findByText('worker interrupted');
        expect(screen.getByTestId('backtest-tool-card')).toHaveTextContent('failed');
        expect(screen.getByRole('link',{name:'Inspect execution →'})).toHaveAttribute('href',`/w/research/backtests/${id}`);
    });

});
