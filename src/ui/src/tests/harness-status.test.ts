import { describe, expect, it } from 'vitest';

/**
 * Fixture-mode coverage for the harness topology/MCP surface added for the
 * frontend wiring: connection status, job polling, and the canonical artifact
 * manifest shape. The real-fetch paths are exercised against the Thesis routes
 * by the Python suite; here the exported fixture contracts are guarded.
 */
import {
	getHarnessRun,
	getHarnessStatus,
	listHarnessArtifacts,
	pollHarnessJob
} from '$lib/api';

describe('harness MCP surface (fixture mode)', () => {
	it('reports the resolved topology and published tool catalog', async () => {
		const status = await getHarnessStatus();

		expect(status.mode).toBe('local');
		expect(status.harness_location).toBe('local');
		expect(status.reachable).toBe(true);
		expect(status.error).toBeNull();
		expect(status.tools.map((tool) => tool.name)).toContain('run_backtest');
		expect(status.tools.map((tool) => tool.name)).toContain('list_artifacts');
	});

	it('resolves a run and its canonical artifact manifest', async () => {
		const run = await getHarnessRun('run-1');
		expect(run.run_id).toBe('run-1');

		const manifest = await listHarnessArtifacts('run-1');
		expect(manifest.run_id).toBe('run-1');
		expect(manifest.artifacts).toEqual([]);
	});

	it('polls a job to a terminal state', async () => {
		const status = await pollHarnessJob('job-1', { intervalMs: 1, maxAttempts: 2 });

		expect(status.job_id).toBe('job-1');
		expect(status.state).toBe('completed');
	});
});
