import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { describe, expect, it } from 'vitest';
import ScreenerPanel from '$lib/components/screener/ScreenerPanel.svelte';
import { fixtures } from '$lib/fixtures';
import type { ScreenerDefinition } from '$lib/types';

const alphaSaved = structuredClone(
	fixtures.screeners['alpha-lab'].items
) as unknown as ScreenerDefinition[];
const betaSaved = structuredClone(
	fixtures.screeners['beta-quant'].items
) as unknown as ScreenerDefinition[];

describe('Screener save flow (workspace artifacts)', () => {
	it('applies preset filters, saves a screener, and lists it as saved', async () => {
		render(ScreenerPanel, {
			props: { workspaceId: 'alpha-lab', initialSaved: [], strategies: [] }
		});
		await tick();

		// A volume preset is active by default; adding the price preset makes 2.
		await fireEvent.click(screen.getByText('Price > $20'));
		await tick();
		expect(screen.getByText(/2 filters/)).toBeInTheDocument();

		// Save requires a name; the button stays disabled without one.
		expect(screen.getByTestId('screener-save-button')).toBeDisabled();
		const input = screen.getByTestId('screener-name-input');
		await fireEvent.input(input, { target: { value: 'Overnight gap study' } });
		await tick();

		await fireEvent.click(screen.getByTestId('screener-save-button'));

		await waitFor(() => {
			expect(screen.getByTestId('saved-screeners')).toHaveTextContent('Overnight gap study');
		});
	});

	it('runs the screener and renders results linking matched strategies', async () => {
		render(ScreenerPanel, {
			props: {
				workspaceId: 'alpha-lab',
				initialSaved: structuredClone(alphaSaved),
				strategies: [
					{ id: 'str-1', name: 'Momentum Breakout', symbol: 'SPY' },
					{ id: 'str-2', name: 'Mean Reversion EQ', symbol: 'QQQ' }
				]
			}
		});

		expect(screen.getByTestId('saved-screeners')).toHaveTextContent('Liquid large caps');
		expect(screen.queryByTestId('screener-row')).toBeNull();

		await fireEvent.click(screen.getByTestId('screener-run'));
		await waitFor(() => {
			expect(screen.getAllByTestId('screener-row').length).toBe(fixtures.screenerResults.items.length);
		});
		expect(screen.getByText('Momentum Breakout')).toBeInTheDocument();
	});

	it('lists previously saved screeners from the workspace', () => {
		render(ScreenerPanel, {
			props: { workspaceId: 'beta-quant', initialSaved: structuredClone(betaSaved), strategies: [] }
		});
		expect(screen.getByTestId('saved-screeners')).toHaveTextContent('Liquid futures universe');
	});
});
