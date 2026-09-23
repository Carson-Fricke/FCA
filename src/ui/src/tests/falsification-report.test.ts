import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import FalsificationReportCard from '$lib/components/chat/FalsificationReportCard.svelte';
import type { FalsificationReportData } from '$lib/transcript';

const mockReportData: FalsificationReportData = {
	type: 'falsification_report',
	content: 'Ran 7 falsification trial(s): 7 survived, 0 refuted.',
	passedCount: 7,
	falsifiedCount: 0,
	totalCount: 7,
	survived: true,
	hypothesis: {
		claim: 'MSTR underperforms IBIT with stationary residual spread',
		disagreement: 'Null predicts alpha=0, beta=1, and I(1) spread',
		universe: ['MSTR', 'IBIT', 'BTC'],
		horizon: '5m RTH'
	},
	trials: [
		{
			testId: 'T01_insufficient_bars',
			falsifierId: 'insufficient_bars',
			observable: 'n_bars',
			condition: 'lt',
			threshold: 4680,
			observed: 25000,
			falsified: false,
			description: 'Requires at least 4680 5-min bars.',
			detail: 'Observed 25000 5-min bars across the evaluation window.',
			runId: 'run_t01',
			runState: 'completed'
		},
		{
			testId: 'T02_no_underperformance_alpha',
			falsifierId: 'no_underperformance_alpha',
			observable: 'alpha',
			condition: 'gte',
			threshold: -0.000005,
			observed: -0.000012,
			falsified: false,
			description: 'Alpha must remain strictly negative.'
		},
		{
			testId: 'T03_no_mean_reversion_adf',
			falsifierId: 'no_mean_reversion_adf',
			observable: 'adf_t_stat',
			condition: 'gt',
			threshold: -2.57,
			observed: -3.45,
			falsified: false,
			description: 'ADF t-statistic on residual spread must beat -2.57.'
		}
	],
	controls: [
		{
			controlId: 'C01_shuffled_ibit',
			description: 'Within-day shuffled IBIT returns',
			expectation: 'alpha centered at 0, no stationarity'
		}
	],
	metrics: {
		alpha: -0.000012,
		beta: 1.25,
		adf_t_stat: -3.45,
		half_life: 42.0,
		hit_rate: 0.582
	}
};

describe('FalsificationReportCard', () => {
	it('renders summary banner, badge, and key metrics', () => {
		render(FalsificationReportCard, { props: { data: mockReportData } });

		expect(screen.getByTestId('falsification-report-card')).toBeInTheDocument();
		// The mock carries stale backend counts (7/7) next to 3 rendered trials;
		// the badge must report what is actually rendered.
		expect(screen.getByTestId('falsification-badge')).toHaveTextContent('3/3 Trials Survived');
		expect(screen.getByTestId('falsification-claim')).toHaveTextContent(
			'MSTR underperforms IBIT'
		);
		expect(screen.getByTestId('metric-chips')).toBeInTheDocument();
	});

	it('renders Scorecard tab with bullet gauge bars by default', () => {
		render(FalsificationReportCard, { props: { data: mockReportData } });

		expect(screen.getByTestId('scorecard-view')).toBeInTheDocument();
		const statuses = screen.getAllByTestId('trial-status');
		expect(statuses).toHaveLength(3);
		expect(statuses[0]).toHaveTextContent('SURVIVED');
		expect(screen.getByText('T01_insufficient_bars')).toBeInTheDocument();
		expect(screen.getByText('T02_no_underperformance_alpha')).toBeInTheDocument();
	});

	it('renders per-trial evidence: observed value, condition, detail, and run state', () => {
		render(FalsificationReportCard, { props: { data: mockReportData } });

		const row = screen.getByTestId('trial-row-T01_insufficient_bars');
		expect(row).toBeInTheDocument();
		expect(row).toHaveTextContent('T01_insufficient_bars');
		expect(row).toHaveTextContent('observed 25000.00');
		expect(row).toHaveTextContent('falsifies when lt 4680.00');
		expect(screen.getByTestId('trial-detail')).toHaveTextContent(
			'Observed 25000 5-min bars across the evaluation window.'
		);
		expect(screen.getByTestId('trial-run')).toHaveTextContent('completed');
		expect(screen.getByTestId('trial-run')).toHaveTextContent('run run_t01');
	});

	it('renders a refuted badge and loss-styled observed value for a falsified trial', () => {
		const refutedReport: FalsificationReportData = {
			...mockReportData,
			trials: [
				{
					testId: 'T04_alpha_fired',
					falsifierId: 'no_underperformance_alpha',
					observable: 'alpha',
					condition: 'gte',
					threshold: -0.000005,
					observed: 0.0004,
					falsified: true,
					detail: 'Positive alpha contradicts the claimed underperformance.'
				}
			]
		};

		render(FalsificationReportCard, { props: { data: refutedReport } });

		const row = screen.getByTestId('trial-row-T04_alpha_fired');
		expect(screen.getByTestId('trial-status')).toHaveTextContent('REFUTED');
		expect(screen.getByTestId('trial-observed')).toHaveTextContent('observed 0.0004');
		expect(row).toHaveTextContent('Positive alpha contradicts the claimed underperformance.');
	});

	it('distinguishes no evidence (observed null) from a fired zero observation', () => {
		const mixedReport: FalsificationReportData = {
			...mockReportData,
			trials: [
				{
					testId: 'T05_pending',
					falsifierId: 'f_pending',
					observable: 'hit_rate',
					condition: 'lt',
					threshold: 0.5,
					observed: null,
					falsified: false
				},
				{
					testId: 'T06_zero_fired',
					falsifierId: 'f_zero',
					observable: 'hit_rate',
					condition: 'lt',
					threshold: 0.5,
					observed: 0,
					falsified: true
				}
			]
		};

		render(FalsificationReportCard, { props: { data: mixedReport } });

		const pendingRow = screen.getByTestId('trial-row-T05_pending');
		expect(pendingRow).toHaveTextContent('NO EVIDENCE');
		expect(pendingRow).toHaveTextContent('no observation recorded');
		expect(pendingRow).not.toHaveTextContent('0.0000');

		const zeroRow = screen.getByTestId('trial-row-T06_zero_fired');
		expect(zeroRow).toHaveTextContent('REFUTED');
		expect(zeroRow).toHaveTextContent('observed 0.0000');
		expect(zeroRow).not.toHaveTextContent('no observation recorded');
	});

	it('derives summary counts from the rendered trial rows, not backend tallies', () => {
		const dishonestCounts: FalsificationReportData = {
			...mockReportData,
			passedCount: 4,
			falsifiedCount: 0,
			totalCount: 4,
			survived: true,
			trials: [
				{
					testId: 'T07_pass',
					falsifierId: 'f1',
					observable: 'alpha',
					condition: 'lt',
					threshold: 0,
					observed: -0.01,
					falsified: false
				},
				{
					testId: 'T08_refuted',
					falsifierId: 'f2',
					observable: 'beta',
					condition: 'lt',
					threshold: 1,
					observed: 1.4,
					falsified: true
				},
				{
					testId: 'T09_pending',
					falsifierId: 'f3',
					observable: 'half_life',
					condition: 'gt',
					threshold: 0,
					observed: null,
					falsified: false
				}
			]
		};

		render(FalsificationReportCard, { props: { data: dishonestCounts } });

		expect(screen.getByTestId('falsification-badge')).toHaveTextContent('1/3 Refuted');
		const statuses = screen.getAllByTestId('trial-status');
		expect(statuses).toHaveLength(3);
		expect(statuses[1]).toHaveTextContent('REFUTED');
		expect(statuses[2]).toHaveTextContent('NO EVIDENCE');
	});

	it('shows control specifications without inventing a null distribution', async () => {
		render(FalsificationReportCard, { props: { data: mockReportData } });

		await fireEvent.click(screen.getByTestId('tab-controls'));
		expect(screen.getByTestId('controls-view')).toBeInTheDocument();
		expect(screen.getByText(/No empirical null-distribution samples/)).toBeInTheDocument();
		expect(screen.getByText('C01_shuffled_ibit')).toBeInTheDocument();
	});

	it('shows actual scalar evidence when no dynamics series was recorded', async () => {
		render(FalsificationReportCard, { props: { data: mockReportData } });

		await fireEvent.click(screen.getByTestId('tab-evidence'));
		expect(screen.getByTestId('evidence-view')).toBeInTheDocument();
		expect(screen.getByText(/No time-series dynamics were recorded/)).toBeInTheDocument();
		expect(screen.getAllByText('Alpha').length).toBeGreaterThan(0);
	});

	it('renders only time-series evidence supplied by the backend artifact', async () => {
		const reportWithSeries: FalsificationReportData = {
			...mockReportData,
			series: [
				{
					name: 'Observed rolling statistic',
					data: [
						{ t: 1, v: 0.1 },
						{ t: 2, v: 0.2 }
					]
				}
			]
		};

		render(FalsificationReportCard, { props: { data: reportWithSeries } });

		await fireEvent.click(screen.getByTestId('tab-evidence'));
		expect(screen.getByText('Observed rolling statistic')).toBeInTheDocument();
		expect(screen.queryByText(/No time-series dynamics were recorded/)).toBeNull();
	});
});
