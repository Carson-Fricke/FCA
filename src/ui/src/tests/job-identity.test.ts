import { describe, expect, it } from 'vitest';
import {
	getBacktest,
	getHarnessJob,
	getHarnessJobResult,
	getScreenerResult,
	launchBacktest,
	listHarnessArtifacts,
	materializeBacktestRun
} from '$lib/api';

const JOB_ID = 'job-0a1b2c3d4e5f60718293a4b5c6d7e8f9';
const SCREEN_RESULT_ID =
	'sha256:7a8b9c0d1e2f30415263748596a7b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f';

describe('one job identity across the fixture contract', () => {
	it('reads one backtest detail under its job id with job-keyed artifacts', async () => {
		const detail = await getBacktest('alpha-lab', JOB_ID);
		expect(detail.job_id).toBe(JOB_ID);
		expect(detail.manifest.job_id).toBe(JOB_ID);
		expect('run_id' in detail).toBe(false);
		expect(detail.artifacts?.length).toBeGreaterThan(0);
		for (const artifact of detail.artifacts ?? []) {
			expect(artifact.object_path.startsWith(`jobs/${JOB_ID}/`)).toBe(true);
		}
	});

	it('materializes a completed card by the same job id without run or backtest aliases', async () => {
		const card = await materializeBacktestRun('alpha-lab', JOB_ID, {
			chatId: 'chat-1',
			executionId: 'exec-1'
		});
		expect(card.job_id).toBe(JOB_ID);
		expect(card.state).toBe('succeeded');
		expect('run_id' in card).toBe(false);
		expect('backtest_id' in card).toBe(false);
	});

	it('reads job status, job result, and artifacts on the shared job routes', async () => {
		expect((await getHarnessJob(JOB_ID)).job_id).toBe(JOB_ID);
		expect((await getHarnessJobResult(JOB_ID)).job_id).toBe(JOB_ID);
		expect((await listHarnessArtifacts(JOB_ID)).job_id).toBe(JOB_ID);
	});

	it('serves screening records under job_id while the frozen artifact reports succeeded', async () => {
		const result = await getScreenerResult('alpha-lab', SCREEN_RESULT_ID);
		expect(result.job_id).toBe('job-5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d');
		expect(result.status).toBe('succeeded');
		expect('run_id' in result).toBe(false);
	});

	it('launches a backtest with one job id and a locked terminal state', async () => {
		const outcome = await launchBacktest('alpha-lab', {
			bundleRef: 'ui-run',
			config: { shell: 'backtest' }
		});
		expect(outcome.job_id).toMatch(/^job-/);
		expect(outcome.state).toBe('succeeded');
	});
});
