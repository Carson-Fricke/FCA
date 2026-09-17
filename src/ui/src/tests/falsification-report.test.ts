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
			description: 'Requires at least 4680 5-min bars.'
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
		expect(screen.getByTestId('falsification-badge')).toHaveTextContent('7/7 Trials Survived');
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
