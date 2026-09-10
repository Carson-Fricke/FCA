import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it } from 'vitest';
import ScreenerPanel from '$lib/components/screener/ScreenerPanel.svelte';
import { fixtures } from '$lib/fixtures';
import type { ScreenerSavedDefinition } from '$lib/types';

/** Real-contract saved definition (ScreenerSavedDefinition shape). */
const etfScreen: ScreenerSavedDefinition = {
	screener_id: 'scr-9',
	definition: {
		schema_version: 1,
		name: 'ETF liquidity core',
		universe_revision: 'universe:us-listed@2026-09',
		lookback_days: 20,
		filters: [
			{ kind: 'security_type', params: { value: 'etf' } },
			{ kind: 'avg_dollar_volume_min', params: { min: 500000000 } }
		]
	},
	definition_hash: '9f2c88a1b0d34e5f6a7b8c9d0e1f2a3b',
	artifact_path: 'screeners/definitions/etf-liquidity-core.json'
};

function paramInput(container: HTMLElement, name: string): HTMLElement {
	return container.querySelector(
		`[data-testid="filter-param"][data-param="${name}"]`
	) as HTMLElement;
}

describe('Screener filter builder (declarative contract)', () => {
	it('adds and removes filter rows with per-kind parameter editors', async () => {
		render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();

		expect(screen.getAllByTestId('filter-row')).toHaveLength(1);
		await fireEvent.click(screen.getByTestId('add-filter'));
		await tick();
		expect(screen.getAllByTestId('filter-row')).toHaveLength(2);

		await fireEvent.click(screen.getAllByTestId('remove-filter')[0]);
		await tick();
		expect(screen.getAllByTestId('filter-row')).toHaveLength(1);
	});

	it('surfaces declaration-time validation and blocks the run', async () => {
		const { container } = render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();

		await fireEvent.change(screen.getByTestId('filter-kind-select'), {
			target: { value: 'rsi_below' }
		});
		await tick();
		expect(paramInput(container, 'period')).toHaveValue(14);

		await fireEvent.input(paramInput(container, 'threshold'), { target: { value: '150' } });
		await tick();
		expect(screen.getByTestId('filter-row-error')).toHaveTextContent('within [0, 100]');
		expect(screen.getByTestId('screener-run')).toBeDisabled();

		await fireEvent.input(paramInput(container, 'threshold'), { target: { value: '30' } });
		await tick();
		expect(screen.queryByTestId('filter-row-error')).toBeNull();
		expect(screen.getByTestId('screener-run')).toBeEnabled();
	});

	it('runs with the full filter set and renders provenance-backed results', async () => {
		const { container } = render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();

		expect(screen.queryByTestId('screener-row')).toBeNull();
		const asOfInput = screen.getByTestId('screener-asof') as HTMLInputElement;
		expect(asOfInput.value).toMatch(/^\d{4}-\d{2}-\d{2}$/);

		await fireEvent.click(screen.getByTestId('screener-run'));
		await waitFor(() => {
			expect(screen.getAllByTestId('screener-row')).toHaveLength(
				fixtures.screenerResults.items.length
			);
		});
		expect(
			container.querySelector('[data-testid="screener-row"][data-symbol="SPY"]')
		).not.toBeNull();
		const provenance = screen.getByTestId('screener-provenance');
		expect(provenance).toHaveTextContent('Ad-hoc screen');
		expect(provenance).toHaveTextContent('fixture-defi');
	});
});

describe('Screener save flow (workspace artifacts)', () => {
	it('saves the validated filter payload and lists the study', async () => {
		render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();

		expect(screen.getByTestId('screener-save-button')).toBeDisabled();
		await fireEvent.input(screen.getByTestId('screener-name-input'), {
			target: { value: 'Overnight gap study' }
		});
		await tick();
		expect(screen.getByTestId('screener-save-button')).toBeEnabled();

		await fireEvent.click(screen.getByTestId('screener-save-button'));
		await waitFor(() => {
			expect(screen.getByTestId('saved-screeners')).toHaveTextContent('Overnight gap study');
		});
		// The saved study chip renders the engine-native filter summary.
		expect(screen.getByTestId('saved-screeners')).toHaveTextContent('avg $vol ≥ 250,000,000');
	});
});

describe('Saved studies (ScreenerSavedDefinition contract)', () => {
	it('lists saved studies with filter summaries and definition-hash provenance', async () => {
		render(ScreenerPanel, { props: { workspaceId: 'alpha-lab', initialSaved: [etfScreen] } });
		await tick();

		const list = screen.getByTestId('saved-screeners');
		expect(list).toHaveTextContent('ETF liquidity core');
		expect(list).toHaveTextContent('type = etf');
		expect(list).toHaveTextContent('avg $vol ≥ 500,000,000');
		expect(list).toHaveTextContent('9f2c88a1b0d3');
	});

	it('loads a saved study back into the builder with hydrated parameters', async () => {
		const { container } = render(ScreenerPanel, {
			props: { workspaceId: 'alpha-lab', initialSaved: [etfScreen] }
		});
		await tick();

		await fireEvent.click(screen.getByTestId('load-study'));
		await tick();

		expect(screen.getAllByTestId('filter-row')).toHaveLength(2);
		expect(screen.getByTestId('screener-name-input')).toHaveValue('ETF liquidity core');
		expect(paramInput(container, 'value')).toHaveValue('etf');
		expect(paramInput(container, 'min')).toHaveValue(500000000);
	});

	it('re-runs a saved study as-is at the current as-of date', async () => {
		render(ScreenerPanel, { props: { workspaceId: 'alpha-lab', initialSaved: [etfScreen] } });
		await tick();

		await fireEvent.click(screen.getByTestId('rerun-study'));
		await waitFor(() => {
			expect(screen.getByTestId('screener-provenance')).toHaveTextContent('ETF liquidity core');
		});
	});
});

describe('Custom Python screen editor', () => {
	it('renders the boundary-clean template and flags violations inline', async () => {
		render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();
		expect(screen.getByTestId('script-clean')).toBeInTheDocument();

		await fireEvent.input(screen.getByTestId('script-source'), {
			target: { value: 'import os as o\ndef screen(ctx):\n    return ctx.__class__\n' }
		});
		await tick();
		const errors = screen.getByTestId('script-errors');
		expect(errors).toHaveTextContent('outside the allowlist');
		expect(errors).toHaveTextContent('import aliases are not permitted');
		expect(errors).toHaveTextContent('dunder attribute access is not permitted');
	});

	it('mirrors SIC exactly-one validation in the builder', async () => {
		render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();

		await fireEvent.change(screen.getByTestId('filter-kind-select'), {
			target: { value: 'sic_industry' }
		});
		await tick();
		expect(screen.getByTestId('filter-row-error')).toHaveTextContent('exactly one');
	});
});

describe('Fixture contract translation (screening routes)', () => {
	it('translates saved fixture rows into the real saved-definition contract', async () => {
		const { listScreeners } = await import('$lib/api');
		const items = await listScreeners('alpha-lab');
		expect(items.length).toBeGreaterThanOrEqual(2);
		const first = items[0];
		expect(first.definition.schema_version).toBe(1);
		expect(first.definition.name).toBe('Liquid large caps');
		expect(first.definition.filters[0].kind).toBe('avg_dollar_volume_min');
		expect(first.definition_hash).toBe('fixture-scr-1');
	});
});