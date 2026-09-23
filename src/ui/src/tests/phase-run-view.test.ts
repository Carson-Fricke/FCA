import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import PhaseRunView from '$lib/components/chat/PhaseRunView.svelte';
import type { FalsificationReportData } from '$lib/transcript';

const falsificationArtifact = {
	type: 'falsification_report',
	content: 'Ran 2 falsification trial(s): 2 survived, 0 refuted.',
	passedCount: 2,
	falsifiedCount: 0,
	totalCount: 2,
	survived: true,
	hypothesis: { claim: 'Five-day momentum in SPY continues.', horizon: '5d' },
	trials: [
		{
			testId: 'T01',
			falsifierId: 'f1',
			observable: 'sharpe',
			condition: 'lt',
			threshold: 0,
			observed: 1.2,
			falsified: false,
			description: 'Run the momentum probe and require a negative Sharpe.'
		},
		{
			testId: 'T02',
			falsifierId: 'f2',
			observable: 'sharpe',
			condition: 'lt',
			threshold: 0,
			observed: 0.8,
			falsified: false,
			description: 'Re-run the probe out of sample.'
		}
	]
} as unknown as FalsificationReportData;

describe('PhaseRunView', () => {
	it('renders the run log of progress lines', () => {
		render(PhaseRunView, {
			props: {
				progress: [
					{ text: 'Composing the falsification plan.', at: '2026-09-20T12:00:00Z' },
					{ text: 'Running the research model.', at: '2026-09-20T12:01:00Z' }
				]
			}
		});

		expect(screen.getByTestId('phase-run-view')).toBeInTheDocument();
		expect(screen.getByTestId('phase-run-log')).toBeInTheDocument();
		const lines = screen.getAllByTestId('phase-progress-line');
		expect(lines).toHaveLength(2);
		expect(lines[0]).toHaveTextContent('Composing the falsification plan.');
		expect(lines[1]).toHaveTextContent('Running the research model.');
		expect(screen.getByTestId('phase-run-status')).toHaveTextContent('Running');
		// The view is read-only: no composer, no inputs.
		expect(screen.queryByRole('textbox')).toBeNull();
	});

	it('renders a falsification result through the typed card', () => {
		render(PhaseRunView, {
			props: {
				progress: [{ text: 'Trials recorded.', at: '2026-09-20T12:02:00Z' }],
				result: {
					summary: 'Ran 2 planned trials: 2 survived.',
					artifact: falsificationArtifact as unknown as Record<string, unknown>
				}
			}
		});

		expect(screen.getByTestId('phase-run-result')).toBeInTheDocument();
		expect(screen.getByTestId('falsification-report-card')).toBeInTheDocument();
		expect(screen.getByTestId('falsification-badge')).toHaveTextContent(
			'2/2 Trials Survived'
		);
		expect(screen.getByTestId('phase-run-status')).toHaveTextContent('Completed');
	});

	it('renders an error variant when the phase failed', () => {
		render(PhaseRunView, {
			props: {
				error: { message: 'the falsification model run failed' }
			}
		});

		expect(screen.getByTestId('phase-run-error')).toBeInTheDocument();
		expect(screen.getByTestId('phase-run-error')).toHaveTextContent(
			'the falsification model run failed'
		);
		expect(screen.queryByTestId('phase-run-result')).toBeNull();
		expect(screen.queryByTestId('falsification-report-card')).toBeNull();
		expect(screen.getByTestId('phase-run-status')).toHaveTextContent('Failed');
	});

	it('threads an error artifact through the same error variant', () => {
		render(PhaseRunView, {
			props: {
				progress: [{ text: 'Falsification run aborted.', at: '2026-09-20T12:03:00Z' }],
				result: {
					summary: 'The falsification phase failed.',
					artifact: { type: 'error', message: 'falsification model crashed: no bars returned' }
				}
			}
		});

		expect(screen.getByTestId('phase-run-error')).toBeInTheDocument();
		expect(screen.getByTestId('phase-run-error-message')).toHaveTextContent(
			'falsification model crashed: no bars returned'
		);
		expect(screen.queryByTestId('phase-run-result')).toBeNull();
		expect(screen.queryByTestId('falsification-report-card')).toBeNull();
		expect(screen.getByTestId('phase-run-status')).toHaveTextContent('Failed');
	});

	it('prefers the failed-computation error record over an error artifact', () => {
		render(PhaseRunView, {
			props: {
				error: { message: 'run record: model exited non-zero' },
				result: {
					summary: 'The falsification phase failed.',
					artifact: { type: 'error', message: 'artifact-level message' }
				}
			}
		});

		expect(screen.getByTestId('phase-run-error')).toHaveTextContent(
			'run record: model exited non-zero'
		);
		expect(screen.queryByText('artifact-level message')).toBeNull();
	});

	it('renders a plain summary for a non-falsification artifact', () => {
		render(PhaseRunView, {
			props: {
				result: {
					summary: 'The optimization backtest completed.',
					artifact: { type: 'backtest_result', metrics: { sharpe: 1.1 } }
				}
			}
		});

		expect(screen.getByTestId('phase-run-result')).toBeInTheDocument();
		expect(screen.getByTestId('phase-run-result')).toHaveTextContent(
			'The optimization backtest completed.'
		);
		expect(screen.queryByTestId('falsification-report-card')).toBeNull();
	});
});
