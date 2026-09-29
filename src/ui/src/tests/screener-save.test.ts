import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it } from 'vitest';
import ScreenerPanel from '$lib/components/screener/ScreenerPanel.svelte';
import { generateScreenerSource, lintScreenerSource, parseFilterRows } from '$lib/components/screener/screenerSource';
import { fixtures } from '$lib/fixtures';
import {
	getScreenerResult,
	listScreeners,
	runScreener,
	saveScreener,
	ThesisApiError
} from '$lib/api';
import type { ScreenerDefinition } from '$lib/types';

const definitions = fixtures.screeners['alpha-lab'].items as ScreenerDefinition[];

function fixtureDefinition(name: string): ScreenerDefinition {
	const found = definitions.find((definition) => definition.name === name);
	if (!found) throw new Error(`missing fixture definition ${name}`);
	return found;
}

function paramInput(container: HTMLElement, name: string): HTMLElement {
	return container.querySelector(
		`[data-testid="filter-param"][data-param="${name}"]`
	) as HTMLElement;
}

describe('Screener filter builder (generated Rust source)', () => {
	it('adds and removes rows and regenerates the source on every valid edit', async () => {
		const { container } = render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();

		expect(screen.getAllByTestId('filter-row')).toHaveLength(1);
		expect(screen.getByTestId('script-source')).toHaveValue(
			generateScreenerSource([
				{ uid: 1, kind: 'avg_dollar_volume_min', params: { min: '250000000' } }
			])
		);

		await fireEvent.click(screen.getByTestId('add-filter'));
		await tick();
		expect(screen.getAllByTestId('filter-row')).toHaveLength(2);

		await fireEvent.click(screen.getAllByTestId('remove-filter')[0]);
		await tick();
		expect(screen.getAllByTestId('filter-row')).toHaveLength(1);

		await fireEvent.change(screen.getByTestId('filter-kind-select'), {
			target: { value: 'rsi_below' }
		});
		await tick();
		expect(paramInput(container, 'period')).toHaveValue(14);
		expect(screen.getByTestId('script-source')).toHaveValue(
			generateScreenerSource([{ uid: 2, kind: 'rsi_below', params: { period: '14', threshold: '30' } }])
		);
	});

	it('surfaces row validation and blocks the run until it is fixed', async () => {
		const { container } = render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();
		await fireEvent.input(screen.getByTestId('screener-name-input'), {
			target: { value: 'Validation screen' }
		});
		await fireEvent.change(screen.getByTestId('filter-kind-select'), {
			target: { value: 'rsi_below' }
		});
		await tick();
		expect(screen.getByTestId('screener-run')).toBeEnabled();

		await fireEvent.input(paramInput(container, 'threshold'), { target: { value: '150' } });
		await tick();
		expect(screen.getByTestId('filter-row-error')).toHaveTextContent('within [0, 100]');
		expect(screen.getByTestId('screener-run')).toBeDisabled();

		await fireEvent.input(paramInput(container, 'threshold'), { target: { value: '30' } });
		await tick();
		expect(screen.queryByTestId('filter-row-error')).toBeNull();
		expect(screen.getByTestId('screener-run')).toBeEnabled();
	});

	it('keeps manual source edits and regenerates only on request', async () => {
		render(ScreenerPanel, { props: { workspaceId: 'alpha-lab' } });
		await tick();

		const source = screen.getByTestId('script-source');
		await fireEvent.input(source, { target: { value: 'fn broken(' } });
		await tick();
		expect(screen.getByTestId('script-errors')).toHaveTextContent("unbalanced '('");
		expect(screen.getByTestId('script-manual')).toBeInTheDocument();
		expect(screen.getByTestId('screener-run')).toBeDisabled();

		// A filter edit must not clobber the manual source.
		await fireEvent.input(paramInput(document.body, 'min'), { target: { value: '50000000' } });
		await tick();
		expect(screen.getByTestId('script-source')).toHaveValue('fn broken(');

		await fireEvent.click(screen.getByTestId('script-regenerate'));
		await tick();
		expect(screen.getByTestId('script-clean')).toBeInTheDocument();
		expect(screen.getByTestId('script-generated')).toBeInTheDocument();
		expect(screen.getByTestId('script-source')).toHaveValue(
			generateScreenerSource([
				{ uid: 1, kind: 'avg_dollar_volume_min', params: { min: '50000000' } }
			])
		);
	});
});

describe('Screener save flow (workspace artifacts)', () => {
	it('saves the authored definition and lists it with its content hash', async () => {
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
		const hashes = screen.getAllByTestId('saved-study-hash');
		expect(hashes.at(-1)?.textContent?.trim()).toMatch(/^[0-9a-f]{12}…$/);
		// The saved row carries the run configuration the generator produced.
		expect(screen.getByTestId('saved-screeners')).toHaveTextContent('1d · 20d · rank close');
	});
});

describe('Saved definitions (content-addressed contract)', () => {
	it('lists saved definitions with hash, configuration, and source preview', async () => {
		render(ScreenerPanel, {
			props: { workspaceId: 'alpha-lab', initialSaved: [fixtureDefinition('Oversold quality')] }
		});
		await tick();

		const list = screen.getByTestId('saved-screeners');
		expect(list).toHaveTextContent('Oversold quality');
		expect(list).toHaveTextContent('rank ind:rsi(period=14)');
		expect(list).toHaveTextContent('RSI(14) <= 30');
		expect(list).toHaveTextContent('3c4d5e6f7081…');
	});

	it('loads a saved definition back into the builder with hydrated rows', async () => {
		const { container } = render(ScreenerPanel, {
			props: { workspaceId: 'alpha-lab', initialSaved: [fixtureDefinition('Oversold quality')] }
		});
		await tick();

		await fireEvent.click(screen.getByTestId('load-study'));
		await tick();

		expect(screen.getAllByTestId('filter-row')).toHaveLength(2);
		expect(screen.getByTestId('screener-name-input')).toHaveValue('Oversold quality');
		expect(screen.getByTestId('screener-rank-metric')).toHaveValue('ind:rsi(period=14)');
		expect(paramInput(container, 'period')).toHaveValue(14);
		expect(paramInput(container, 'threshold')).toHaveValue(30);
		expect(paramInput(container, 'field')).toHaveValue('revenue');
		expect(paramInput(container, 'min')).toHaveValue(1000000000);
	});
});

describe('Screener run + results render', () => {
	it('runs a saved definition and renders ranked rows with provenance', async () => {
		render(ScreenerPanel, {
			props: { workspaceId: 'alpha-lab', initialSaved: [fixtureDefinition('Liquid large caps')] }
		});
		await tick();

		await fireEvent.click(screen.getByTestId('load-study'));
		await tick();
		await fireEvent.click(screen.getByTestId('screener-run'));

		await waitFor(() => {
			expect(screen.getAllByTestId('screener-row')).toHaveLength(
				fixtures.screenerResults['alpha-lab'].items[0].rows.length
			);
		});
		expect(
			document.querySelector('[data-testid="screener-row"][data-symbol="MSFT"]')
		).not.toBeNull();

		const provenance = screen.getByTestId('screener-provenance');
		expect(provenance).toHaveTextContent('Liquid large caps');
		expect(provenance).toHaveTextContent('sha256:1a2b3c4d5e6f7081');
		expect(provenance).toHaveTextContent('hash 1a2b3c4d5e6f…');
		expect(provenance).toHaveTextContent('as of');
		expect(provenance).toHaveTextContent('ranked by avg_dollar_volume');
		expect(provenance).toHaveTextContent('window 2026-09-05 → 2026-09-25');
		expect(screen.getByTestId('screener-status')).toHaveTextContent('complete');
		expect(screen.getByTestId('provenance-run-id').textContent).toMatch(/^run screen-/);
	});

	it('re-runs a saved definition without loading it into the editor', async () => {
		render(ScreenerPanel, {
			props: { workspaceId: 'alpha-lab', initialSaved: [fixtureDefinition('Liquid large caps')] }
		});
		await tick();

		await fireEvent.click(screen.getByTestId('rerun-study'));
		await waitFor(() => {
			expect(screen.getByTestId('screener-provenance')).toHaveTextContent(
				'sha256:1a2b3c4d5e6f7081'
			);
		});
		expect(screen.getByTestId('screener-name-input')).toHaveValue('');
	});

	it('surfaces harness failures inline next to the authored source', async () => {
		const phantom = {
			...fixtureDefinition('Liquid large caps'),
			definitionId: 'sha256:missing',
			definitionSha256: 'f'.repeat(64)
		};
		render(ScreenerPanel, { props: { workspaceId: 'alpha-lab', initialSaved: [phantom] } });
		await tick();

		await fireEvent.click(screen.getByTestId('rerun-study'));
		await waitFor(() => {
			expect(screen.getByTestId('screener-error')).toHaveTextContent('does not exist');
		});
		expect(screen.getByTestId('script-server-error')).toHaveTextContent('HTTP 404');
	});
});

describe('Fixture mode (no backend)', () => {
	it('ships fixture definitions that satisfy the pre-flight and marker contract', () => {
		for (const workspace of Object.values(fixtures.screeners)) {
			for (const definition of workspace.items) {
				expect(lintScreenerSource(definition.source), definition.name).toEqual([]);
				expect(parseFilterRows(definition.source)?.length, definition.name).toBeGreaterThan(0);
			}
		}
	});

	it('lists, saves, runs, and fetches synthetic complete results', async () => {
		const listed = await listScreeners('alpha-lab');
		expect(listed.length).toBeGreaterThanOrEqual(2);
		const liquid = listed.find((definition) => definition.name === 'Liquid large caps');
		expect(liquid?.schema).toBe('fca-thesis-screener-definition-v1');
		expect(liquid?.definitionId).toBe(`sha256:${liquid?.definitionSha256}`);

		const input = {
			name: 'Fixture API save',
			lookback_days: 10,
			bar_interval: '1d' as const,
			rank_metric: 'close',
			source: generateScreenerSource([])
		};
		const created = await saveScreener('alpha-lab', input);
		const again = await saveScreener('alpha-lab', input);
		expect(again.definitionId).toBe(created.definitionId);
		expect((await listScreeners('alpha-lab')).filter((d) => d.definitionId === created.definitionId)).toHaveLength(1);

		const result = await runScreener('alpha-lab', {
			definition_id: created.definitionId,
			as_of: '2026-09-25'
		});
		expect(result.status).toBe('complete');
		expect(result.definition_hash).toBe(created.definitionSha256);
		expect(result.definition_id).toBe(created.definitionId);
		expect(result.run_id).toMatch(/^screen-[0-9a-f]{16}$/);
		expect(result.rows.length).toBeGreaterThan(0);

		const fetched = await getScreenerResult('alpha-lab', result.result_id);
		expect(fetched.result_id).toBe(result.result_id);
		expect(fetched.rows[0].symbol).toBe(result.rows[0].symbol);

		await expect(
			runScreener('alpha-lab', { definition_id: 'sha256:missing', as_of: '2026-09-25' })
		).rejects.toBeInstanceOf(ThesisApiError);
		await expect(getScreenerResult('alpha-lab', 'sha256:missing')).rejects.toMatchObject({
			status: 404
		});
	});
});
