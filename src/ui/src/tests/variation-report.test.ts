import { render, screen, within } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import PhaseRunView from '$lib/components/chat/PhaseRunView.svelte';
import ScopeTranscript from '$lib/components/chat/ScopeTranscript.svelte';
import VariationReportCard from '$lib/components/chat/VariationReportCard.svelte';
import { isSucceededState, runStateTone } from '$lib/components/charts/parameter-bar';
import { buildNodeEntries, decodeVariationReport } from '$lib/transcript';
import type { ExecutionEvent, ExecutionEventType } from '$lib/types';

/**
 * One deterministic one-at-a-time neighborhood around a three-key center:
 * numeric lookback, boolean use_filter, categorical mode. job-filter has no
 * metrics at all, job-slow is still running with a missing profit factor, and
 * job-40 failed with an error.
 */
const payload = {
	type: 'variation',
	center: {
		parameters: { lookback: 20, use_filter: false, mode: 'balanced' },
		jobId: 'job-center',
		state: 'succeeded',
		metrics: { total_return: 0.12, profit_factor: 1.4 }
	},
	points: [
		{
			parameters: { lookback: 10, use_filter: false, mode: 'balanced' },
			jobId: 'job-10',
			state: 'succeeded',
			metrics: { total_return: 0.08, profit_factor: 1.2 }
		},
		{
			parameters: { lookback: 30, use_filter: false, mode: 'balanced' },
			jobId: 'job-30',
			state: 'succeeded',
			metrics: { total_return: 0.18, profit_factor: 1.6 }
		},
		{
			parameters: { lookback: 20, use_filter: true, mode: 'balanced' },
			jobId: 'job-filter',
			state: 'succeeded',
			metrics: null
		},
		{
			parameters: { lookback: 20, use_filter: false, mode: 'slow' },
			jobId: 'job-slow',
			state: 'running',
			metrics: { total_return: 0.11, profit_factor: null }
		},
		{
			parameters: { lookback: 20, use_filter: false, mode: 'fast' },
			jobId: 'job-fast',
			state: 'succeeded',
			metrics: { total_return: 0.15, profit_factor: 1.5 }
		},
		{
			parameters: { lookback: 40, use_filter: false, mode: 'balanced' },
			jobId: 'job-40',
			state: 'failed',
			error: 'engine crashed',
			metrics: null
		}
	],
	parameters: ['lookback', 'use_filter', 'mode'],
	stability: 'Plateaus on lookback; isolated peak on mode=fast.'
};

const decoded = decodeVariationReport(payload);

function rows(chart: HTMLElement): HTMLElement[] {
	return within(chart).getAllByTestId('parameter-bar-row');
}

function row(chart: HTMLElement, level: string): HTMLElement {
	const found = rows(chart).find((candidate) => candidate.dataset.level === level);
	if (!found) throw new Error(`No ${level} row in chart ${chart.dataset.testid}`);
	return found;
}

describe('VariationReportCard', () => {
	it('renders one chart per varied parameter with both metrics and the center marker', () => {
		render(VariationReportCard, { props: { data: decoded! } });

		expect(screen.getByTestId('variation-report-card')).toBeInTheDocument();
		const lookback = screen.getByTestId('variation-chart-lookback');
		const legend = within(lookback).getByTestId('variation-chart-lookback-legend');
		expect(legend).toHaveTextContent('Total return');
		expect(legend).toHaveTextContent('Profit factor');
		expect(rows(lookback).map((candidate) => candidate.dataset.level)).toEqual(['10', '20', '30', '40']);

		const center = row(lookback, '20');
		expect(center.dataset.center).toBe('true');
		expect(within(center).getByTestId('parameter-bar-center')).toHaveTextContent('center');
		expect(within(center).getByTestId('parameter-bar-total-return')).toHaveTextContent('0.1200');
		expect(within(center).getByTestId('parameter-bar-profit-factor')).toHaveTextContent('1.40');

		expect(screen.getByTestId('variation-center')).toHaveTextContent('job-center');
		expect(screen.getByTestId('variation-stability')).toHaveTextContent('isolated peak');
	});

	it('plots only the points that vary the parameter', () => {
		render(VariationReportCard, { props: { data: decoded! } });
		expect(rows(screen.getByTestId('variation-chart-lookback'))).toHaveLength(4);
		expect(rows(screen.getByTestId('variation-chart-use_filter'))).toHaveLength(2);
		expect(rows(screen.getByTestId('variation-chart-mode'))).toHaveLength(3);
	});

	it('orders numeric levels numerically and categorical levels by first appearance', () => {
		render(VariationReportCard, { props: { data: decoded! } });
		expect(rows(screen.getByTestId('variation-chart-use_filter')).map((candidate) => candidate.dataset.level)).toEqual([
			'false',
			'true'
		]);
		expect(rows(screen.getByTestId('variation-chart-use_filter'))[0].dataset.center).toBe('true');
		const mode = screen.getByTestId('variation-chart-mode');
		expect(rows(mode).map((candidate) => candidate.dataset.level)).toEqual(['balanced', 'slow', 'fast']);
		expect(row(mode, 'balanced').dataset.center).toBe('true');
	});

	it('renders missing metrics as explicit gaps, never as zero', () => {
		render(VariationReportCard, { props: { data: decoded! } });
		const filterTrue = row(screen.getByTestId('variation-chart-use_filter'), 'true');
		const missingTotal = within(filterTrue).getByTestId('parameter-bar-total-return');
		const missingFactor = within(filterTrue).getByTestId('parameter-bar-profit-factor');
		expect(missingTotal).toHaveAttribute('data-missing', 'true');
		expect(missingTotal).toHaveTextContent('no data');
		expect(missingTotal).not.toHaveTextContent('0');
		expect(missingFactor).toHaveAttribute('data-missing', 'true');
		expect(missingFactor).toHaveTextContent('no data');

		const slow = row(screen.getByTestId('variation-chart-mode'), 'slow');
		expect(within(slow).getByTestId('parameter-bar-total-return')).toHaveTextContent('0.1100');
		expect(within(slow).getByTestId('parameter-bar-profit-factor')).toHaveAttribute('data-missing', 'true');
	});

	it('distinguishes failed, running, and succeeded points', () => {
		render(VariationReportCard, { props: { data: decoded! } });
		const failed = row(screen.getByTestId('variation-chart-lookback'), '40');
		expect(failed.dataset.state).toBe('failed');
		expect(within(failed).getByTestId('parameter-bar-state')).toHaveTextContent('failed');
		expect(within(failed).getByTestId('parameter-bar-state').className).toContain('text-loss');

		const succeeded = row(screen.getByTestId('variation-chart-lookback'), '20');
		expect(succeeded.dataset.state).toBe('succeeded');
		expect(within(succeeded).getByTestId('parameter-bar-state').className).toContain('text-profit');

		const running = row(screen.getByTestId('variation-chart-mode'), 'slow');
		expect(running.dataset.state).toBe('running');
		expect(within(running).getByTestId('parameter-bar-state').className).toContain('text-accent');

		expect(screen.getByTestId('variation-state-summary')).toHaveTextContent('4/6 points succeeded');
		expect(screen.getByTestId('variation-summary')).toHaveTextContent('3 parameters · 6 points');
		expect(screen.getByTestId('variation-center-state')).toHaveTextContent('succeeded');
	});

	it('renders an explicit empty state instead of inventing charts', () => {
		const empty = decodeVariationReport({ type: 'variation' });
		render(VariationReportCard, { props: { data: empty! } });
		expect(screen.getByTestId('variation-empty')).toHaveTextContent('No varied parameters');
		expect(screen.queryByTestId('variation-charts')).toBeNull();
	});
});

describe('parameter bar state vocabulary', () => {
	it('maps exactly the harness job states and keeps retired aliases unknown', () => {
		for (const state of ['succeeded', 'queued', 'running', 'failed', 'cancelled']) {
			expect(runStateTone(state)).not.toBe('text-warn');
		}
		expect(runStateTone('succeeded')).toBe('text-profit');
		expect(runStateTone('failed')).toBe('text-loss');
		expect(runStateTone('cancelled')).toBe('text-loss');
		expect(runStateTone('queued')).toBe('text-accent');
		expect(runStateTone('running')).toBe('text-accent');
		for (const state of ['complete', 'completed', 'pending', 'error', '', null, undefined]) {
			expect(runStateTone(state)).toBe('text-warn');
			expect(isSucceededState(state)).toBe(false);
		}
		expect(isSucceededState('succeeded')).toBe(true);
	});
});

describe('decodeVariationReport', () => {
	it('decodes the frozen payload shape', () => {
		expect(decoded).not.toBeNull();
		expect(decoded!.center?.parameters).toEqual({ lookback: 20, use_filter: false, mode: 'balanced' });
		expect(decoded!.center?.metrics).toEqual({ total_return: 0.12, profit_factor: 1.4 });
		expect(decoded!.points).toHaveLength(6);
		expect(decoded!.points[3].state).toBe('running');
		expect(decoded!.points[5].error).toBe('engine crashed');
		expect(decoded!.parameters).toEqual(['lookback', 'use_filter', 'mode']);
		expect(decoded!.stability).toContain('Plateaus');
	});

	it('rejects values that are not variation artifacts', () => {
		for (const value of [null, undefined, 42, 'variation', [], {}, { type: 'falsification_report' }]) {
			expect(decodeVariationReport(value)).toBeNull();
		}
	});

	it('skips malformed points and degrades malformed fields to explicit gaps', () => {
		const data = decodeVariationReport({
			type: 'variation',
			center: {
				parameters: { alpha: 1, beta: 'x', drop: { nested: true }, bad: Number.POSITIVE_INFINITY },
				jobId: '',
				state: 7,
				metrics: { total_return: 'NaN', profit_factor: Number.POSITIVE_INFINITY, trade_count: 12 },
				error: null
			},
			points: [null, 'x', 5, { parameters: { alpha: 2 }, metrics: { total_return: Number.NaN } }, { parameters: 'nope' }],
			parameters: ['alpha', 'alpha', '', 3, null],
			stability: 5
		});
		expect(data).not.toBeNull();
		expect(data!.center?.parameters).toEqual({ alpha: 1, beta: 'x' });
		expect(data!.center?.jobId).toBeNull();
		expect(data!.center?.state).toBeNull();
		expect(data!.center?.metrics).toEqual({ total_return: null, profit_factor: null, trade_count: 12 });
		expect(data!.points).toEqual([
			{
				parameters: { alpha: 2 },
				jobId: null,
				state: null,
				metrics: { total_return: null },
				error: null
			},
			{ parameters: {}, jobId: null, state: null, metrics: {}, error: null }
		]);
		expect(data!.parameters).toEqual(['alpha']);
		expect(data!.stability).toBe('');
	});

	it('derives missing parameter names from observed vectors without inventing values', () => {
		const data = decodeVariationReport({
			type: 'variation',
			center: { parameters: { alpha: 1 } },
			points: [{ parameters: { alpha: 2, beta: true } }]
		});
		expect(data!.parameters).toEqual(['alpha', 'beta']);
		expect(data!.center?.metrics).toEqual({});
		expect(data!.center?.jobId).toBeNull();
		expect(data!.stability).toBe('');
	});
});

describe('variation artifact transport', () => {
	function event(
		cursor: number,
		eventType: ExecutionEventType,
		eventPayload: Record<string, unknown>
	): ExecutionEvent {
		return {
			actorId: 'thesis-workflow',
			actorScope: 'workflow',
			correlationId: 'exec-1',
			cursor,
			eventId: `evt-${cursor}`,
			eventType,
			executionId: 'exec-1',
			intentEpoch: 0,
			lateResult: false,
			occurredAt: `2026-01-01T00:00:${String(cursor).padStart(2, '0')}Z`,
			payload: eventPayload,
			workspaceId: 'ws-1'
		};
	}

	it('renders through the phase run view when a variation phase result is present', () => {
		render(PhaseRunView, {
			props: {
				attempts: [
					{
						attempt: 1,
						progress: [{ text: 'submitted variation batch', at: '2026-01-01T00:00:01Z' }],
						result: { summary: 'Variation complete', artifact: payload },
						error: null
					}
				]
			}
		});
		expect(screen.getByTestId('phase-run-status')).toHaveTextContent('Completed');
		expect(screen.getByTestId('variation-report-card')).toBeInTheDocument();
	});

	it('renders through the scope transcript from a decoded output artifact', () => {
		const entries = buildNodeEntries({
			messages: [],
			events: [event(1, 'sub_agent.output', { text: 'Variation report', artifact: payload })]
		});
		expect(entries.filter((entry) => entry.kind === 'variation')).toHaveLength(1);
		render(ScopeTranscript, { props: { entries, workspaceId: 'ws-1' } });
		expect(screen.getByTestId('variation-report-card')).toBeInTheDocument();
		expect(screen.getByTestId('variation-chart-mode')).toBeInTheDocument();
	});
});
