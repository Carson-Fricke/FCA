import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import StatusSurfaceView from '$lib/components/common/StatusSurfaceView.svelte';
import type { StatusSurface } from '$lib/types';

describe('Status surface rendering (runtime_status.py semantics)', () => {
	it('renders the explicit absence with its reason and no substitute values', () => {
		const unavailable: StatusSurface<unknown> = {
			state: 'unavailable',
			value: null,
			reason: 'unavailable'
		};
		render(StatusSurfaceView, {
			props: { surface: unavailable, label: 'Portfolio' }
		});

		const marker = screen.getByTestId('surface-unavailable');
		expect(marker).toBeInTheDocument();
		expect(screen.getByText(/Portfolio unavailable/i)).toBeInTheDocument();
		expect(screen.getByText('unavailable')).toBeInTheDocument();
		expect(marker.textContent).toContain('not been published');
	});

	it('surfaces the documented 404 reason as not_found', () => {
		const missing: StatusSurface<unknown> = { state: 'unavailable', value: null, reason: 'not_found' };
		render(StatusSurfaceView, {
			props: { surface: missing, label: 'Risk decisions' }
		});
		expect(screen.getByText(/Risk decisions unavailable/i)).toBeInTheDocument();
		expect(screen.getByText('not_found')).toBeInTheDocument();
	});

	it('renders nothing (no fabricated panel) when the surface is available', () => {
		const available: StatusSurface<unknown> = {
			state: 'available',
			value: { equity: '1' },
			reason: null
		};
		const { container } = render(StatusSurfaceView, {
			props: { surface: available, label: 'Portfolio' }
		});
		expect(screen.queryByTestId('surface-unavailable')).toBeNull();
		expect(container.textContent?.trim()).toBe('');
	});
});
