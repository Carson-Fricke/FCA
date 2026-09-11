import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * Focused coverage for T293: the hypothesis-validation surface. The api
 * module is mocked so the component tests observe only the launch call and
 * payloads; a separate case exercises the real fixture-mode client contract.
 */

const mocks = vi.hoisted(() => ({
	listValidations: vi.fn(),
	launchValidation: vi.fn(),
	getValidation: vi.fn(),
	notify: vi.fn()
}));

vi.mock('$lib/api', () => ({
	listValidations: mocks.listValidations,
	launchValidation: mocks.launchValidation,
	getValidation: mocks.getValidation
}));

vi.mock('$lib/workspace.svelte', () => ({ notify: mocks.notify }));

import ValidationPage from '../routes/w/[workspace]/validation/+page.svelte';
import { fixtures } from '$lib/fixtures';
import type { ValidationRecord, WorkspaceSnapshot } from '$lib/types';

const snapshot: WorkspaceSnapshot = {
	workspaces: fixtures.workspaces.items,
	overview: fixtures.workspaceSnapshot['alpha-lab'].overview,
	counts: fixtures.workspaceSnapshot['alpha-lab'].counts
};

const pageData = { workspaceId: 'alpha-lab', snapshot };

const seeded: ValidationRecord = {
	id: 'val-sample-1',
	workspace_id: 'alpha-lab',
	status: 'completed',
	claim: 'Momentum signals persist over a 20-day horizon',
	contract: {
		schema_version: 1,
		hypothesis: 'Ranking by trailing 20-day return yields a positive forward 5-day spread',
		metric: 'sharpe',
		threshold: 1.0,
		direction: 'increase',
		evidence_requirements: ['backtest with a declared in/out-of-sample split']
	},
	verdict: 'supported',
	evidence: [
		{
			id: 'ev-run',
			title: 'Momentum Breakout v3.2 — declared split',
			reference: 'backtests/bt-204',
			kind: 'backtest',
			run_id: 'bt-204',
			artifact: null
		},
		{
			id: 'ev-artifact',
			title: 'Metrics artifact',
			reference: 'backtests/run-1/artifacts/metrics.json',
			kind: 'artifact',
			run_id: 'bt-204',
			artifact: 'backtests/run-1/artifacts/metrics.json'
		}
	],
	created_at: '2026-09-08T18:04:00Z',
	updated_at: null
};

const launched: ValidationRecord = {
	...seeded,
	id: 'val-local-1',
	claim: 'Rates lead equities',
	verdict: 'inconclusive',
	evidence: [seeded.evidence[0]]
};

beforeEach(() => {
	vi.clearAllMocks();
});

describe('Validation launch surface (T293)', () => {
	it('launches a claim and renders the returned contract, verdict, and evidence', async () => {
		mocks.listValidations.mockResolvedValue([]);
		mocks.launchValidation.mockResolvedValue(launched);
		mocks.getValidation.mockResolvedValue(launched);

		render(ValidationPage, { props: { data: pageData } });
		await waitFor(() => expect(mocks.listValidations).toHaveBeenCalledWith('alpha-lab'));

		const input = screen.getByTestId('validation-claim') as HTMLTextAreaElement;
		await fireEvent.input(input, { target: { value: 'Rates lead equities' } });
		await fireEvent.click(screen.getByTestId('launch-validation'));

		await waitFor(() =>
			expect(mocks.launchValidation).toHaveBeenCalledWith('alpha-lab', 'Rates lead equities')
		);
		await waitFor(() =>
			expect(screen.getByTestId('validation-detail')).toHaveTextContent('Rates lead equities')
		);
		expect(mocks.notify).toHaveBeenCalledWith(expect.stringContaining('launched'));
		expect(screen.getByTestId('validation-verdict')).toHaveTextContent('inconclusive');
		expect(screen.getByTestId('validation-contract')).toHaveTextContent('sharpe');
	});

	it('links evidence to the producing run', async () => {
		mocks.listValidations.mockResolvedValue([seeded]);
		mocks.getValidation.mockResolvedValue(seeded);

		render(ValidationPage, { props: { data: pageData } });
		await waitFor(() => expect(screen.getByTestId('validation-detail')).toBeInTheDocument());
		expect(mocks.getValidation).toHaveBeenCalledWith('alpha-lab', 'val-sample-1');

		const evidence = screen.getByTestId('validation-evidence');
		expect(evidence).toHaveTextContent('Momentum Breakout v3.2');
		expect(evidence).toHaveTextContent('backtests/run-1/artifacts/metrics.json');

		const links = screen.getAllByTestId('evidence-link') as HTMLAnchorElement[];
		expect(links[0]).toHaveAttribute('href', '/w/alpha-lab/backtests/bt-204');
	});

	it('fixture mode resolves a deterministic sample with run-linked evidence', async () => {
		const actual = await vi.importActual<typeof import('$lib/api')>('$lib/api');
		const items = await actual.listValidations('alpha-lab');

		expect(items.length).toBeGreaterThanOrEqual(1);
		expect(items[0].id).toBe('val-sample-1');
		expect(items[0].verdict).toBe('supported');
		expect(items[0].evidence[0].run_id).toBe('bt-204');
		expect(items[0].contract?.metric).toBe('sharpe');
	});
});
