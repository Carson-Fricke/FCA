import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/svelte';
import { afterEach, beforeAll, vi } from 'vitest';

afterEach(() => cleanup());

beforeAll(() => {
	// jsdom lacks ResizeObserver and canvas 2D support; the chart components
	// degrade to no-ops under these stubs (no drawing attempted in tests).
	vi.stubGlobal(
		'ResizeObserver',
		class {
			observe() {}
			unobserve() {}
			disconnect() {}
		}
	);

	const noop = () => {};
	const gradient = { addColorStop: noop };
	const ctx = {
		setTransform: noop,
		fillRect: noop,
		beginPath: noop,
		moveTo: noop,
		lineTo: noop,
		stroke: noop,
		fill: noop,
		fillText: noop,
		measureText: () => ({ width: 10 }),
		createLinearGradient: () => gradient,
		strokeRect: noop,
		arcTo: noop,
		closePath: noop,
		save: noop,
		restore: noop
	};
	HTMLCanvasElement.prototype.getContext = vi.fn(() => ctx) as unknown as typeof HTMLCanvasElement.prototype.getContext;
});
